"""
Continuous CSV safety net.

The database is durable, but a .db file is no use at a venue if the laptop
itself is the problem. So:

  journal.csv     append-only: one line per accepted answer change, written
                  right after the commit and fsync'd about once a second. An
                  append-only file can only ever lose its last line.
  scoreboard.csv  \
  answers.csv      > rewritten (atomically) at most every few seconds while
  matrix.csv      /  anything has changed -- NOT once per action, which would
  teams.csv          be hundreds of file rewrites while 100 teams type.

Failures here are swallowed: a full disk or a pulled USB stick must never take
the live quiz down.
"""

import csv
import io
import os
import threading
import time
import traceback


def _csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    for r in rows:
        w.writerow(r)
    return out.getvalue()


def _stamp(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# --------------------------------------------------------------- builders
def scoreboard_rows(eng):
    yield ["# quiz", eng.get("quiz_name"), "# saved", _stamp()]
    yield ["Rank", "Team No", "Team Name", "Score", "Answers Given", "Fully Correct"]
    for r in eng.scoreboard():
        yield [r["rank"], r["number"], r["name"], r["score"], r["answered"], r["correct"]]


def _grade_map(eng):
    return {(g["question_id"], g["norm"]): (g["fraction"], g["source"]) for g in eng.db.q(
        "SELECT question_id, norm, fraction, source FROM grades")}


def answers_rows(eng):
    yield ["# quiz", eng.get("quiz_name"), "# saved", _stamp()]
    yield ["Q No", "Question", "Official Answer", "Team No", "Team Name", "Answer", "Hint",
           "Credit", "Points", "Graded By"]
    grades = _grade_map(eng)
    rows = eng.db.q("SELECT question_id, team_id, answer, hint, norm FROM answers")
    by_q = {}
    for r in rows:
        by_q.setdefault(r["question_id"], []).append(r)
    for q in eng.questions():
        pts = eng.points_for(q)
        for r in sorted(by_q.get(q["id"], []), key=lambda r: eng._by_id.get(r["team_id"], {}).get("number", 0)):
            t = eng._by_id.get(r["team_id"])
            if not t:
                continue
            fr, src = grades.get((q["id"], r["norm"]), (None, "")) if r["norm"] else (None, "")
            yield [q["number"], q["text"], q["official_answer"], t["number"], t["name"],
                   r["answer"], r["hint"], "" if fr is None else fr,
                   round((fr or 0) * pts, 2), src or ("" if not r["norm"] else "UNREVIEWED")]


def matrix_rows(eng):
    qs = eng.questions()
    grades = _grade_map(eng)
    mine = {}
    for r in eng.db.q("SELECT question_id, team_id, norm FROM answers WHERE norm <> ''"):
        mine[(r["team_id"], r["question_id"])] = r["norm"]
    yield ["# quiz", eng.get("quiz_name"), "# saved", _stamp()]
    yield ["Team No", "Team Name"] + ["Q%d" % q["number"] for q in qs] + ["Adjust", "TOTAL"]
    adj = {r["team_id"]: r["s"] for r in eng.db.q(
        "SELECT team_id, SUM(points) AS s FROM adjustments GROUP BY team_id")}
    for t in eng.teams():
        cells, total = [], 0.0
        for q in qs:
            norm = mine.get((t["id"], q["id"]))
            fr = grades.get((q["id"], norm), (None, ""))[0] if norm else None
            p = round((fr or 0) * eng.points_for(q), 2)
            total += p
            cells.append(p if norm else "")
        a = adj.get(t["id"], 0) or 0
        yield [t["number"], t["name"]] + cells + [a, round(total + a, 2)]


def teams_rows(eng):
    yield ["# quiz", eng.get("quiz_name"), "# saved", _stamp()]
    yield ["Team No", "Team Name", "PIN", "Joined", "Active"]
    for t in eng.teams(active_only=False):
        yield [t["number"], t["name"], t["pin"], "YES" if t["joined_at"] else "",
               "YES" if t["active"] else "NO"]


def questions_rows(eng):
    yield ["Q No", "Question", "Official Answer", "Also Accepted", "Points", "Notes"]
    for q in eng.questions():
        yield [q["number"], q["text"], q["official_answer"], "|".join(q["alt_answers"]),
               "" if q["points"] is None else q["points"], q["notes"]]


class Backup(object):
    def __init__(self, eng, root, interval=3.0):
        self.eng = eng
        self.dir = os.path.join(root, "backups")
        self.interval = interval
        self.writes = 0
        self.last_error = None
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._jlock = threading.Lock()
        self._jfile = None
        self._jsync = 0.0

    def status(self):
        return {"writes": self.writes, "last_error": self.last_error}

    def _ensure(self):
        os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def _atomic_write(path, text):
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ---------------------------------------------------------- journal
    def journal(self, entries):
        """entries: (ts, team_no, q_no, rev, answer, hint) tuples."""
        try:
            with self._jlock:
                if self._jfile is None:
                    self._ensure()
                    path = os.path.join(self.dir, "journal.csv")
                    new = not os.path.exists(path)
                    self._jfile = open(path, "a", newline="", encoding="utf-8")
                    if new:
                        csv.writer(self._jfile).writerow(
                            ["time", "team", "question", "rev", "answer", "hint"])
                w = csv.writer(self._jfile)
                for ts, team, q, rev, answer, hint in entries:
                    w.writerow([_stamp(ts), team, q, rev, answer, hint])
                self._jfile.flush()
                if time.time() - self._jsync > 1.0:
                    os.fsync(self._jfile.fileno())
                    self._jsync = time.time()
        except Exception:
            self.last_error = traceback.format_exc(limit=2)

    def event(self, ts, actor, action, detail):
        try:
            with self._jlock:
                self._ensure()
                path = os.path.join(self.dir, "event-log.csv")
                new = not os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["time", "actor", "action", "detail"])
                    w.writerow([_stamp(ts), actor, action, detail])
        except Exception:
            self.last_error = traceback.format_exc(limit=2)

    # ------------------------------------------------------- snapshots
    def mark_dirty(self):
        self._dirty.set()

    def write_all(self, directory=None):
        d = directory or self.dir
        try:
            os.makedirs(d, exist_ok=True)
            for name, rows in (("scoreboard.csv", scoreboard_rows), ("answers.csv", answers_rows),
                               ("matrix.csv", matrix_rows), ("teams.csv", teams_rows),
                               ("questions.csv", questions_rows)):
                self._atomic_write(os.path.join(d, name), _csv(rows(self.eng)))
            self.writes += 1
            self.last_error = None
            return True
        except Exception:
            self.last_error = traceback.format_exc(limit=2)
            return False

    def archive_now(self, label):
        d = os.path.join(self.dir, "archive", "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), label))
        return d if self.write_all(d) else None

    def start(self):
        def run():
            while not self._stop.is_set():
                self._dirty.wait()
                if self._stop.is_set():
                    return
                time.sleep(self.interval)       # coalesce a burst into one rewrite
                self._dirty.clear()
                self.write_all()
        threading.Thread(target=run, name="backup", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._dirty.set()
        with self._jlock:
            if self._jfile is not None:
                try:
                    self._jfile.close()
                except OSError:
                    pass
                self._jfile = None
