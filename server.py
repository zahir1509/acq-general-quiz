#!/usr/bin/env python3
"""
Prelims quiz server (TCS IT Wiz style) -- stdlib only.

    python3 server.py                 # loads/creates data/wiz.db
    python3 server.py --seed          # wipe and load a 20-team demo
    python3 server.py --port 8080 --pin 4321

Scorer (laptop)  : http://localhost:8080/scorer
Projector        : http://<laptop-ip>:8080/screen
Team phones      : http://<laptop-ip>:8080/j     (or scan the QR)
"""

import argparse
import gzip
import hashlib
import json
import os
import queue
import random
import re
import socket
import sys
import threading
import time
import traceback
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wiz import api, netinfo, qr  # noqa: E402
from wiz.backup import Backup  # noqa: E402
from wiz.db import Database  # noqa: E402
from wiz.engine import Engine, QuizError  # noqa: E402
from wiz.hub import Hub, chunk, frame_bytes  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
MEDIA = os.path.join(HERE, "media")

PAGES = {
    "/": "scorer.html", "/scorer": "scorer.html", "/welcome": "welcome.html",
    "/team": "team.html", "/screen": "screen.html", "/board": "board.html",
    "/j": "join.html", "/join": "join.html",
}

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".flac": "audio/flac", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".webmanifest": "application/manifest+json", ".ico": "image/x-icon",
}
RANGE_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac",
                    ".mp4", ".m4v", ".mov", ".webm"}
COMPRESSIBLE = {".html", ".css", ".js", ".json", ".svg", ".webmanifest"}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico")

DOWNLOADS = {
    "/export.csv": (api.export_csv, "text/csv; charset=utf-8", "quiz-results.csv", True),
    "/export-matrix.csv": (api.export_matrix_csv, "text/csv; charset=utf-8",
                           "team-x-question.csv", True),
    "/export.json": (api.export_json, "application/json; charset=utf-8", "quiz-results.json", True),
    "/question-template.csv": (lambda app: api.questions_template_csv(),
                               "text/csv; charset=utf-8", "questions-template.csv", False),
    "/question-example.csv": (lambda app: api.questions_template_csv(filled=True),
                              "text/csv; charset=utf-8", "questions-example.csv", False),
}

SSE_SEND_TIMEOUT = 20.0     # a phone that cannot take a frame in this long is dropped
IDLE_TIMEOUT = 30.0         # idle keep-alive sockets are reaped so threads never pile up
LATE_FRAME_MIN_GAP = 0.4    # coalescing window for save-driven projector/scorer updates


@lru_cache(maxsize=64)
def _qr_svg(url):
    return qr.to_svg(url)


# ================================================================== app
class App(object):
    def __init__(self, db_path, port, host="0.0.0.0", pin=None, lan_ip=None):
        self.db = Database(db_path)
        self.eng = Engine(self.db)
        self.hub = Hub()
        self.port, self.host = port, host
        self.root = os.path.dirname(os.path.abspath(db_path))
        self.media_dir = MEDIA
        self.backup = Backup(self.eng, self.root)
        self.db.on_audit = lambda ts, actor, action, detail: self.backup.event(
            ts, actor, action, detail)
        if not self.eng.get("server_secret"):
            self.eng.set_cfg({"server_secret": os.urandom(16).hex()})
        if pin:
            self.eng.set_cfg({"admin_pin": pin})
        self.lan_ip = lan_ip or netinfo.detect_lan_ip()
        self.save_times = {}
        self.save_min_gap = 0.15     # one team may not flood the writer with saves
        self.media_cmd = {"n": 0, "action": "", "value": 0, "idx": 0}
        self.started = time.time()
        self._ssid = ""
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._pub_lock = threading.Lock()
        self._memo = {}

    # ------------------------------------------------------------ identity
    def admin_token(self):
        """Derived, not random: survives a restart, and rotates when the PIN changes."""
        raw = self.eng.get("server_secret") + "|" + self.eng.get("admin_pin")
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    def base_url(self):
        return "http://%s:%d" % (self.lan_ip, self.port)

    def join_url(self):
        return self.base_url() + "/j"

    def scorer_url(self):
        return self.base_url() + "/scorer"

    def detect_ssid(self):
        return self._ssid

    # ----------------------------------------------------- cached queries
    def _memoize(self, name, key, build):
        hit = self._memo.get(name)
        if hit and hit[0] == key:
            return hit[1]
        value = build()
        self._memo[name] = (key, value)
        return value

    def answer_counts(self):
        e = self.eng
        return self._memoize("counts", (e.ver["a"], e.ver["t"], e.get("current_q")),
                             e.answered_counts)

    def review_summary(self):
        e = self.eng
        return self._memoize("summary", (e.ver["a"], e.ver["g"], e.ver["t"], e.ver["q"]),
                             e.review_summary)

    def online_ids(self):
        now = time.time()
        ids = set(self.hub.online_team_ids())
        ids.update(i for i, t in list(self.eng.last_seen.items()) if now - t < api.PRESENT_WINDOW)
        return ids

    # ---------------------------------------------------------- publishing
    def publish_role(self, role):
        try:
            frame = api.FRAMES[role](self)
        except Exception:
            traceback.print_exc()
            return
        self.hub.publish(role, frame)

    def publish_all(self):
        """Immediate: a phase change or setting change must reach phones at once."""
        with self._pub_lock:
            for role in ("team", "screen", "board", "scorer"):
                self.publish_role(role)

    def mark_dirty(self):
        """Something saved: refresh projector and scorer, coalesced."""
        self._dirty.set()

    def start_threads(self):
        def publisher():
            while not self._stop.is_set():
                self._dirty.wait()
                if self._stop.is_set():
                    return
                time.sleep(LATE_FRAME_MIN_GAP)
                self._dirty.clear()
                with self._pub_lock:
                    for role in ("screen", "board", "scorer"):
                        self.publish_role(role)

        def ticker():
            beat = 0
            while not self._stop.wait(0.5):
                try:
                    if self.eng.tick():
                        self.publish_all()
                    beat += 1
                    if beat % 20 == 0:
                        self.hub.ping()
                except Exception:
                    traceback.print_exc()

        def refresh_ssid():
            while True:
                found = netinfo.probe_ssid()
                if found != self._ssid:
                    self._ssid = found
                    self.publish_all()
                if self._stop.wait(30.0 if found else 5.0):
                    return

        for fn, name in ((publisher, "publisher"), (ticker, "ticker"), (refresh_ssid, "ssid")):
            threading.Thread(target=fn, name=name, daemon=True).start()
        self.backup.start()

    def stop(self):
        self._stop.set()
        self._dirty.set()
        self.backup.stop()


# ============================================================ static cache
_static_cache = {}
_static_lock = threading.Lock()
_blob_gz = {}


def load_static(full):
    """(etag, raw, gzipped-or-None); recompressed only when the file changes."""
    info = os.stat(full)
    stamp = (info.st_mtime_ns, info.st_size)
    with _static_lock:
        hit = _static_cache.get(full)
        if hit and hit[0] == stamp:
            return hit[1]
    with open(full, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(full)[1].lower()
    gz = gzip.compress(raw, 6) if ext in COMPRESSIBLE and len(raw) > 600 else None
    value = ('"%x-%x"' % stamp, raw, gz)
    with _static_lock:
        _static_cache[full] = (stamp, value)
    return value


# ============================================================== handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Wiz"
    timeout = IDLE_TIMEOUT
    disable_nagle_algorithm = True
    app = None

    def log_message(self, fmt, *args):
        if os.environ.get("WIZ_VERBOSE"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------ plumbing
    def _accepts_gzip(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:                 # the phone went away mid-reply: routine
                self.close_connection = True

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, default=str, separators=(",", ":")), extra=extra)

    def _client_ip(self):
        return api.normalise_ip(self.client_address[0])

    def _error(self, e):
        extra = {"Retry-After": "1"} if e.code == 429 else None
        self._json(e.code, {"ok": False, "error": e.message}, extra)

    def _auth_admin_or_401(self, query):
        if api.secrets_match(api.bearer_token(self.headers, query), self.app.admin_token()):
            return False
        self._json(401, {"ok": False, "error": "Scorer authentication required"})
        return True

    # ----------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path, query = u.path, parse_qs(u.query)
        try:
            if path == "/api/events":
                return self.sse(query)
            if path.startswith("/api/"):
                return self.api(path, query, {})
            if path == "/qr.svg":
                target = (query.get("url") or [self.app.join_url()])[0]
                if len(target) > 200:
                    return self._send(400, "url too long", "text/plain")
                return self._send(200, _qr_svg(target), "image/svg+xml",
                                  {"Cache-Control": "max-age=3600"})
            if path in DOWNLOADS:
                make, ctype, filename, needs_auth = DOWNLOADS[path]
                if needs_auth and self._auth_admin_or_401(query):
                    return
                return self._send(200, make(self.app), ctype,
                                  {"Content-Disposition": 'attachment; filename="%s"' % filename})
            if path == "/cards":
                if self._auth_admin_or_401(query):
                    return
                return self._send(200, api.export_team_cards(self.app), "text/html; charset=utf-8")
            if path in PAGES:
                return self.file_from(STATIC, PAGES[path])
            if path == "/sample-questions.csv":
                return self.file_from(HERE, "sample-questions.csv")
            if path.startswith("/media/"):
                name = path[len("/media/"):]
                return self.file_from(MEDIA, name,
                                      allow_range=os.path.splitext(name)[1].lower() in RANGE_EXTENSIONS)
            return self.file_from(STATIC, path.lstrip("/"))
        except QuizError as e:
            return self._error(e)
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": str(e)})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            limit = api.MAX_BODY if u.path.startswith("/api/admin/") else api.MAX_TEAM_BODY
            if n < 0:
                return self._json(400, {"ok": False, "error": "bad Content-Length"})
            if n > limit:
                self.close_connection = True
                return self._json(413, {"ok": False, "error": "body too large"})
            raw = self.rfile.read(n) if n else b""
            body = {}
            if raw:
                ct = (self.headers.get("Content-Type") or "").lower()
                if "application/json" in ct or raw[:1] in (b"{", b"["):
                    body = json.loads(raw.decode("utf-8"))
                else:
                    body = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
            return self.api(u.path, parse_qs(u.query), body)
        except QuizError as e:
            return self._error(e)
        except ValueError:
            return self._json(400, {"ok": False, "error": "malformed request"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": str(e)})

    def api(self, path, query, body):
        ctx = api.Ctx(self.app, self.command, path, query, body, self.headers, self._client_ip())
        try:
            result = api.route(ctx)
        except QuizError as e:
            return self._error(e)
        if isinstance(result, api.Raw):
            return self._raw(result)
        if isinstance(result, dict) and "ok" not in result:
            result = dict(result, ok=True)
        return self._json(200, result)

    def _raw(self, r):
        extra = {}
        body = r.body
        if r.etag:
            extra["ETag"] = r.etag
            extra["Cache-Control"] = "no-cache"
            if self.headers.get("If-None-Match") == r.etag:
                return self._send(304, b"", r.ctype, extra)
        if r.gzip_ok and len(body) > 1024 and self._accepts_gzip():
            key = r.etag or hashlib.md5(body).hexdigest()
            gz = _blob_gz.get(key)
            if gz is None:
                gz = _blob_gz[key] = gzip.compress(body, 5)
                if len(_blob_gz) > 16:
                    _blob_gz.pop(next(iter(_blob_gz)))
            body = gz
            extra["Content-Encoding"] = "gzip"
        self._send(200, body, r.ctype, extra)

    # ----------------------------------------------------------------- SSE
    def sse(self, query):
        app = self.app
        role = (query.get("role") or ["screen"])[0]
        token = (query.get("t") or [""])[0]
        team = None
        if role == "team":
            team = app.eng.team_by_token(token)
            if not team:
                return self._json(401, {"ok": False, "error": "bad team token"})
        elif role == "scorer":
            if not api.secrets_match(token, app.admin_token()):
                return self._json(401, {"ok": False, "error": "bad scorer token"})
        elif role not in ("screen", "board"):
            role = "screen"
        sub = app.hub.subscribe(role, team["id"] if team else None)
        if sub is None:
            self.close_connection = True
            return self._json(503, {"ok": False, "error": "server is full"},
                              {"Retry-After": "5"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(SSE_SEND_TIMEOUT)
        if team:
            app.eng.touch(team["id"])
        app.mark_dirty()
        try:
            # a random reconnect delay spreads a mass reconnect over a few seconds
            self.wfile.write(chunk(b"retry: %d\n\n" % random.randint(1500, 6000)))
            have = app.hub.latest.get(role)
            if have is None:
                app.publish_role(role)
                have = app.hub.latest.get(role)
            if have:
                self.wfile.write(have[2])
            self.wfile.write(frame_bytes(
                b"ping", json.dumps({"type": "ping", "t": time.time()}).encode()))
            self.wfile.flush()
            while True:
                try:
                    data = sub.q.get(timeout=12.0)
                except queue.Empty:
                    data = chunk(b": keepalive\n\n")
                self.wfile.write(data)
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            app.hub.unsubscribe(sub)
            app.mark_dirty()
            try:
                self.wfile.write(b"0\r\n\r\n")
            except Exception:
                pass

    # -------------------------------------------------------------- files
    def file_from(self, root, rel, allow_range=False):
        rel = rel.replace("\\", "/").lstrip("/")
        root = os.path.abspath(root)
        full = os.path.abspath(os.path.join(root, rel))
        try:
            inside = os.path.commonpath([root, full]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(full):
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        if allow_range:
            return self.stream_ranged(full, ctype)
        etag, raw, gz = load_static(full)
        extra = {"ETag": etag, "Accept-Ranges": "none",
                 "Cache-Control": "max-age=3600" if ext in IMAGE_EXTENSIONS else "no-cache"}
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", ctype, extra)
        if gz is not None and self._accepts_gzip():
            extra["Content-Encoding"] = "gzip"
            return self._send(200, gz, ctype, extra)
        return self._send(200, raw, ctype, extra)

    def stream_ranged(self, full, ctype):
        size = os.path.getsize(full)
        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range", "")
        bad = {"Content-Range": "bytes */%d" % size, "Accept-Ranges": "bytes"}
        if header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not m or size == 0:
                return self._send(416, b"", ctype, bad)
            first, last = m.groups()
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:
                start = max(size - int(last), 0)
            else:
                return self._send(416, b"", ctype, bad)
            if start >= size or end < start:
                return self._send(416, b"", ctype, bad)
            end, status = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=3600")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    data = f.read(min(64 * 1024, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True


# ============================================================ listener
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # 100 phones joining in the same second overflow the default backlog of 5,
    # and the dropped SYNs show up as multi-second page loads.
    request_queue_size = 512
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


class _Server4(_Server):
    address_family = socket.AF_INET


class _Server6(_Server):
    """Dual-stack, so http://localhost works whether it resolves to ::1 or 127.0.0.1."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        ThreadingHTTPServer.server_bind(self)


WILDCARD = ("", "*", "0.0.0.0", "::")


def make_server(host, port, handler):
    if host in WILDCARD and getattr(socket, "has_ipv6", False):
        try:
            return _Server6(("::", port), handler)
        except OSError:
            pass
    return _Server4(("0.0.0.0" if host in WILDCARD else host, port), handler)


def main():
    ap = argparse.ArgumentParser(description="Prelims quiz server")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "wiz.db"))
    ap.add_argument("--pin", default=None, help="scorer PIN (default: keep existing / 'pyaar')")
    ap.add_argument("--ip", default=None,
                    help="address phones should use, if auto-detection picks the wrong adapter")
    ap.add_argument("--quiz-name", default=None)
    ap.add_argument("--seed", action="store_true", help="wipe the DB and load the 20-team demo")
    ap.add_argument("--reset", action="store_true", help="empty teams, answers and scores first")
    ap.add_argument("--qr", action="store_true", help="print the join QR in the terminal")
    args = ap.parse_args()

    if args.seed:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(args.db + suffix):
                os.remove(args.db + suffix)

    try:
        threading.stack_size(512 * 1024)   # ~300 handler threads at 100 teams
    except (ValueError, RuntimeError):
        pass

    app = App(args.db, args.port, args.host, args.pin, args.ip)
    if args.reset and not args.seed:
        where = app.backup.archive_now("before-cli-reset")
        cleared = app.eng.master_reset(drop_questions=False)
        print("\n  --reset: cleared %d teams and %d answers. Archived to %s\n"
              % (cleared["teams"], cleared["answers"], where or "(archive failed)"))
    if args.seed:
        from sample_data import seed
        seed(app.eng)
    if args.quiz_name is not None:
        app.eng.set_cfg({"quiz_name": args.quiz_name})

    Handler.app = app
    try:
        httpd = make_server(args.host, args.port, Handler)
    except OSError as e:
        print("\n  Could not start on port %d: %s" % (args.port, e))
        print("  ...pick another port:  python server.py --port 8090\n")
        sys.exit(1)
    app.start_threads()
    app.publish_all()

    stats = app.eng.quiz_stats()
    base = app.base_url()
    print("\n  quiz            : %s" % app.eng.get("quiz_name"))
    print("  teams/questions : %d teams, %d questions" % (stats["teams"], stats["questions"]))
    print("  scorer PIN      : %s" % app.eng.get("admin_pin"))
    print("  database        : %s" % os.path.abspath(args.db))
    print("  csv backups     : %s" % os.path.join(app.root, "backups"))
    print("")
    print("  SCORER (laptop) : http://localhost:%d/scorer" % args.port)
    print("  SCORER (others) : %s/scorer" % base)
    print("  WELCOME SCREEN  : %s/welcome     <-- put this up first" % base)
    print("  PROJECTOR       : %s/screen" % base)
    print("  SCOREBOARD      : %s/board" % base)
    print("  TEAM PHONES     : %s/j" % base)
    print("")
    if args.qr:
        print(qr.to_ascii(app.join_url()))
        print("")
    if (stats["answers"] or stats["teams_joined"]) and not args.seed:
        print("  !! This database already holds %d teams (%d joined) and %d answers."
              % (stats["teams"], stats["teams_joined"], stats["answers"]))
        print("  !! For a fresh event run with --reset, or use CLEAR in SETUP.\n")
    print("  Ctrl-C to stop. State is saved on every action; restarting resumes it.\n")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping...")
    finally:
        app.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
