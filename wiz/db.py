"""
SQLite storage.

Concurrency model
-----------------
* ONE writer thread owns the only write connection. Every mutation is a job
  queued to it; jobs that arrive together are committed together (group
  commit), so 100 phones saving in the same second cost a handful of fsyncs,
  not 100, and there is never a "database is locked" error.
* A request is only acknowledged AFTER its transaction is durable
  (synchronous=FULL), so an ack to a phone means the answer is on disk.
* Readers use their own per-thread connections (WAL: readers never block the
  writer and the writer never blocks readers).
"""

import json
import os
import queue
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     INTEGER NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    token      TEXT    NOT NULL UNIQUE,
    pin        TEXT    NOT NULL,
    device_id  TEXT,
    joined_at  REAL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    number            INTEGER NOT NULL UNIQUE,
    text              TEXT    NOT NULL DEFAULT '',
    images            TEXT    NOT NULL DEFAULT '[]',   -- JSON: images, audio, video
    answer_images     TEXT    NOT NULL DEFAULT '[]',
    official_answer   TEXT    NOT NULL DEFAULT '',
    alt_answers       TEXT    NOT NULL DEFAULT '[]',   -- JSON list
    notes             TEXT    NOT NULL DEFAULT '',     -- quizmaster-only
    points            REAL,                            -- NULL = quiz default
    created_at        REAL    NOT NULL
);

-- One row per team per question: its CURRENT (= final) answer and its private
-- notes. `rev` is a client-side revision, so retries and out-of-order
-- delivery can never overwrite a newer edit with an older one.
CREATE TABLE IF NOT EXISTS answers (
    question_id INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    answer      TEXT    NOT NULL DEFAULT '',
    hint        TEXT    NOT NULL DEFAULT '',
    norm        TEXT    NOT NULL DEFAULT '',
    rev         INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (question_id, team_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_answers_qn ON answers(question_id, norm);

-- Append-only history of every accepted change: settles any "I typed it" dispute.
CREATE TABLE IF NOT EXISTS answer_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    question_id INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    phase       TEXT NOT NULL,
    rev         INTEGER NOT NULL,
    answer      TEXT NOT NULL,
    hint        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alog_team ON answer_log(team_id, question_id);

-- A decision is about an ANSWER, not a team: every team that wrote the same
-- thing is scored by the same row. AUTO rows are the official answer and its
-- alternates; MANUAL rows are the reviewer's calls.
CREATE TABLE IF NOT EXISTS grades (
    question_id INTEGER NOT NULL,
    norm        TEXT    NOT NULL,
    fraction    REAL    NOT NULL,        -- 0 wrong .. 1 full credit
    source      TEXT    NOT NULL,        -- AUTO | MANUAL
    label       TEXT    NOT NULL DEFAULT '',
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (question_id, norm)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS adjustments (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    points  REAL    NOT NULL,
    note    TEXT    NOT NULL DEFAULT '',
    ts      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""

DEFAULT_CONFIG = {
    "quiz_name": "IT Wiz Quiz",
    "admin_pin": "pyaar",
    "default_points": "10",
    # --- phone display ---
    # Off = phones show only the question number and the answer boxes.
    "show_question_on_phone": "1",
    # Images / audio / video are shown on the projector; this beams them to phones too.
    "send_media_to_phones": "0",
    "results_to_phones": "0",
    "strict_single_device": "1",
    # --- timing ---
    "pass3_seconds": "120",
    "q_timer_seconds": "30",
    # --- join screen ---
    "wifi_ssid": "",
    "wifi_password": "",
    # --- runtime state (persisted, so a restart resumes exactly where it was) ---
    "phase": "LOBBY",          # LOBBY PASS1 PASS2 PASS3 LOCKED REVEAL
    "current_q": "0",          # question number on screen in pass 1 / 2
    "max_revealed": "0",       # highest question number shown so far in pass 1
    "deadline": "",            # epoch seconds; Pass 3 enforces it, passes 1/2 only display it
    "locked_at": "",
    "hint_cutoff": "",         # hints stop being accepted after this (Pass 3 start)
    "reveal_q": "0",
    "reveal_view": "single",   # single | list
    "reveal_page": "0",
    "reveal_shown": "0",      # 1 once the quizmaster presses SHOW ANSWER for the current reveal step
}


class _Job(object):
    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error = None


class Database(object):
    MAX_BATCH = 64

    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self._local = threading.local()
        self._queue = queue.Queue()
        self.stats = {"commits": 0, "jobs": 0, "max_batch": 0}
        self.on_audit = None  # called after an audit row is written
        self._wconn = self._open(readonly=False)
        self._wconn.executescript(SCHEMA)
        for k, v in DEFAULT_CONFIG.items():
            self._wconn.execute(
                "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (k, v))
        self._writer = threading.Thread(target=self._write_loop, name="db-writer", daemon=True)
        self._writer.start()

    # -------------------------------------------------------------- plumbing
    def _open(self, readonly):
        c = sqlite3.connect(self.path, timeout=30.0, isolation_level=None,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        # FULL + group commit: an ack means durable, and the cost is shared.
        c.execute("PRAGMA synchronous=%s" % ("NORMAL" if readonly else "FULL"))
        c.execute("PRAGMA foreign_keys=OFF")
        if readonly:
            c.execute("PRAGMA query_only=ON")
        return c

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._open(readonly=True)
        return c

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def q1(self, sql, args=()):
        r = self.conn().execute(sql, args).fetchone()
        return dict(r) if r else None

    # ----------------------------------------------------------------- write
    def write(self, fn, timeout=30.0):
        """Run fn(conn) on the writer thread inside a transaction; return its result."""
        if threading.current_thread() is self._writer:
            return fn(self._wconn)
        job = _Job(fn)
        self._queue.put(job)
        if not job.done.wait(timeout):
            raise RuntimeError("database write timed out")
        if job.error is not None:
            raise job.error
        return job.result

    def _write_loop(self):
        c = self._wconn
        while True:
            batch = [self._queue.get()]
            while len(batch) < self.MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            committed = []
            try:
                c.execute("BEGIN IMMEDIATE")
                for job in batch:
                    c.execute("SAVEPOINT job")
                    try:
                        job.result = job.fn(c)
                        c.execute("RELEASE job")
                        committed.append(job)
                    except Exception as e:  # this job fails alone; the rest still commit
                        job.error = e
                        c.execute("ROLLBACK TO job")
                        c.execute("RELEASE job")
                c.execute("COMMIT")
            except Exception as e:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                for job in committed:
                    job.error = e
            self.stats["commits"] += 1
            self.stats["jobs"] += len(batch)
            self.stats["max_batch"] = max(self.stats["max_batch"], len(batch))
            for job in batch:
                job.done.set()

    # ---------------------------------------------------------------- config
    def config(self):
        return {r["key"]: r["value"] for r in self.q("SELECT key, value FROM config")}

    def set_config(self, values):
        def job(c):
            for k, v in values.items():
                c.execute(
                    "INSERT INTO config(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, "" if v is None else str(v)))
        self.write(job)

    # ----------------------------------------------------------------- audit
    def audit(self, actor, action, detail=None):
        blob = json.dumps(detail or {}, default=str)
        ts = time.time()
        self.write(lambda c: c.execute(
            "INSERT INTO audit_log(ts, actor, action, detail) VALUES (?,?,?,?)",
            (ts, actor, action, blob)))
        if self.on_audit:
            try:
                self.on_audit(ts, actor, action, blob)
            except Exception:
                pass  # a backup must never break the live quiz
