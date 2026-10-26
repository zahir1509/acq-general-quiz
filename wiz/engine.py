"""
The quiz engine: phases, answer saving, grading and scoring.

Every rule that matters is enforced here, server-side. The phone hiding a box
is a courtesy; `save_answers()` refusing the request is the rule.

Format
------
LOBBY -> PASS1 -> PASS2 -> PASS3 -> LOCKED -> REVEAL

* PASS1 / PASS2: the quizmaster steps through the questions one at a time.
  Only the current question is visible. Teams type an Answer and a private
  Hint/Notes. Pass 2 is simply pass 1 again, with everything pre-filled.
* PASS3: teams may jump to any question on their phone for a timed final pass.
* The LATEST answer a team saved is its final answer.
"""

import json
import secrets
import threading
import time

from . import text as T

PHASES = ["LOBBY", "PASS1", "PASS2", "PASS3", "LOCKED", "REVEAL"]
GRACE = 10.0            # seconds after a lock in which a delayed save may still land
MAX_ANSWER = 300
MAX_HINT = 500

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 -- read aloud safely


class QuizError(Exception):
    def __init__(self, message, code=400):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


def new_token():
    return secrets.token_urlsafe(18)


def new_pin(n=4):
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _loads(s, default):
    try:
        v = json.loads(s or "")
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


class Engine(object):
    def __init__(self, db):
        self.db = db
        self.lock = threading.RLock()
        self.last_seen = {}                # team_id -> epoch of last contact
        self.ver = {"q": 0, "t": 0, "a": 0, "g": 0, "s": 0, "c": 0}
        self._sb_cache = (None, None)
        self._ups_cache = (None, None)
        self._blob_cache = {}
        self.reload()

    # ------------------------------------------------------------- caches
    def reload(self):
        self.cfg = self.db.config()
        self.reload_teams()
        self.reload_questions()

    def bump(self, *names):
        for n in names:
            self.ver[n] += 1
        if "a" in names or "g" in names or "q" in names or "t" in names:
            self.ver["s"] += 1

    def reload_teams(self):
        rows = self.db.q("SELECT * FROM teams ORDER BY number")
        with self.lock:
            self._teams_all = rows
            self._teams = [t for t in rows if t["active"]]
            self._by_token = {t["token"]: t for t in self._teams}
            self._by_number = {t["number"]: t for t in rows}
            self._by_id = {t["id"]: t for t in rows}
        self.bump("t")

    def reload_questions(self):
        rows = self.db.q("SELECT * FROM questions ORDER BY number")
        for q in rows:
            q["images"] = _loads(q["images"], [])
            q["answer_images"] = _loads(q["answer_images"], [])
            q["alt_answers"] = _loads(q["alt_answers"], [])
        with self.lock:
            self._questions = rows
            self._q_by_number = {q["number"]: q for q in rows}
            self._q_by_id = {q["id"]: q for q in rows}
            self._blob_cache = {}
        self.bump("q")

    def get(self, key, default=""):
        return self.cfg.get(key, default)

    def get_num(self, key, default=0.0):
        return _f(self.cfg.get(key), default)

    def get_bool(self, key, default=False):
        v = self.cfg.get(key)
        if v is None:
            return default
        return str(v).strip().lower() not in ("", "0", "false", "no", "off")

    def set_cfg(self, values):
        self.db.set_config(values)
        with self.lock:
            self.cfg.update({k: "" if v is None else str(v) for k, v in values.items()})
        self.bump("c")

    def touch(self, team_id):
        self.last_seen[team_id] = time.time()

    # ================================================================ teams
    def teams(self, active_only=True):
        return list(self._teams if active_only else self._teams_all)

    def team_by_token(self, token):
        return self._by_token.get(token) if token else None

    def team_by_number(self, number):
        try:
            return self._by_number.get(int(number))
        except (TypeError, ValueError):
            return None

    def add_team(self, number, name, pin=None):
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise QuizError("Team number must be a whole number", 400)
        if number < 1:
            raise QuizError("Team number must be positive", 400)
        if self.team_by_number(number):
            raise QuizError("Team number %d already exists" % number, 409)
        name = (name or "Team %d" % number).strip()[:60]
        now = time.time()
        self.db.write(lambda c: c.execute(
            "INSERT INTO teams(number, name, token, pin, created_at) VALUES (?,?,?,?,?)",
            (number, name, new_token(), pin or new_pin(), now)))
        self.reload_teams()
        return self.team_by_number(number)

    def update_team(self, team_id, name=None, number=None, active=None):
        t = self._by_id.get(team_id)
        if not t:
            raise QuizError("No such team", 404)
        if number not in (None, "") and int(number) != t["number"]:
            other = self.team_by_number(number)
            if other:
                raise QuizError("Team number %s is taken" % number, 409)
        def job(c):
            if number not in (None, ""):
                c.execute("UPDATE teams SET number = ? WHERE id = ?", (int(number), team_id))
            if name is not None:
                c.execute("UPDATE teams SET name = ? WHERE id = ?", (name.strip()[:60], team_id))
            if active is not None:
                c.execute("UPDATE teams SET active = ? WHERE id = ?", (1 if active else 0, team_id))
        self.db.write(job)
        self.reload_teams()
        return self._by_id[team_id]

    def rename_teams(self, names):
        """Names in order are applied to the teams in number order."""
        def job(c):
            for t, nm in zip(self._teams_all, names):
                nm = (nm or "").strip()
                if nm:
                    c.execute("UPDATE teams SET name = ? WHERE id = ?", (nm[:60], t["id"]))
        self.db.write(job)
        self.reload_teams()

    def reset_team_session(self, team_id):
        """Fresh token + PIN: use when a phone dies or is handed over."""
        if team_id not in self._by_id:
            raise QuizError("No such team", 404)
        self.db.write(lambda c: c.execute(
            "UPDATE teams SET token = ?, pin = ?, device_id = NULL, joined_at = NULL"
            " WHERE id = ?", (new_token(), new_pin(), team_id)))
        self.reload_teams()
        return self._by_id[team_id]

    def generate_teams(self, count, names=None, replace=False):
        if self._teams_all and not replace:
            raise QuizError("There are already %d teams. Send replace:true to start over."
                            % len(self._teams_all), 409)
        names = names or []
        now = time.time()
        def job(c):
            if replace:
                c.execute("DELETE FROM answers")
                c.execute("DELETE FROM answer_log")
                c.execute("DELETE FROM adjustments")
                c.execute("DELETE FROM teams")
            for i in range(1, count + 1):
                nm = (names[i - 1].strip() if i <= len(names) and names[i - 1] else "") \
                    or "Team %d" % i
                c.execute(
                    "INSERT INTO teams(number, name, token, pin, created_at) VALUES (?,?,?,?,?)",
                    (i, nm[:60], new_token(), new_pin(), now))
        self.db.write(job)
        self.reload_teams()
        self.bump("a", "g")

    def join(self, number, pin, device, ip=""):
        t = self.team_by_number(number)
        if not t or not t["active"]:
            raise QuizError("No such team", 404)
        if (pin or "").strip().upper() != (t["pin"] or "").upper():
            raise QuizError("Wrong PIN for Team %s" % t["number"], 401)
        if (t["device_id"] and device and t["device_id"] != device
                and self.get_bool("strict_single_device", True)):
            raise QuizError("Team %s is already signed in on another phone. Ask the "
                            "quizmaster to reset your session." % t["number"], 409)
        now = time.time()
        self.db.write(lambda c: c.execute(
            "UPDATE teams SET device_id = COALESCE(NULLIF(?, ''), device_id),"
            " joined_at = COALESCE(joined_at, ?) WHERE id = ?", (device, now, t["id"])))
        self.reload_teams()
        self.touch(t["id"])
        return self._by_id[t["id"]]

    # ============================================================ questions
    def questions(self):
        return list(self._questions)

    def question_by_number(self, number):
        try:
            return self._q_by_number.get(int(number))
        except (TypeError, ValueError):
            return None

    def points_for(self, q):
        return q["points"] if q["points"] is not None else self.get_num("default_points", 10.0)

    def _clean_alts(self, v):
        if isinstance(v, str):
            v = [a.strip() for a in v.replace("\n", "|").split("|")]
        return [a.strip()[:MAX_ANSWER] for a in (v or []) if a and a.strip()]

    def accepted(self, q):
        """(key, normalized, raw) for the official answer and every alternate."""
        out = []
        for raw in [q["official_answer"]] + list(q["alt_answers"]):
            k = T.key(raw)
            if k:
                out.append((k, T.normalize(raw), raw))
        return out

    def _sync_auto(self, c, q):
        """Re-derive the AUTO grades of one question; MANUAL decisions are never touched."""
        c.execute("DELETE FROM grades WHERE question_id = ? AND source = 'AUTO'", (q["id"],))
        now = time.time()
        for k, _norm, raw in self.accepted(q):
            c.execute(
                "INSERT OR IGNORE INTO grades(question_id, norm, fraction, source, label,"
                " updated_at) VALUES (?,?,1.0,'AUTO',?,?)", (q["id"], k, raw, now))

    def _question_values(self, data, existing=None):
        def pick(field, default=""):
            if field in data:
                return data.get(field)
            return existing[field] if existing else default
        number = pick("number", None)
        if number in (None, ""):
            r = self.db.q1("SELECT COALESCE(MAX(number), 0) AS m FROM questions")
            number = int(r["m"]) + 1
        points = pick("points", None)
        return {
            "number": int(number),
            "text": (pick("text") or "").strip(),
            "images": json.dumps(list(pick("images", []) or [])),
            "answer_images": json.dumps(list(pick("answer_images", []) or [])),
            "official_answer": (pick("official_answer") or "").strip()[:MAX_ANSWER],
            "alt_answers": json.dumps(self._clean_alts(pick("alt_answers", []))),
            "notes": (pick("notes") or "").strip(),
            "points": None if points in (None, "") else _f(points),
        }

    def add_question(self, data):
        v = self._question_values(data)
        if v["number"] in self._q_by_number:
            raise QuizError("Question %d already exists" % v["number"], 409)
        now = time.time()
        def job(c):
            cur = c.execute(
                "INSERT INTO questions(number, text, images, answer_images, official_answer,"
                " alt_answers, notes, points, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (v["number"], v["text"], v["images"], v["answer_images"], v["official_answer"],
                 v["alt_answers"], v["notes"], v["points"], now))
            return cur.lastrowid
        qid = self.db.write(job)
        self.reload_questions()
        self.db.write(lambda c: self._sync_auto(c, self._q_by_id[qid]))
        self.bump("g")
        return self._q_by_id[qid]

    def update_question(self, qid, data):
        q = self._q_by_id.get(qid)
        if not q:
            raise QuizError("No such question", 404)
        v = self._question_values(data, existing=dict(
            q, images=q["images"], answer_images=q["answer_images"], alt_answers=q["alt_answers"]))
        other = self._q_by_number.get(v["number"])
        if other and other["id"] != qid:
            raise QuizError("Question number %d is taken" % v["number"], 409)
        def job(c):
            c.execute(
                "UPDATE questions SET number=?, text=?, images=?, answer_images=?,"
                " official_answer=?, alt_answers=?, notes=?, points=? WHERE id=?",
                (v["number"], v["text"], v["images"], v["answer_images"], v["official_answer"],
                 v["alt_answers"], v["notes"], v["points"], qid))
        self.db.write(job)
        self.reload_questions()
        self.db.write(lambda c: self._sync_auto(c, self._q_by_id[qid]))
        self.bump("g")
        return self._q_by_id[qid]

    def delete_question(self, qid):
        def job(c):
            c.execute("DELETE FROM answers WHERE question_id = ?", (qid,))
            c.execute("DELETE FROM answer_log WHERE question_id = ?", (qid,))
            c.execute("DELETE FROM grades WHERE question_id = ?", (qid,))
            c.execute("DELETE FROM questions WHERE id = ?", (qid,))
        self.db.write(job)
        self.reload_questions()
        self.bump("a", "g")

    # ============================================================== phases
    def phase(self):
        return self.cfg.get("phase") or "LOBBY"

    def current_q(self):
        return self.question_by_number(self.get_num("current_q", 0)) if self.get_num("current_q", 0) else None

    def deadline(self):
        return _f(self.cfg.get("deadline"))

    def _numbers(self):
        return [q["number"] for q in self._questions]

    def set_phase(self, phase):
        phase = (phase or "").upper()
        if phase not in PHASES:
            raise QuizError("Unknown phase %s" % phase, 400)
        nums = self._numbers()
        if phase != "LOBBY" and not nums:
            raise QuizError("Add questions before starting", 400)
        if phase != "LOBBY" and not self._teams:
            raise QuizError("Add teams before starting", 400)
        cur = self.phase()
        now = time.time()
        u = {"phase": phase}
        if phase == "PASS1":
            if not self.get_num("current_q", 0):
                u["current_q"] = nums[0]
            u["max_revealed"] = max(int(self.get_num("max_revealed", 0)), int(u.get("current_q", self.get_num("current_q"))))
            u.update(deadline="", hint_cutoff="", locked_at="")
        elif phase == "PASS2":
            u.update(current_q=nums[0], max_revealed=nums[-1], deadline="",
                     hint_cutoff="", locked_at="")
        elif phase == "PASS3":
            u.update(max_revealed=nums[-1], deadline="", locked_at="")
            if not self.cfg.get("hint_cutoff"):
                u["hint_cutoff"] = now
        elif phase == "LOCKED":
            if cur == "PASS3" and self.deadline() and self.deadline() < now:
                u["locked_at"] = self.deadline()
            else:
                u["locked_at"] = now
            u["deadline"] = ""
            u["max_revealed"] = nums[-1]
        elif phase == "REVEAL":
            u["max_revealed"] = nums[-1]
            if not self.cfg.get("locked_at"):
                u["locked_at"] = now
            if not self.get_num("reveal_q", 0):
                u["reveal_q"] = nums[0]
            if cur != "REVEAL":
                u["reveal_shown"] = 0
            u["deadline"] = ""
        self.set_cfg(u)
        self.db.audit("scorer", "phase", {"from": cur, "to": phase})
        return phase

    def goto(self, number):
        """Show a specific question in pass 1 or 2."""
        if self.phase() not in ("PASS1", "PASS2"):
            raise QuizError("Questions are stepped through in pass 1 and pass 2 only", 409)
        q = self.question_by_number(number)
        if not q:
            raise QuizError("No such question", 404)
        u = {"current_q": q["number"], "deadline": ""}
        if self.phase() == "PASS1":
            u["max_revealed"] = max(int(self.get_num("max_revealed", 0)), q["number"])
        self.set_cfg(u)
        self.db.audit("scorer", "goto", {"q": q["number"]})
        return q

    def step(self, delta):
        nums = self._numbers()
        cur = int(self.get_num("current_q", 0))
        if cur not in nums:
            idx = -1 if delta > 0 else len(nums)
        else:
            idx = nums.index(cur)
        idx += delta
        if idx < 0:
            raise QuizError("This is the first question", 409)
        if idx >= len(nums):
            raise QuizError("This was the last question", 409)
        return self.goto(nums[idx])

    def start_timer(self, seconds):
        if self.phase() not in ("PASS1", "PASS2", "PASS3"):
            raise QuizError("There is nothing to time right now", 409)
        seconds = max(5.0, min(3600.0, float(seconds)))
        self.set_cfg({"deadline": time.time() + seconds})
        self.db.audit("scorer", "timer.start", {"seconds": seconds, "phase": self.phase()})

    def extend_timer(self, seconds):
        d = self.deadline()
        now = time.time()
        if d is None or d < now:
            d = now
        self.set_cfg({"deadline": d + float(seconds)})
        self.db.audit("scorer", "timer.extend", {"seconds": seconds})

    def clear_timer(self):
        self.set_cfg({"deadline": ""})

    def tick(self):
        """Called twice a second: ends Pass 3 when its timer runs out. True if it did."""
        d = self.deadline()
        if self.phase() == "PASS3" and d is not None and time.time() >= d:
            self.set_phase("LOCKED")
            return True
        return False

    def reveal_step(self, delta):
        nums = self._numbers()
        cur = int(self.get_num("reveal_q", 0))
        idx = nums.index(cur) if cur in nums else 0
        idx = max(0, min(len(nums) - 1, idx + delta))
        self.set_cfg({"reveal_q": nums[idx], "reveal_page": idx // 6, "reveal_shown": 0})

    def reveal_limits(self):
        """(highest question whose text is up, highest question whose answer is public)."""
        nums = self._numbers()
        if not nums:
            return 0, 0
        shown = self.get_bool("reveal_shown", False)
        if self.get("reveal_view") == "list":
            start = int(self.get_num("reveal_page", 0)) * 6
            start = max(0, min(start, len(nums) - 1))
            end = min(len(nums), start + 6) - 1
            return nums[end], (nums[end] if shown else (nums[start - 1] if start else 0))
        cur = int(self.get_num("reveal_q", 0))
        idx = nums.index(cur) if cur in nums else 0
        return nums[idx], (nums[idx] if shown else (nums[idx - 1] if idx else 0))

    def scores_upto(self, limit):
        """Standings counting only questions up to `limit`; what phones see mid-reveal."""
        key = (self.ver["s"], limit)
        with self.lock:
            if self._ups_cache[0] == key:
                return self._ups_cache[1]
        dp = self.get_num("default_points", 10.0)
        rows = self.db.q(
            "SELECT t.id, t.number,"
            " COALESCE((SELECT SUM(g.fraction * COALESCE(q.points, ?))"
            "   FROM answers a JOIN questions q ON q.id = a.question_id"
            "   JOIN grades g ON g.question_id = a.question_id AND g.norm = a.norm"
            "   WHERE a.team_id = t.id AND a.norm <> '' AND q.number <= ?), 0) AS s,"
            " COALESCE((SELECT SUM(points) FROM adjustments WHERE team_id = t.id), 0) AS adj"
            " FROM teams t WHERE t.active = 1", (dp, limit))
        nums = self._numbers()
        finished = bool(nums) and limit >= nums[-1]
        for r in rows:
            r["score"] = round(r["s"] + (r["adj"] if finished else 0), 2)
        rows.sort(key=lambda r: (-r["score"], r["number"]))
        rank, prev = 0, None
        for i, r in enumerate(rows):
            if r["score"] != prev:
                rank, prev = i + 1, r["score"]
            r["rank"] = rank
        with self.lock:
            self._ups_cache = (key, rows)
        return rows

    def accepting(self, qnum, ts):
        """May an answer for question `qnum`, typed at server-time `ts`, be stored now?"""
        phase, now = self.phase(), time.time()
        if phase == "PASS1":
            return qnum <= self.get_num("max_revealed", 0)
        if phase == "PASS2":
            return True
        if phase == "PASS3":
            d = self.deadline()
            if d is None:
                return False
            if now <= d:
                return True
            cutoff = d
        elif phase in ("LOCKED", "REVEAL"):
            cutoff = _f(self.cfg.get("locked_at"))
            if cutoff is None:
                return False
        else:
            return False
        return now <= cutoff + GRACE and ts is not None and ts <= cutoff + 0.5

    # ============================================================= answers
    def save_answers(self, team, items):
        """
        Idempotent batch save. Per item: ok | stale | closed | bad.

        `stale` means the server already holds this revision or a newer one --
        the client adopts the server's copy and stops retrying that item.
        """
        now = time.time()
        hint_cutoff = _f(self.cfg.get("hint_cutoff"))
        phase = self.phase()
        results = []
        touched = []

        def job(c):
            for it in items:
                q = self.question_by_number(it.get("q"))
                rev = int(it.get("rev") or 0)
                if not q or rev <= 0:
                    results.append({"q": it.get("q"), "status": "bad"})
                    continue
                ts = _f(it.get("ts"))
                if ts is not None:
                    ts = min(ts, now)
                row = c.execute(
                    "SELECT answer, hint, rev FROM answers WHERE question_id = ? AND team_id = ?",
                    (q["id"], team["id"])).fetchone()
                if row and rev <= row["rev"]:
                    r = {"q": q["number"], "status": "ok" if rev == row["rev"] else "stale",
                         "rev": row["rev"]}
                    if r["status"] == "stale":
                        r.update(answer=row["answer"], hint=row["hint"])
                    results.append(r)
                    continue
                if not self.accepting(q["number"], ts):
                    results.append({"q": q["number"], "status": "closed"})
                    continue
                answer = str(it.get("answer") or "").strip()[:MAX_ANSWER]
                hint = str(it.get("hint") or "").strip()[:MAX_HINT]
                if row and hint_cutoff is not None and (ts is None or ts > hint_cutoff):
                    hint = row["hint"]          # notes are frozen once Pass 3 begins
                elif not row and hint_cutoff is not None and (ts is None or ts > hint_cutoff):
                    hint = ""
                c.execute(
                    "INSERT INTO answers(question_id, team_id, answer, hint, norm, rev, updated_at)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT(question_id, team_id) DO UPDATE SET"
                    " answer=excluded.answer, hint=excluded.hint, norm=excluded.norm,"
                    " rev=excluded.rev, updated_at=excluded.updated_at",
                    (q["id"], team["id"], answer, hint, T.key(answer), rev, now))
                if not row or row["answer"] != answer or row["hint"] != hint:
                    c.execute(
                        "INSERT INTO answer_log(ts, question_id, team_id, phase, rev, answer, hint)"
                        " VALUES (?,?,?,?,?,?,?)", (now, q["id"], team["id"], phase, rev, answer, hint))
                    touched.append((now, team["number"], q["number"], rev, answer, hint))
                results.append({"q": q["number"], "status": "ok", "rev": rev})

        self.db.write(job)
        if touched:
            self.bump("a")
        return results, touched

    def team_answers(self, team_id):
        out = []
        for r in self.db.q("SELECT question_id, answer, hint, rev FROM answers WHERE team_id = ?",
                           (team_id,)):
            q = self._q_by_id.get(r["question_id"])
            if q:
                out.append({"q": q["number"], "answer": r["answer"], "hint": r["hint"],
                            "rev": r["rev"]})
        return out

    # =========================================================== phone views
    def visible_questions(self):
        """Questions a phone may know about right now (everything is revealed by pass 3)."""
        hi = self.get_num("max_revealed", 0)
        return [q for q in self._questions if q["number"] <= hi]

    def phone_question(self, q):
        """Public form of a question. Never carries the answer."""
        out = {"n": q["number"]}
        if self.get_bool("show_question_on_phone", True):
            out["text"] = q["text"]
        if self.get_bool("send_media_to_phones", False):
            out["images"] = q["images"]
        return out

    def question_blob(self):
        """Cached JSON of the phone-visible question list: (etag, bytes)."""
        key = (self.ver["q"], int(self.get_num("max_revealed", 0)),
               self.get_bool("show_question_on_phone", True),
               self.get_bool("send_media_to_phones", False))
        with self.lock:
            hit = self._blob_cache.get(key)
        if hit:
            return hit
        body = json.dumps({"ok": True, "questions": [self.phone_question(q)
                                                     for q in self.visible_questions()]},
                          separators=(",", ":")).encode("utf-8")
        etag = '"q%d-%d-%d%d"' % (key[0], key[1], key[2], key[3])
        with self.lock:
            self._blob_cache = {key: (etag, body)}
        return etag, body

    def reveal_items(self, team=None):
        """Questions up on the reveal; answers and marks only for those already shown."""
        publish = self.get_bool("results_to_phones", False)
        limit, shown_upto = self.reveal_limits()
        mine = {}
        grades = {}
        if team:
            for r in self.db.q("SELECT question_id, answer, norm FROM answers WHERE team_id = ?",
                               (team["id"],)):
                mine[r["question_id"]] = r
            for g in self.db.q("SELECT question_id, norm, fraction FROM grades"):
                grades[(g["question_id"], g["norm"])] = g["fraction"]
        items = []
        for q in self._questions:
            if q["number"] > limit:
                break
            it = {"n": q["number"], "text": q["text"]}
            if q["number"] <= shown_upto:
                it["answer"] = q["official_answer"]
                it["images"] = q["answer_images"] if self.get_bool("send_media_to_phones") else []
            if team:
                a = mine.get(q["id"])
                it["mine"] = a["answer"] if a else ""
                if publish and q["number"] <= shown_upto:
                    fr = grades.get((q["id"], a["norm"]), 0.0) if a and a["norm"] else 0.0
                    it["points"] = round(fr * self.points_for(q), 2)
            items.append(it)
        return items

    # ================================================================ review
    def _grades_for(self, qid):
        return {g["norm"]: g for g in self.db.q(
            "SELECT norm, fraction, source, label FROM grades WHERE question_id = ?", (qid,))}

    def review(self, number):
        q = self.question_by_number(number)
        if not q:
            raise QuizError("No such question", 404)
        grades = self._grades_for(q["id"])
        accepted = self.accepted(q)
        clusters = {}
        answered_ids = set()
        for r in self.db.q("SELECT team_id, answer, norm FROM answers"
                           " WHERE question_id = ? AND norm <> ''", (q["id"],)):
            t = self._by_id.get(r["team_id"])
            if not t or not t["active"]:
                continue
            answered_ids.add(r["team_id"])
            cl = clusters.setdefault(r["norm"], {"norm": r["norm"], "variants": {}, "teams": []})
            cl["variants"][r["answer"]] = cl["variants"].get(r["answer"], 0) + 1
            cl["teams"].append(t["number"])
        out = []
        unrev_clusters = unrev_answers = 0
        for norm, cl in clusters.items():
            variants = sorted(cl["variants"].items(), key=lambda kv: -kv[1])
            g = grades.get(norm)
            item = {"norm": norm, "label": variants[0][0], "variants": variants,
                    "count": len(cl["teams"]), "teams": sorted(cl["teams"]),
                    "fraction": g["fraction"] if g else None,
                    "source": g["source"] if g else None, "suggest": None}
            if not g:
                unrev_clusters += 1
                unrev_answers += item["count"]
                item["suggest"] = T.suggest(norm, T.normalize(variants[0][0]), accepted)
            out.append(item)
        def order(c):
            if c["fraction"] is None:
                return (0, -(c["suggest"]["score"] if c["suggest"] else 0), -c["count"], c["label"])
            return (1 if c["source"] == "MANUAL" else 2, 0, -c["count"], c["label"])
        out.sort(key=order)
        silent = [t["number"] for t in self._teams if t["id"] not in answered_ids]
        return {
            "question": {"number": q["number"], "text": q["text"], "images": q["images"],
                         "official_answer": q["official_answer"],
                         "alt_answers": q["alt_answers"], "points": self.points_for(q)},
            "clusters": out, "silent": silent,
            "stats": {"answers": sum(c["count"] for c in out), "clusters": len(out),
                      "unreviewed_clusters": unrev_clusters,
                      "unreviewed_answers": unrev_answers},
        }

    def review_summary(self):
        return self.db.q(
            "SELECT q.number AS number,"
            " COUNT(a.team_id) AS answers,"
            " COUNT(DISTINCT CASE WHEN g.norm IS NULL THEN a.norm END) AS unreviewed_clusters,"
            " COALESCE(SUM(CASE WHEN a.team_id IS NOT NULL AND g.norm IS NULL THEN 1 ELSE 0 END), 0)"
            " AS unreviewed_answers"
            " FROM questions q"
            " LEFT JOIN answers a ON a.question_id = q.id AND a.norm <> ''"
            " AND a.team_id IN (SELECT id FROM teams WHERE active = 1)"
            " LEFT JOIN grades g ON g.question_id = q.id AND g.norm = a.norm"
            " GROUP BY q.id ORDER BY q.number")

    def set_grade(self, number, norm, fraction, label=""):
        """Decide one answer for every team that wrote it. fraction=None removes the decision."""
        q = self.question_by_number(number)
        if not q:
            raise QuizError("No such question", 404)
        if not norm:
            raise QuizError("Nothing to grade", 400)
        if fraction is not None:
            fraction = _f(fraction)
            if fraction is None or not -1.0 <= fraction <= 1.0:
                raise QuizError("fraction must be between -1 and 1", 400)
        now = time.time()
        def job(c):
            if fraction is None:
                c.execute("DELETE FROM grades WHERE question_id=? AND norm=? AND source='MANUAL'",
                          (q["id"], norm))
                self._sync_auto(c, q)
            else:
                c.execute(
                    "INSERT INTO grades(question_id, norm, fraction, source, label, updated_at)"
                    " VALUES (?,?,?,'MANUAL',?,?) ON CONFLICT(question_id, norm) DO UPDATE SET"
                    " fraction=excluded.fraction, source='MANUAL', label=excluded.label,"
                    " updated_at=excluded.updated_at",
                    (q["id"], norm, fraction, (label or "")[:MAX_ANSWER], now))
        self.db.write(job)
        self.bump("g")
        self.db.audit("scorer", "grade", {"q": number, "norm": norm, "fraction": fraction,
                                          "label": label})

    def mark_rest_wrong(self, number=None):
        """Every still-unreviewed answer (one question, or all) becomes WRONG."""
        nums = [number] if number else self._numbers()
        now = time.time()
        count = [0]
        def job(c):
            for n in nums:
                q = self.question_by_number(n)
                if not q:
                    continue
                rows = c.execute(
                    "SELECT a.norm, MIN(a.answer) AS label FROM answers a"
                    " LEFT JOIN grades g ON g.question_id = a.question_id AND g.norm = a.norm"
                    " WHERE a.question_id = ? AND a.norm <> '' AND g.norm IS NULL GROUP BY a.norm",
                    (q["id"],)).fetchall()
                for r in rows:
                    c.execute("INSERT OR IGNORE INTO grades(question_id, norm, fraction, source,"
                              " label, updated_at) VALUES (?,?,0.0,'MANUAL',?,?)",
                              (q["id"], r["norm"], r["label"], now))
                    count[0] += 1
        self.db.write(job)
        self.bump("g")
        self.db.audit("scorer", "grade.rest_wrong", {"q": number, "clusters": count[0]})
        return count[0]

    def adjust(self, team_id, points, note=""):
        t = self._by_id.get(team_id)
        if not t:
            raise QuizError("No such team", 404)
        pts, now = float(points), time.time()
        self.db.write(lambda c: c.execute(
            "INSERT INTO adjustments(team_id, points, note, ts) VALUES (?,?,?,?)",
            (team_id, pts, (note or "")[:200], now)))
        self.bump("g")
        self.db.audit("scorer", "adjust", {"team": t["number"], "points": pts, "note": note})

    # ============================================================== scoring
    def scoreboard(self):
        ver = self.ver["s"]
        with self.lock:
            if self._sb_cache[0] == ver:
                return self._sb_cache[1]
        dp = self.get_num("default_points", 10.0)
        rows = self.db.q(
            "SELECT t.id, t.number, t.name,"
            " COALESCE((SELECT SUM(g.fraction * COALESCE(q.points, ?))"
            "   FROM answers a JOIN questions q ON q.id = a.question_id"
            "   JOIN grades g ON g.question_id = a.question_id AND g.norm = a.norm"
            "   WHERE a.team_id = t.id AND a.norm <> ''), 0) AS qscore,"
            " COALESCE((SELECT SUM(points) FROM adjustments WHERE team_id = t.id), 0) AS adj,"
            " COALESCE((SELECT COUNT(*) FROM answers a"
            "   JOIN grades g ON g.question_id = a.question_id AND g.norm = a.norm"
            "   WHERE a.team_id = t.id AND a.norm <> '' AND g.fraction >= 1.0), 0) AS correct,"
            " COALESCE((SELECT COUNT(*) FROM answers a WHERE a.team_id = t.id AND a.norm <> ''), 0)"
            " AS answered"
            " FROM teams t WHERE t.active = 1", (dp,))
        for r in rows:
            r["score"] = round(r["qscore"] + r["adj"], 2)
        rows.sort(key=lambda r: (-r["score"], -r["correct"], r["number"]))
        rank, prev = 0, None
        for i, r in enumerate(rows):
            if prev is None or (r["score"], r["correct"]) != prev:
                rank, prev = i + 1, (r["score"], r["correct"])
            r["rank"] = rank
        with self.lock:
            self._sb_cache = (ver, rows)
        return rows

    def team_sheet(self, team_id):
        """Everything one team wrote, per question, with what it scored."""
        t = self._by_id.get(team_id)
        if not t:
            raise QuizError("No such team", 404)
        grades = {(g["question_id"], g["norm"]): g["fraction"]
                  for g in self.db.q("SELECT question_id, norm, fraction FROM grades")}
        mine = {r["question_id"]: r for r in self.db.q(
            "SELECT * FROM answers WHERE team_id = ?", (team_id,))}
        rows = []
        for q in self._questions:
            a = mine.get(q["id"])
            fr = grades.get((q["id"], a["norm"])) if a and a["norm"] else None
            rows.append({"q": q["number"], "answer": a["answer"] if a else "",
                         "hint": a["hint"] if a else "",
                         "official": q["official_answer"],
                         "fraction": fr,
                         "points": round((fr or 0) * self.points_for(q), 2)})
        history = self.db.q(
            "SELECT l.ts, l.phase, l.rev, l.answer, l.hint, q.number AS q FROM answer_log l"
            " JOIN questions q ON q.id = l.question_id WHERE l.team_id = ? ORDER BY l.id",
            (team_id,))
        return {"team": {"number": t["number"], "name": t["name"]}, "rows": rows,
                "history": history}

    # ============================================================ live stats
    def answered_counts(self):
        """(answered-per-team totals, set of team ids that answered the current question)."""
        totals = {r["team_id"]: r["n"] for r in self.db.q(
            "SELECT team_id, COUNT(*) AS n FROM answers WHERE norm <> '' GROUP BY team_id")}
        cur = self.current_q()
        here = set()
        if cur:
            here = {r["team_id"] for r in self.db.q(
                "SELECT team_id FROM answers WHERE question_id = ? AND norm <> ''", (cur["id"],))}
        return totals, here

    # =============================================================== resets
    def quiz_stats(self):
        q1 = self.db.q1
        return {
            "teams": q1("SELECT COUNT(*) AS n FROM teams")["n"],
            "teams_joined": q1("SELECT COUNT(*) AS n FROM teams WHERE joined_at IS NOT NULL")["n"],
            "questions": q1("SELECT COUNT(*) AS n FROM questions")["n"],
            "answers": q1("SELECT COUNT(*) AS n FROM answers")["n"],
            "phase": self.phase(),
        }

    def clear_answers(self):
        """Back to LOBBY with no answers or decisions; teams and questions stay."""
        def job(c):
            c.execute("DELETE FROM answers")
            c.execute("DELETE FROM answer_log")
            c.execute("DELETE FROM grades")
            c.execute("DELETE FROM adjustments")
            for q in self._questions:
                self._sync_auto(c, q)
        self.db.write(job)
        self.set_cfg({"phase": "LOBBY", "current_q": 0, "max_revealed": 0, "deadline": "",
                      "locked_at": "", "hint_cutoff": "", "reveal_q": 0, "reveal_page": 0, "reveal_shown": 0})
        self.bump("a", "g")
        self.db.audit("scorer", "quiz.clear", {})

    def master_reset(self, drop_questions=True):
        stats = self.quiz_stats()
        def job(c):
            for t in ("answers", "answer_log", "grades", "adjustments", "teams", "audit_log"):
                c.execute("DELETE FROM %s" % t)
            if drop_questions:
                c.execute("DELETE FROM questions")
            c.execute("DELETE FROM sqlite_sequence")
            if not drop_questions:
                for q in self._questions:
                    self._sync_auto(c, q)
        self.db.write(job)
        self.set_cfg({"phase": "LOBBY", "current_q": 0, "max_revealed": 0, "deadline": "",
                      "locked_at": "", "hint_cutoff": "", "reveal_q": 0, "reveal_page": 0, "reveal_shown": 0})
        self.reload()
        self.bump("a", "g")
        self.db.audit("scorer", "quiz.master_reset", dict(stats, dropped_questions=drop_questions))
        return stats
