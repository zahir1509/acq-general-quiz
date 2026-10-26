"""
Load test: N simulated phones play a whole quiz against an in-process server.

    python3 tests/loadtest.py                      # 100 teams, 8 questions
    python3 tests/loadtest.py --teams 150 --flaky 0.1

Each phone holds a live event stream, types answers in several edits per
question, and (with --flaky) sometimes drops a request before reading the
reply, then retries the same revision. The run passes only if every team's
final answer on the server equals the last thing that team typed, and no
request outright failed.
"""

import argparse
import http.client
import json
import os
import random
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


class Phone(threading.Thread):
    def __init__(self, port, number, token, flaky, stats, n_questions):
        threading.Thread.__init__(self, daemon=True)
        self.port, self.number, self.token = port, number, token
        self.flaky, self.stats, self.nq = flaky, stats, n_questions
        self.rev = 0
        self.final = {}                 # q -> last answer typed
        self.handled = set()
        self.stop = False
        self.rnd = random.Random(number)
        self.lock = threading.Lock()    # a real phone has one save request in flight at a time

    def request(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        return c

    def save(self, q, answer):
        with self.lock:
            self._save(q, answer)

    def _save(self, q, answer):
        self.rev += 1
        item = {"q": q, "answer": answer, "hint": "", "rev": self.rev, "ts": time.time()}
        for attempt in range(8):
            t0 = time.time()
            try:
                c = self.request("POST", "/api/team/save", {"items": [item]})
                if self.rnd.random() < self.flaky and attempt == 0:
                    c.sock.shutdown(socket.SHUT_RDWR)     # request left, reply never read
                    c.close()
                    self.stats["dropped"] += 1
                    time.sleep(0.2)
                    continue
                r = c.getresponse()
                data = json.loads(r.read() or b"{}")
                c.close()
                self.stats["lat"].append(time.time() - t0)
                if r.status == 429:
                    self.stats["429"] += 1
                    time.sleep(0.3)
                    continue
                if r.status != 200:
                    self.stats["errors"].append((r.status, data))
                    return
                st = data["results"][0]["status"]
                if st == "ok":
                    self.final[q] = answer
                elif st == "closed":
                    self.stats["closed"] += 1
                return
            except Exception:  # noqa: BLE001
                self.stats["retries"] += 1
                time.sleep(0.2)
        self.stats["errors"].append(("gave up", q))

    def type_answer(self, q):
        word = "ans%d_%d" % (self.number, q)
        time.sleep(self.rnd.uniform(0.1, 1.2))
        for cut in sorted(self.rnd.sample(range(2, len(word)), 2)) + [len(word)]:
            self.save(q, word[:cut])
            time.sleep(self.rnd.uniform(0.25, 0.7))

    def run(self):
        try:
            c = self.request("GET", "/api/events?role=team&t=" + self.token)
            c.sock.settimeout(20)
            r = c.getresponse()
            buf = b""
            while not self.stop:
                data = r.read1(65536)
                if not data:
                    break
                buf += data
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    for line in block.split(b"\n"):
                        if line.startswith(b"data: "):
                            self.on_frame(json.loads(line[6:]))
        except Exception:  # noqa: BLE001
            if not self.stop:
                self.stats["stream_errors"] += 1

    def on_frame(self, f):
        if f.get("type") != "state":
            return
        self.stats["frame_lat"].append(max(0.0, time.time() - f["now"]))
        key = (f["phase"], (f.get("q") or {}).get("n"))
        if f["phase"] in ("PASS1", "PASS2") and f.get("q") and key not in self.handled:
            self.handled.add(key)
            threading.Thread(target=self.type_answer, args=(f["q"]["n"],), daemon=True).start()
        elif f["phase"] == "PASS3" and f.get("deadline") and key not in self.handled:
            self.handled.add(key)
            for q in self.rnd.sample(range(1, self.nq + 1), min(4, self.nq)):
                threading.Thread(target=self.type_answer, args=(q,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", type=int, default=100)
    ap.add_argument("--questions", type=int, default=8)
    ap.add_argument("--flaky", type=float, default=0.05)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp()
    app = server.App(os.path.join(tmp, "load.db"), 0, lan_ip="127.0.0.1", pin="pw")
    server.Handler.app = app
    httpd = server.make_server("127.0.0.1", 0, server.Handler)
    port = httpd.server_address[1]
    app.start_threads()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    eng = app.eng
    eng.generate_teams(args.teams, replace=True)
    for i in range(1, args.questions + 1):
        eng.add_question({"number": i, "text": "Question %d?" % i, "official_answer": "ans%d" % i})
    app.publish_all()

    stats = {"lat": [], "frame_lat": [], "errors": [], "retries": 0, "dropped": 0,
             "429": 0, "closed": 0, "stream_errors": 0}
    phones = []
    t0 = time.time()
    for t in eng.teams():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("POST", "/api/team/join", json.dumps({"number": t["number"], "pin": t["pin"],
                                                         "device_id": "d%d" % t["number"]}),
                  {"Content-Type": "application/json"})
        tok = json.loads(c.getresponse().read())["token"]
        c.close()
        p = Phone(port, t["number"], tok, args.flaky, stats, args.questions)
        phones.append(p)
        p.start()
    print("%d phones joined in %.2fs" % (len(phones), time.time() - t0))
    time.sleep(1.0)

    def phase(p):
        eng.set_phase(p)
        app.publish_all()

    for p_name in ("PASS1", "PASS2"):
        phase(p_name)
        for q in range(1, args.questions + 1):
            eng.goto(q)
            app.publish_all()
            time.sleep(2.2)
    phase("PASS3")
    eng.start_timer(10)
    app.publish_all()
    time.sleep(11.5)
    time.sleep(2.0)
    eng.tick()

    for p in phones:
        p.stop = True
    time.sleep(0.5)

    bad = 0
    by_num = {t["number"]: t for t in eng.teams()}
    for p in phones:
        server_ans = {a["q"]: a["answer"] for a in eng.team_answers(by_num[p.number]["id"])}
        for q, ans in p.final.items():
            if server_ans.get(q) != ans:
                bad += 1
    lat, fl = stats["lat"], stats["frame_lat"]
    print("saves           : %d ok-latency samples" % len(lat))
    print("save latency ms : p50 %.0f  p95 %.0f  max %.0f" % (
        statistics.median(lat) * 1000, pct(lat, .95) * 1000, max(lat) * 1000))
    print("frame delivery  : p50 %.0f ms  p95 %.0f  max %.0f (%d frames)" % (
        statistics.median(fl) * 1000, pct(fl, .95) * 1000, max(fl) * 1000, len(fl)))
    print("flaky drops     : %d (retried with the same revision)" % stats["dropped"])
    print("429 back-offs   : %d   transport retries: %d   stream errors: %d   closed: %d" % (
        stats["429"], stats["retries"], stats["stream_errors"], stats["closed"]))
    print("db              : %d commits for %d writes, largest batch %d" % (
        app.db.stats["commits"], app.db.stats["jobs"], app.db.stats["max_batch"]))
    print("final answers wrong on server : %d" % bad)
    print("request errors  : %d %s" % (len(stats["errors"]), stats["errors"][:3]))
    ok = bad == 0 and not stats["errors"]
    print("RESULT          : %s" % ("PASS" if ok else "FAIL"))
    app.stop()
    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
