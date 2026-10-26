"""
HTTP API: role-scoped frame builders and the route table.

The frame builders are the privacy boundary. A phone's frame is built from
scratch, field by field, and never contains an official answer, another team's
answer, or a score. Inspecting the page's JS or calling the API directly gets a
phone exactly the same fields.
"""

import csv
import hmac
import io
import json
import os
import re
import time
from collections import deque

from . import qr
from .engine import QuizError, _f

MAX_BODY = 2 * 1024 * 1024
MAX_TEAM_BODY = 64 * 1024
PRESENT_WINDOW = 30.0


class Raw(object):
    """A pre-built response body (cached blobs, pre-serialised frames)."""

    def __init__(self, body, ctype="application/json; charset=utf-8", etag=None, gzip_ok=True):
        self.body, self.ctype, self.etag, self.gzip_ok = body, ctype, etag, gzip_ok


def normalise_ip(ip):
    ip = (ip or "").strip()
    return ip[7:] if ip.startswith("::ffff:") and "." in ip else ip


def bearer_token(headers, query):
    h = headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return (query.get("t") or [None])[0]


def secrets_match(given, expected):
    return hmac.compare_digest(str(given or "").encode(), str(expected or "").encode())


class Ctx(object):
    def __init__(self, app, method, path, query, body, headers, ip):
        self.app, self.db, self.eng, self.hub = app, app.db, app.eng, app.hub
        self.method, self.path, self.query = method, path, query
        self.body = body if isinstance(body, dict) else {}
        self.headers, self.ip = headers, ip

    def arg(self, name, default=None):
        v = self.body.get(name)
        if v is None:
            v = self.query[name][0] if name in self.query else default
        return v

    def flag(self, name, default=False):
        v = self.arg(name)
        if v is None:
            return default
        return str(v).lower() in ("1", "true", "yes", "on")

    def bearer(self):
        return bearer_token(self.headers, self.query)

    def require_admin(self):
        if not secrets_match(self.bearer(), self.app.admin_token()):
            raise QuizError("Scorer authentication required", 401)

    def require_team(self):
        t = self.eng.team_by_token(self.bearer())
        if not t:
            raise QuizError("Not signed in as a team", 401)
        self.eng.touch(t["id"])
        return t


# ============================================================ frame builders
def _pos(eng):
    nums = eng._numbers()
    cur = int(eng.get_num("current_q", 0))
    return (nums.index(cur) + 1) if cur in nums else 0


def team_frame(app):
    eng = app.eng
    phase = eng.phase()
    cur = eng.current_q() if phase in ("PASS1", "PASS2") else None
    return {
        "type": "state", "role": "team", "quiz_name": eng.get("quiz_name"),
        "phase": phase, "total": len(eng.questions()), "pos": _pos(eng),
        "q": eng.phone_question(cur) if cur else None,
        "deadline": eng.deadline(), "now": time.time(),
        "cv": eng.question_blob()[0],
        "reveal_q": int(eng.get_num("reveal_q", 0)),
        "results": eng.get_bool("results_to_phones", False),
        "show_text": eng.get_bool("show_question_on_phone", True),
    }


def _reveal_block(eng):
    nums = eng._numbers()
    if eng.get("reveal_view") == "list":
        per, page = 6, int(eng.get_num("reveal_page", 0))
        pages = max(1, -(-len(nums) // per))
        page = max(0, min(pages - 1, page))
        qs = eng.questions()[page * per:(page + 1) * per]
        return {"view": "list", "page": page, "pages": pages,
                "items": [{"n": q["number"], "text": q["text"], "answer": q["official_answer"]}
                          for q in qs]}
    q = eng.question_by_number(eng.get_num("reveal_q", 0))
    if not q:
        return {"view": "single", "item": None}
    return {"view": "single", "pos": nums.index(q["number"]) + 1,
            "item": {"n": q["number"], "text": q["text"], "images": q["images"],
                     "answer": q["official_answer"], "alts": q["alt_answers"],
                     "answer_images": q["answer_images"]}}


def screen_frame(app):
    eng = app.eng
    phase = eng.phase()
    cur = eng.current_q() if phase in ("PASS1", "PASS2") else None
    teams = eng.teams()
    f = {
        "type": "state", "role": "screen", "quiz_name": eng.get("quiz_name"),
        "phase": phase, "total": len(eng.questions()), "pos": _pos(eng),
        "q": ({"n": cur["number"], "text": cur["text"], "images": cur["images"]}
              if cur else None),
        "deadline": eng.deadline(), "now": time.time(),
        "pass3_seconds": int(eng.get_num("pass3_seconds", 120)),
        "media": app.media_cmd,
        "join_url": app.join_url(),
        "wifi": {"ssid": eng.get("wifi_ssid") or app.detect_ssid(),
                 "password": eng.get("wifi_password")},
        "team_count": len(teams), "joined_count": sum(1 for t in teams if t["joined_at"]),
    }
    if cur:
        _, here = app.answer_counts()
        f["answered"] = len(here)
    if phase == "REVEAL":
        f["reveal"] = _reveal_block(eng)
    return f


def board_frame(app):
    eng = app.eng
    return {"type": "state", "role": "board", "quiz_name": eng.get("quiz_name"),
            "phase": eng.phase(),
            "scoreboard": [{"rank": r["rank"], "number": r["number"], "name": r["name"],
                            "score": r["score"]} for r in eng.scoreboard()]}


def scorer_frame(app):
    eng = app.eng
    teams = eng.teams()
    totals, here = app.answer_counts()
    online = app.online_ids()
    summary = app.review_summary()
    tiles = [[t["number"], 1 if t["id"] in online else 0, 1 if t["id"] in here else 0,
              totals.get(t["id"], 0), 1 if t["joined_at"] else 0] for t in teams]
    cur = eng.current_q()
    return {
        "type": "state", "role": "scorer", "quiz_name": eng.get("quiz_name"),
        "phase": eng.phase(), "total": len(eng.questions()), "pos": _pos(eng),
        "current": ({"n": cur["number"], "text": cur["text"], "images": cur["images"],
                     "official_answer": cur["official_answer"],
                     "alt_answers": cur["alt_answers"], "notes": cur["notes"]} if cur else None),
        "deadline": eng.deadline(), "now": time.time(),
        "reveal_q": int(eng.get_num("reveal_q", 0)), "reveal_view": eng.get("reveal_view"),
        "reveal_page": int(eng.get_num("reveal_page", 0)),
        "counts": {"teams": len(teams), "joined": sum(1 for t in teams if t["joined_at"]),
                   "online": sum(1 for t in teams if t["id"] in online),
                   "answered": len(here)},
        "tiles": tiles,
        "unreviewed": {"clusters": sum(r["unreviewed_clusters"] for r in summary),
                       "answers": sum(r["unreviewed_answers"] for r in summary)},
        "ver": dict(eng.ver), "media": app.media_cmd,
        "scorer_devices": app.hub.count("scorer"),
        "server": {"sse": app.hub.counts(), "db": dict(app.db.stats),
                   "backup": app.backup.status(), "uptime": int(time.time() - app.started)},
        "join_url": app.join_url(), "scorer_url": app.scorer_url(),
    }


FRAMES = {"team": team_frame, "screen": screen_frame, "board": board_frame,
          "scorer": scorer_frame}

# ================================================================== routes
_failures = {}


def _too_many_failures(ip, bucket, limit, window):
    now = time.time()
    dq = _failures.setdefault((bucket, ip), deque())
    while dq and now - dq[0] > window:
        dq.popleft()
    return len(dq) >= limit


def _record_failure(ip, bucket):
    _failures.setdefault((bucket, ip), deque()).append(time.time())


def route(ctx):
    fn = ROUTES.get((ctx.method, ctx.path))
    if fn is None:
        raise QuizError("Unknown endpoint %s %s" % (ctx.method, ctx.path), 404)
    return fn(ctx)


# ------------------------------------------------------------------ public
def r_health(ctx):
    eng = ctx.eng
    return {"ok": True, "quiz": eng.get("quiz_name"), "phase": eng.phase(),
            "teams": len(eng.teams()), "questions": len(eng.questions())}


def r_pub(ctx):
    """
    Polling fallback for a phone whose event stream has stalled. Answers with
    a few bytes when nothing changed, otherwise the already-serialised frame.
    """
    role = ctx.arg("role", "screen")
    if role not in ("team", "screen", "board", "scorer"):
        raise QuizError("bad role", 400)
    if role == "team":
        ctx.require_team()
    elif role == "scorer":
        ctx.require_admin()
    have = ctx.app.hub.latest.get(role)
    if have is None:
        ctx.app.publish_role(role)
        have = ctx.app.hub.latest[role]
    seq, raw, _ = have
    now = time.time()
    try:
        known = int(ctx.arg("v", -1))
    except (TypeError, ValueError):
        known = -1
    if known == seq:
        return Raw(b'{"ok":true,"t":%.3f,"same":true}' % now)
    return Raw(b'{"ok":true,"t":%.3f,"frame":%s}' % (now, raw))


def r_team_join(ctx):
    if _too_many_failures(ctx.ip, "join", 15, 300):
        raise QuizError("Too many failed attempts from this phone. Wait a few minutes "
                        "or ask the quizmaster.", 429)
    number = ctx.arg("number")
    if number in (None, ""):
        raise QuizError("Team number required", 400)
    try:
        t = ctx.eng.join(number, ctx.arg("pin"), (ctx.arg("device_id") or "").strip()[:64], ctx.ip)
    except QuizError as e:
        if e.code in (401, 404):
            _record_failure(ctx.ip, "join")
        raise
    ctx.db.audit("team:%d" % t["number"], "join", {"ip": ctx.ip})
    ctx.app.mark_dirty()
    return {"ok": True, "token": t["token"], "team": {"number": t["number"], "name": t["name"]}}


def r_team_me(ctx):
    t = ctx.require_team()
    return {"ok": True, "team": {"number": t["number"], "name": t["name"]},
            "answers": ctx.eng.team_answers(t["id"]), "now": time.time(),
            "phase": ctx.eng.phase()}


def r_team_save(ctx):
    t = ctx.require_team()
    now = time.time()
    if now - ctx.app.save_times.get(t["id"], 0.0) < ctx.app.save_min_gap:
        raise QuizError("Slow down", 429)
    ctx.app.save_times[t["id"]] = now
    items = ctx.body.get("items")
    if not isinstance(items, list) or len(items) > 80:
        raise QuizError("items must be a list of up to 80 answers", 400)
    results, touched = ctx.eng.save_answers(t, [i for i in items if isinstance(i, dict)])
    if touched:
        ctx.app.backup.journal(touched)
        ctx.app.backup.mark_dirty()
        ctx.app.mark_dirty()
    return {"ok": True, "results": results, "phase": ctx.eng.phase(), "now": time.time()}


def r_team_questions(ctx):
    ctx.require_team()
    etag, body = ctx.eng.question_blob()
    return Raw(body, etag=etag)


def r_team_reveal(ctx):
    t = ctx.require_team()
    eng = ctx.eng
    out = {"ok": True, "phase": eng.phase(), "items": [], "now": time.time()}
    if eng.phase() == "REVEAL":
        out["items"] = eng.reveal_items(t)
        if eng.get_bool("results_to_phones", False):
            for r in eng.scoreboard():
                if r["number"] == t["number"]:
                    out.update(total=r["score"], rank=r["rank"], of=len(eng.scoreboard()))
    return out


# ------------------------------------------------------------------- admin
def r_admin_login(ctx):
    if _too_many_failures(ctx.ip, "admin_login", 10, 300):
        raise QuizError("Too many failed attempts. Wait five minutes.", 429)
    if not secrets_match((ctx.arg("pin") or "").strip(), ctx.eng.get("admin_pin")):
        _record_failure(ctx.ip, "admin_login")
        raise QuizError("Wrong scorer PIN", 401)
    return {"ok": True, "token": ctx.app.admin_token()}


def r_admin_state(ctx):
    ctx.require_admin()
    return Raw(json.dumps(scorer_frame(ctx.app), default=str).encode())


CONFIG_KEYS = {
    "quiz_name", "default_points", "show_question_on_phone", "send_media_to_phones",
    "results_to_phones", "strict_single_device", "pass3_seconds", "q_timer_seconds",
    "wifi_ssid", "wifi_password",
}
BOOL_KEYS = {"show_question_on_phone", "send_media_to_phones", "results_to_phones",
             "strict_single_device"}


def r_admin_config_get(ctx):
    ctx.require_admin()
    cfg = {k: v for k, v in ctx.eng.cfg.items() if k in CONFIG_KEYS}
    cfg["detected_ssid"] = ctx.app.detect_ssid()
    return {"ok": True, "config": cfg}


def r_admin_config(ctx):
    ctx.require_admin()
    changed = {}
    for k, v in ctx.body.items():
        if k not in CONFIG_KEYS:
            continue
        if k in BOOL_KEYS:
            v = "1" if str(v).lower() in ("1", "true", "yes", "on") else "0"
        elif k in ("default_points", "pass3_seconds", "q_timer_seconds"):
            if _f(v) is None or _f(v) < 0:
                raise QuizError("%s must be a number" % k, 400)
        changed[k] = str(v).strip()
    if changed:
        ctx.eng.set_cfg(changed)
        if "default_points" in changed:
            ctx.eng.bump("g")
        ctx.eng.reload_questions()      # phone-visibility toggles change the question list
        ctx.db.audit("scorer", "config", changed)
        ctx.app.publish_all()
    return {"ok": True}


def _team_row(app, t, online):
    return {"id": t["id"], "number": t["number"], "name": t["name"], "pin": t["pin"],
            "joined": bool(t["joined_at"]), "online": t["id"] in online,
            "active": bool(t["active"])}


def r_teams_list(ctx):
    ctx.require_admin()
    online = ctx.app.online_ids()
    return {"ok": True, "teams": [_team_row(ctx.app, t, online)
                                  for t in ctx.eng.teams(active_only=False)]}


def r_teams_add(ctx):
    ctx.require_admin()
    t = ctx.eng.add_team(ctx.arg("number"), ctx.arg("name"))
    ctx.app.publish_all()
    return {"ok": True, "team": {"id": t["id"], "number": t["number"], "pin": t["pin"]}}


def r_teams_update(ctx):
    ctx.require_admin()
    active = ctx.arg("active")
    t = ctx.eng.update_team(int(ctx.arg("id")), ctx.arg("name"), ctx.arg("number"),
                            None if active is None else ctx.flag("active"))
    ctx.app.publish_all()
    return {"ok": True, "team": {"id": t["id"], "number": t["number"], "name": t["name"]}}


def r_teams_rename(ctx):
    ctx.require_admin()
    names = ctx.arg("names") or []
    if isinstance(names, str):
        names = names.splitlines()
    ctx.eng.rename_teams(names)
    ctx.app.publish_all()
    return {"ok": True}


def r_teams_reset(ctx):
    ctx.require_admin()
    t = ctx.eng.reset_team_session(int(ctx.arg("id")))
    ctx.app.publish_all()
    return {"ok": True, "team": {"id": t["id"], "number": t["number"], "pin": t["pin"]}}


def r_teams_generate(ctx):
    ctx.require_admin()
    try:
        count = int(ctx.arg("count"))
    except (TypeError, ValueError):
        raise QuizError("count must be a number", 400)
    if not 1 <= count <= 300:
        raise QuizError("count must be between 1 and 300", 400)
    ctx.eng.generate_teams(count, ctx.arg("names") or [], ctx.flag("replace"))
    ctx.db.audit("scorer", "teams.generate", {"count": count})
    ctx.app.publish_all()
    return {"ok": True, "created": count}


def r_team_sheet(ctx):
    ctx.require_admin()
    return dict(ctx.eng.team_sheet(int(ctx.arg("id"))), ok=True)


def _qrow(app, q):
    base = app.media_dir
    sizes = []
    for u in q["images"]:
        p = os.path.join(base, u[len("/media/"):]) if u.startswith("/media/") else ""
        sizes.append(os.path.getsize(p) if p and os.path.isfile(p) else 0)
    return {"id": q["id"], "number": q["number"], "text": q["text"], "images": q["images"],
            "answer_images": q["answer_images"], "official_answer": q["official_answer"],
            "alt_answers": q["alt_answers"], "notes": q["notes"], "points": q["points"],
            "media_bytes": sizes}


def r_questions_list(ctx):
    ctx.require_admin()
    return {"ok": True, "questions": [_qrow(ctx.app, q) for q in ctx.eng.questions()],
            "default_points": ctx.eng.get_num("default_points", 10.0)}


def r_questions_add(ctx):
    ctx.require_admin()
    data = dict(ctx.body)
    if "images" in data and isinstance(data["images"], str):
        data["images"] = [u.strip() for u in data["images"].split("|") if u.strip()]
    q = ctx.eng.add_question(data)
    ctx.app.publish_all()
    return {"ok": True, "question": {"id": q["id"], "number": q["number"]}}


def r_questions_update(ctx):
    ctx.require_admin()
    data = dict(ctx.body)
    if "images" in data and isinstance(data["images"], str):
        data["images"] = [u.strip() for u in data["images"].split("|") if u.strip()]
    q = ctx.eng.update_question(int(ctx.arg("id")), data)
    ctx.app.publish_all()
    return {"ok": True, "question": {"id": q["id"], "number": q["number"]}}


def r_questions_delete(ctx):
    ctx.require_admin()
    ctx.eng.delete_question(int(ctx.arg("id")))
    ctx.app.publish_all()
    return {"ok": True}


# Column names accepted on import, in any order and capitalisation.
CSV_ALIASES = {
    "number": ("question number", "question no", "number", "no", "q", "qno", "q no", "question_no"),
    "text": ("question", "text", "question text", "question_text"),
    "official_answer": ("answer", "official answer", "correct answer", "official_answer"),
    "alt_answers": ("also accepted", "alternate answers", "alt", "alternates", "acceptable",
                    "acceptable answers", "alt_answers", "also_accepted"),
    "points": ("points", "marks", "score", "point"),
    "image_url": ("image url", "image", "media", "media_url", "picture"),
    "notes": ("notes", "note", "qm notes", "quizmaster notes"),
}
TEMPLATE_COLUMNS = ["Question Number", "Question", "Answer", "Also Accepted", "Points",
                    "Image URL", "Notes"]
TEMPLATE_HELP = [
    "# One row per question. Lines starting with # are ignored.",
    "# Answer       : the official answer. Matching ignores case, spaces, punctuation,"
    " accents and a leading The/A/An, so 'The Ganges' and 'ganges' are the same.",
    "# Also Accepted: other wordings you would accept, separated by |  e.g.  Ganga|River Ganga",
    "# Points       : worth of this question. Blank = the quiz default.",
    "# Image URL    : optional; pictures named 5.jpg or 5_1.jpg in media/ are found automatically.",
]


def parse_questions_csv(text):
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    rows = []
    for raw in csv.DictReader(io.StringIO("\n".join(lines))):
        norm = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        out = {}
        for field, names in CSV_ALIASES.items():
            for n in names:
                if norm.get(n):
                    out[field] = norm[n]
                    break
        if not out.get("text") and not out.get("official_answer"):
            continue
        alts = out.get("alt_answers")
        if alts:
            sep = "|" if "|" in alts else ";" if ";" in alts else ","
            out["alt_answers"] = [x.strip() for x in alts.split(sep) if x.strip()]
        if out.get("points") is not None:
            out["points"] = _f(out["points"])
        rows.append(out)
    return rows


MEDIA_EXTENSIONS = ("jpe?g", "png", "gif", "webp", "mp3", "m4a", "aac", "wav", "ogg", "opus",
                    "flac", "mp4", "m4v", "mov", "webm")


def numbered_media_for_import(directory, number, url_prefix="/media/"):
    """Numbered image/audio/video files for a question, in stable order."""
    if not directory or not os.path.isdir(directory):
        return []
    pat = re.compile(r"^%s(?:_(\d+))?\.(?:%s)$" % (int(number), "|".join(MEDIA_EXTENSIONS)),
                     re.IGNORECASE)
    hits = []
    for name in os.listdir(directory):
        m = pat.match(name)
        if m and os.path.isfile(os.path.join(directory, name)):
            hits.append((int(m.group(1)) if m.group(1) else 0, name.lower(), name))
    hits.sort()
    return [url_prefix + n for _, _, n in hits]


def questions_template_csv(filled=False):
    out = io.StringIO()
    w = csv.writer(out)
    for line in TEMPLATE_HELP:
        out.write(line + "\n")
    w.writerow(TEMPLATE_COLUMNS)
    if filled:
        w.writerow([1, "Which Indian city is known as the Pink City?", "Jaipur", "Jaipur City",
                    10, "", "Painted pink in 1876"])
        w.writerow([2, "Who wrote 'Midnight's Children'?", "Salman Rushdie",
                    "Rushdie|Ahmed Salman Rushdie", 10, "", "Booker Prize 1981"])
    else:
        for _ in range(12):
            w.writerow([""] * len(TEMPLATE_COLUMNS))
    return out.getvalue()


def r_questions_import(ctx):
    ctx.require_admin()
    text = ctx.body.get("csv") or ""
    if not text.strip():
        raise QuizError("No CSV content", 400)
    rows = parse_questions_csv(text)
    if not rows:
        raise QuizError("No usable rows found. Need at least a question or answer column.", 400)
    eng = ctx.eng
    if ctx.flag("replace"):
        for q in eng.questions():
            eng.delete_question(q["id"])
    added, skipped = 0, []
    for i, r in enumerate(rows):
        try:
            number = r.get("number") or (max([q["number"] for q in eng.questions()] or [0]) + 1)
            images = [u for u in [r.pop("image_url", "")] if u]
            for u in numbered_media_for_import(ctx.app.media_dir, number):
                if u not in images:
                    images.append(u)
            r["images"] = images
            r["answer_images"] = numbered_media_for_import(
                os.path.join(ctx.app.media_dir, "answers"), number, "/media/answers/")
            eng.add_question(r)
            added += 1
        except (QuizError, ValueError) as e:
            skipped.append("row %d: %s" % (i + 2, getattr(e, "message", str(e))))
    ctx.db.audit("scorer", "questions.import", {"added": added, "skipped": len(skipped)})
    ctx.app.publish_all()
    return {"ok": True, "added": added, "skipped": skipped}


# ------------------------------------------------------------- quiz control
def r_phase(ctx):
    ctx.require_admin()
    ctx.eng.set_phase(ctx.arg("phase"))
    ctx.app.publish_all()
    return {"ok": True, "phase": ctx.eng.phase()}


def r_step(ctx):
    ctx.require_admin()
    if ctx.arg("goto") not in (None, ""):
        ctx.eng.goto(int(ctx.arg("goto")))
    else:
        ctx.eng.step(int(ctx.arg("delta", 1)))
    ctx.app.publish_all()
    return {"ok": True}


def r_timer(ctx):
    ctx.require_admin()
    action = ctx.arg("action")
    eng = ctx.eng
    if action == "start":
        secs = _f(ctx.arg("seconds"))
        if secs is None:
            secs = eng.get_num("pass3_seconds", 120) if eng.phase() == "PASS3" \
                else eng.get_num("q_timer_seconds", 30)
        eng.start_timer(secs)
    elif action == "extend":
        eng.extend_timer(_f(ctx.arg("seconds"), 30.0))
    elif action == "clear":
        eng.clear_timer()
    else:
        raise QuizError("action must be start, extend or clear", 400)
    ctx.app.publish_all()
    return {"ok": True}


def r_reveal(ctx):
    ctx.require_admin()
    eng = ctx.eng
    if eng.phase() != "REVEAL":
        raise QuizError("Start the reveal phase first", 409)
    action = ctx.arg("action")
    if action in ("next", "prev"):
        delta = 1 if action == "next" else -1
        if eng.get("reveal_view") == "list":
            nums = eng._numbers()
            pages = max(1, -(-len(nums) // 6))
            page = max(0, min(pages - 1, int(eng.get_num("reveal_page", 0)) + delta))
            eng.set_cfg({"reveal_page": page})
        else:
            eng.reveal_step(delta)
    elif action == "view":
        view = "list" if ctx.arg("view") == "list" else "single"
        eng.set_cfg({"reveal_view": view})
    elif action == "goto":
        q = eng.question_by_number(ctx.arg("q"))
        if not q:
            raise QuizError("No such question", 404)
        eng.set_cfg({"reveal_q": q["number"]})
    else:
        raise QuizError("unknown reveal action", 400)
    ctx.app.publish_all()
    return {"ok": True}


def r_media(ctx):
    """Projector playback control: play | pause | restart | seek | volume | mute."""
    ctx.require_admin()
    action = ctx.arg("action")
    if action not in ("play", "pause", "restart", "seek", "volume", "mute", "unmute"):
        raise QuizError("unknown media action", 400)
    m = ctx.app.media_cmd
    ctx.app.media_cmd = {"n": m["n"] + 1, "action": action,
                         "value": _f(ctx.arg("value"), 0.0), "idx": int(_f(ctx.arg("idx"), 0))}
    ctx.app.publish_role("screen")
    ctx.app.publish_role("scorer")
    return {"ok": True}


# ------------------------------------------------------------------ marking
def r_review_summary(ctx):
    ctx.require_admin()
    return {"ok": True, "summary": ctx.app.review_summary()}


def r_review(ctx):
    ctx.require_admin()
    return dict(ctx.eng.review(int(ctx.arg("q"))), ok=True)


def r_grade(ctx):
    ctx.require_admin()
    fraction = ctx.arg("fraction")
    ctx.eng.set_grade(int(ctx.arg("q")), ctx.arg("norm"),
                      None if fraction in (None, "", "clear") else fraction,
                      ctx.arg("label") or "")
    ctx.app.mark_dirty()
    ctx.app.backup.mark_dirty()
    return {"ok": True}


def r_grade_rest_wrong(ctx):
    ctx.require_admin()
    q = ctx.arg("q")
    n = ctx.eng.mark_rest_wrong(int(q) if q not in (None, "", "all") else None)
    ctx.app.mark_dirty()
    ctx.app.backup.mark_dirty()
    return {"ok": True, "marked": n}


def r_adjust(ctx):
    ctx.require_admin()
    ctx.eng.adjust(int(ctx.arg("team_id")), float(ctx.arg("points")), ctx.arg("note") or "")
    ctx.app.mark_dirty()
    ctx.app.backup.mark_dirty()
    return {"ok": True}


def r_scoreboard(ctx):
    ctx.require_admin()
    return {"ok": True, "scoreboard": ctx.eng.scoreboard()}


def r_audit(ctx):
    ctx.require_admin()
    return {"ok": True, "audit": ctx.db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")}


# ------------------------------------------------------------------ danger
def r_change_pin(ctx):
    ctx.require_admin()
    if _too_many_failures(ctx.ip, "change_pin", 6, 300):
        raise QuizError("Too many failed attempts. Wait five minutes.", 429)
    if not secrets_match((ctx.arg("current_pin") or "").strip(), ctx.eng.get("admin_pin")):
        _record_failure(ctx.ip, "change_pin")
        raise QuizError("Current PIN is wrong - nothing was changed.", 401)
    new = (ctx.arg("new_pin") or "").strip()
    if not 3 <= len(new) <= 64:
        raise QuizError("New PIN must be 3 to 64 characters", 400)
    ctx.eng.set_cfg({"admin_pin": new})
    ctx.db.audit("scorer", "admin_pin.change", {"length": len(new)})
    return {"ok": True, "token": ctx.app.admin_token()}


def _check_pin(ctx, bucket):
    if _too_many_failures(ctx.ip, bucket, 5, 300):
        raise QuizError("Too many failed attempts. Wait five minutes.", 429)
    pin = (ctx.arg("pin") or "").strip()
    if not pin or not secrets_match(pin, ctx.eng.get("admin_pin")):
        _record_failure(ctx.ip, bucket)
        raise QuizError("Wrong scorer PIN - nothing was deleted.", 401)


def r_clear(ctx):
    ctx.require_admin()
    _check_pin(ctx, "clear")
    archived = ctx.app.backup.archive_now("before-clear")
    ctx.eng.clear_answers()
    ctx.app.publish_all()
    return {"ok": True, "archived_to": archived}


def r_master_reset(ctx):
    ctx.require_admin()
    _check_pin(ctx, "master_reset")
    if (ctx.arg("confirm") or "").strip().upper() != "RESET EVERYTHING":
        raise QuizError("Type RESET EVERYTHING to confirm - nothing was deleted.", 400)
    archived = ctx.app.backup.archive_now("before-master-reset")
    stats = ctx.eng.master_reset(ctx.flag("drop_questions", True))
    ctx.app.publish_all()
    return {"ok": True, "cleared": stats, "archived_to": archived}


def r_reset_stats(ctx):
    ctx.require_admin()
    return {"ok": True, "stats": ctx.eng.quiz_stats()}


# ----------------------------------------------------------------- exports
def export_csv(app):
    from . import backup
    eng = app.eng
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["# Quiz", eng.get("quiz_name"), "# Exported", time.strftime("%Y-%m-%d %H:%M:%S")])
    for part in (backup.scoreboard_rows, backup.matrix_rows, backup.answers_rows):
        for row in part(eng):
            w.writerow(row)
        w.writerow([])
    return out.getvalue()


def export_matrix_csv(app):
    from . import backup
    out = io.StringIO()
    csv.writer(out).writerows(backup.matrix_rows(app.eng))
    return out.getvalue()


def export_json(app):
    eng = app.eng
    return json.dumps({
        "quiz_name": eng.get("quiz_name"), "exported_at": time.time(),
        "config": {k: v for k, v in eng.cfg.items() if k != "admin_pin"},
        "teams": [{"number": t["number"], "name": t["name"]} for t in eng.teams(active_only=False)],
        "scoreboard": eng.scoreboard(),
        "questions": eng.questions(),
        "answers": app.db.q("SELECT * FROM answers"),
        "grades": app.db.q("SELECT * FROM grades"),
        "adjustments": app.db.q("SELECT * FROM adjustments"),
        "answer_log": app.db.q("SELECT * FROM answer_log ORDER BY id"),
        "audit_log": app.db.q("SELECT * FROM audit_log ORDER BY id"),
    }, indent=2, default=str)


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def export_team_cards(app):
    """Printable one-per-team join slips with a QR code each."""
    eng, base = app.eng, app.join_url()
    cards = []
    for t in eng.teams():
        url = "%s?n=%d" % (base, t["number"])
        cards.append(
            '<div class="card"><div class="tno">TEAM %d</div><div class="tname">%s</div>%s'
            '<div class="pin">PIN <b>%s</b></div><div class="url">%s</div></div>'
            % (t["number"], _esc(t["name"]), qr.to_svg(url, module=4, quiet=2), t["pin"], _esc(base)))
    return (
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:8mm;"
        "background:#fff;color:#000}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:6mm}"
        ".card{border:2px dashed #999;border-radius:6px;padding:6mm;text-align:center;"
        "break-inside:avoid}.tno{font-size:26px;font-weight:800;letter-spacing:1px}"
        ".tname{font-size:16px;margin:2px 0 6px;color:#333}.card svg{width:130px;height:130px}"
        ".pin{font-size:22px;margin-top:6px;letter-spacing:3px}"
        ".url{font-size:11px;color:#555;margin-top:4px;word-break:break-all}h1{font-size:18px}"
        "@media print{@page{margin:8mm}}</style>"
        "<h1>%s &mdash; team join slips (cut and hand one to each table)</h1>"
        "<div class=grid>%s</div>" % (_esc(eng.get("quiz_name")), "".join(cards)))


ROUTES = {
    ("GET", "/api/health"): r_health,
    ("GET", "/api/pub"): r_pub,
    ("POST", "/api/team/join"): r_team_join,
    ("GET", "/api/team/me"): r_team_me,
    ("POST", "/api/team/save"): r_team_save,
    ("GET", "/api/team/questions"): r_team_questions,
    ("GET", "/api/team/reveal"): r_team_reveal,

    ("POST", "/api/admin/login"): r_admin_login,
    ("GET", "/api/admin/state"): r_admin_state,
    ("GET", "/api/admin/config"): r_admin_config_get,
    ("POST", "/api/admin/config"): r_admin_config,
    ("GET", "/api/admin/teams"): r_teams_list,
    ("POST", "/api/admin/teams"): r_teams_add,
    ("POST", "/api/admin/teams/generate"): r_teams_generate,
    ("POST", "/api/admin/teams/update"): r_teams_update,
    ("POST", "/api/admin/teams/rename"): r_teams_rename,
    ("POST", "/api/admin/teams/reset"): r_teams_reset,
    ("GET", "/api/admin/team"): r_team_sheet,
    ("GET", "/api/admin/questions"): r_questions_list,
    ("POST", "/api/admin/questions"): r_questions_add,
    ("POST", "/api/admin/questions/update"): r_questions_update,
    ("POST", "/api/admin/questions/delete"): r_questions_delete,
    ("POST", "/api/admin/questions/import"): r_questions_import,
    ("POST", "/api/admin/phase"): r_phase,
    ("POST", "/api/admin/step"): r_step,
    ("POST", "/api/admin/timer"): r_timer,
    ("POST", "/api/admin/reveal"): r_reveal,
    ("POST", "/api/admin/media"): r_media,
    ("GET", "/api/admin/review/summary"): r_review_summary,
    ("GET", "/api/admin/review"): r_review,
    ("POST", "/api/admin/grade"): r_grade,
    ("POST", "/api/admin/grade/rest_wrong"): r_grade_rest_wrong,
    ("POST", "/api/admin/adjust"): r_adjust,
    ("GET", "/api/admin/scoreboard"): r_scoreboard,
    ("GET", "/api/admin/audit"): r_audit,
    ("POST", "/api/admin/change_pin"): r_change_pin,
    ("POST", "/api/admin/clear"): r_clear,
    ("POST", "/api/admin/master_reset"): r_master_reset,
    ("GET", "/api/admin/reset_stats"): r_reset_stats,
}
