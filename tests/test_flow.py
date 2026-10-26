"""
End-to-end tests: normalisation, the pass flow, save rules, grading, and the
HTTP layer (join, batched saves, event stream, polling fallback).

    python3 tests/test_flow.py
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from wiz import text as T  # noqa: E402
from wiz.engine import QuizError  # noqa: E402


class TextTests(unittest.TestCase):
    def test_equal_keys(self):
        same = [("The Ganges", "ganges"), ("Spider-Man", "spiderman"), ("C.V. Raman", "cv raman"),
                ("Café", "cafe"), ("Rock & Roll", "rock and roll"), ("  Leg   Before Wicket ", "leg before wicket"),
                ("Seven", "7"), ("An Apple", "apple")]
        for a, b in same:
            self.assertEqual(T.key(a), T.key(b), (a, b))

    def test_different_keys(self):
        for a, b in [("1947", "1948"), ("Serbia", "Siberia"), ("Hindu", "Hindi"), ("Nile", "Niles")]:
            self.assertNotEqual(T.key(a), T.key(b), (a, b))

    def test_suggest_never_crosses_numbers(self):
        acc = [(T.key("1947"), T.normalize("1947"), "1947")]
        self.assertIsNone(T.suggest(T.key("1948"), "1948", acc))
        acc = [(T.key("Mahatma Gandhi"), T.normalize("Mahatma Gandhi"), "Mahatma Gandhi")]
        self.assertEqual(T.suggest(T.key("Gandhi"), "gandhi", acc)["kind"], "contains")
        acc = [(T.key("Jaipur"), T.normalize("Jaipur"), "Jaipur")]
        self.assertEqual(T.suggest(T.key("Jaipurr"), "jaipurr", acc)["kind"], "close")
        self.assertIsNone(T.suggest(T.key("Delhi"), "delhi", acc))


class Env(object):
    """A server on an ephemeral port over a temp database."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = server.App(os.path.join(self.tmp.name, "t.db"), 0, lan_ip="127.0.0.1", pin="pw")
        self.app.save_min_gap = 0.0
        server.Handler.app = self.app
        self.httpd = server.make_server("127.0.0.1", 0, server.Handler)
        self.port = self.httpd.server_address[1]
        self.app.start_threads()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.eng = self.app.eng
        self.eng.generate_teams(3, ["Alpha", "Beta", "Gamma"], replace=True)
        for i, (qt, ans, alts) in enumerate([("Capital of France?", "Paris", []),
                                              ("Year of Independence?", "1947", []),
                                              ("Pink City?", "Jaipur", ["Jaipur City"])], 1):
            self.eng.add_question({"number": i, "text": qt, "official_answer": ans,
                                   "alt_answers": alts})
        self.app.publish_all()

    def close(self):
        self.app.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        time.sleep(0.1)
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def call(self, method, path, body=None, token=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        if raw:
            return r.status, data
        return r.status, json.loads(data or b"{}")

    def admin(self):
        return self.call("POST", "/api/admin/login", {"pin": "pw"})[1]["token"]

    def join(self, number):
        t = self.eng.team_by_number(number)
        s, r = self.call("POST", "/api/team/join", {"number": number, "pin": t["pin"], "device_id": "d%d" % number})
        assert s == 200, r
        return r["token"]


def item(q, answer, rev, hint="", ts=None):
    return {"q": q, "answer": answer, "hint": hint, "rev": rev, "ts": ts or time.time()}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.eng = self.env.eng
        self.adm = self.env.admin()

    def tearDown(self):
        self.env.close()

    def phase(self, p):
        s, r = self.env.call("POST", "/api/admin/phase", {"phase": p}, self.adm)
        self.assertEqual(s, 200, r)

    def save(self, token, *items):
        s, r = self.env.call("POST", "/api/team/save", {"items": list(items)}, token)
        self.assertEqual(s, 200, r)
        return r["results"]

    # ----------------------------------------------------------- rules
    def test_pass1_only_revealed_questions_accept_answers(self):
        tok = self.env.join(1)
        self.assertEqual(self.save(tok, item(1, "x", 1))[0]["status"], "closed")   # lobby
        self.phase("PASS1")
        self.assertEqual(self.save(tok, item(1, "Paris", 1))[0]["status"], "ok")
        self.assertEqual(self.save(tok, item(2, "1947", 1))[0]["status"], "closed")  # not shown yet
        self.env.call("POST", "/api/admin/step", {"delta": 1}, self.adm)
        self.assertEqual(self.save(tok, item(2, "1947", 1))[0]["status"], "ok")
        # a delayed save for the question we just left is still welcome
        self.assertEqual(self.save(tok, item(1, "Paris!", 2))[0]["status"], "ok")

    def test_revision_makes_saves_idempotent_and_ordered(self):
        tok = self.env.join(1)
        self.phase("PASS1")
        self.save(tok, item(1, "abc", 5))
        r = self.save(tok, item(1, "abc", 5))[0]
        self.assertEqual((r["status"], r["rev"]), ("ok", 5))             # retry after a lost ack
        r = self.save(tok, item(1, "older", 3))[0]
        self.assertEqual(r["status"], "stale")                            # reordered delivery
        self.assertEqual(r["answer"], "abc")
        me = self.env.call("GET", "/api/team/me", token=tok)[1]
        self.assertEqual(me["answers"][0]["answer"], "abc")

    def test_pass3_needs_timer_then_locks_with_grace(self):
        tok = self.env.join(1)
        self.phase("PASS1")
        self.save(tok, item(1, "Paris", 1, hint="easy"))
        self.phase("PASS3")
        self.assertEqual(self.save(tok, item(1, "Paris", 2))[0]["status"], "closed")  # not started
        self.env.call("POST", "/api/admin/timer", {"action": "start", "seconds": 60}, self.adm)
        self.assertEqual(self.save(tok, item(2, "1947", 1))[0]["status"], "ok")
        # notes are frozen once pass 3 begins
        self.save(tok, item(1, "Paris", 3, hint="changed"))
        self.assertEqual(self.eng.team_answers(self.eng.team_by_number(1)["id"])[0]["hint"], "easy")
        t_before = time.time()
        self.phase("LOCKED")
        time.sleep(0.05)
        # typed before the lock, delivered after it: accepted inside the grace window
        self.assertEqual(self.save(tok, item(2, "1947!", 2, ts=t_before))[0]["status"], "ok")
        # typed after the lock (beyond the half-second clock slack): refused
        time.sleep(0.7)
        self.assertEqual(self.save(tok, item(2, "late", 3, ts=time.time()))[0]["status"], "closed")

    def test_timer_expiry_locks_automatically(self):
        self.phase("PASS3")
        self.env.call("POST", "/api/admin/timer", {"action": "start", "seconds": 5}, self.adm)
        self.eng.set_cfg({"deadline": time.time() - 1})
        self.assertTrue(self.eng.tick())
        self.assertEqual(self.eng.phase(), "LOCKED")

    # --------------------------------------------------------- grading
    def test_autograde_review_and_scoreboard(self):
        toks = {n: self.env.join(n) for n in (1, 2, 3)}
        self.phase("PASS2")
        self.save(toks[1], item(3, "jaipur", 1))             # exact after normalisation
        self.save(toks[2], item(3, "The Jaipur City", 1))    # alternate (article ignored)
        self.save(toks[3], item(3, "Jaipurr", 1))            # near miss: never automatic
        sb = {r["number"]: r for r in self.eng.scoreboard()}
        self.assertEqual((sb[1]["score"], sb[2]["score"], sb[3]["score"]), (10, 10, 0))
        rv = self.env.call("GET", "/api/admin/review?q=3", token=self.adm)[1]
        un = [c for c in rv["clusters"] if c["fraction"] is None]
        self.assertEqual(len(un), 1)
        self.assertEqual(un[0]["suggest"]["kind"], "close")
        self.assertEqual(rv["stats"]["unreviewed_answers"], 1)
        # one decision scores every team that wrote the same thing
        self.env.call("POST", "/api/admin/grade", {"q": 3, "norm": un[0]["norm"], "fraction": 0.5,
                                                   "label": "Jaipurr"}, self.adm)
        self.assertEqual({r["number"]: r["score"] for r in self.eng.scoreboard()}[3], 5)
        # a later team writing the same thing is graded at once
        self.assertEqual(self.env.call("GET", "/api/admin/review/summary", token=self.adm)[1]
                         ["summary"][2]["unreviewed_answers"], 0)

    def test_phone_payloads_never_leak_answers(self):
        tok = self.env.join(1)
        self.phase("PASS1")
        s, raw = self.env.call("GET", "/api/pub?role=team", token=tok, raw=True)
        self.assertNotIn(b"Paris", raw)
        _, blob = self.env.call("GET", "/api/team/questions", token=tok, raw=True)
        self.assertNotIn(b"Paris", blob)
        self.assertNotIn(b"official", blob)

    def test_show_question_toggle_and_media_toggle(self):
        tok = self.env.join(1)
        self.env.call("POST", "/api/admin/config", {"show_question_on_phone": "0"}, self.adm)
        self.phase("PASS1")
        frame = json.loads(self.env.call("GET", "/api/pub?role=team", token=tok, raw=True)[1])["frame"]
        self.assertNotIn("text", frame["q"])
        self.env.call("POST", "/api/admin/config", {"show_question_on_phone": "1"}, self.adm)
        frame = json.loads(self.env.call("GET", "/api/pub?role=team", token=tok, raw=True)[1])["frame"]
        self.assertEqual(frame["q"]["text"], "Capital of France?")
        self.assertNotIn("images", frame["q"])

    def test_auth_and_join_rules(self):
        self.assertEqual(self.env.call("POST", "/api/team/save", {"items": []})[0], 401)
        self.assertEqual(self.env.call("GET", "/api/admin/teams")[0], 401)
        self.assertEqual(self.env.call("POST", "/api/team/join", {"number": 1, "pin": "NOPE"})[0], 401)
        self.env.join(1)
        t = self.eng.team_by_number(1)
        s, _ = self.env.call("POST", "/api/team/join", {"number": 1, "pin": t["pin"], "device_id": "other"})
        self.assertEqual(s, 409)

    def test_event_stream_delivers_frames(self):
        tok = self.env.join(1)
        c = http.client.HTTPConnection("127.0.0.1", self.env.port, timeout=10)
        c.request("GET", "/api/events?role=team&t=" + tok)
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        buf = b""
        deadline = time.time() + 5
        while b"event: state" not in buf and time.time() < deadline:
            buf += r.fp.read1(4096) if hasattr(r.fp, "read1") else r.read(1)
        self.assertIn(b"event: state", buf)
        self.phase("PASS1")
        while b'"phase":"PASS1"' not in buf and time.time() < deadline + 5:
            buf += r.fp.read1(4096)
        self.assertIn(b'"phase":"PASS1"', buf)
        c.close()

    def test_group_commit_under_burst(self):
        toks = {n: self.env.join(n) for n in (1, 2, 3)}
        self.phase("PASS2")
        errors = []

        def hammer(n):
            for i in range(1, 30):
                try:
                    s, r = self.env.call("POST", "/api/team/save",
                                         {"items": [item(1, "a%d" % i, i)]}, toks[n])
                    if s == 429:
                        time.sleep(0.2)
                        continue
                    if s != 200:
                        errors.append((s, r))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                time.sleep(0.16)
        ts = [threading.Thread(target=hammer, args=(n,)) for n in (1, 2, 3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])

    def test_clear_and_reset_guarded_by_pin(self):
        s, _ = self.env.call("POST", "/api/admin/clear", {"pin": "wrong"}, self.adm)
        self.assertEqual(s, 401)
        s, _ = self.env.call("POST", "/api/admin/master_reset", {"pin": "pw", "confirm": "nope"}, self.adm)
        self.assertEqual(s, 400)
        self.assertEqual(len(self.eng.teams()), 3)

    def test_phase_requires_content(self):
        self.eng.delete_question(self.eng.question_by_number(1)["id"])
        self.eng.delete_question(self.eng.question_by_number(2)["id"])
        self.eng.delete_question(self.eng.question_by_number(3)["id"])
        with self.assertRaises(QuizError):
            self.eng.set_phase("PASS1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
