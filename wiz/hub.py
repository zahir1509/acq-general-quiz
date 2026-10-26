"""
Server-Sent Events fan-out.

Phones all receive the SAME frame, so each frame is serialised and chunk-framed
exactly once and the identical bytes object is handed to every subscriber --
publishing to 100 phones costs 100 queue puts, not 100 JSON encodes. Anything
specific to one team (its own answers) never rides the stream; phones fetch it
with ordinary requests, so a dropped stream can never lose an answer.

Each subscriber has a tiny bounded queue; when a slow phone falls behind we
drop its OLDEST frame. Frames are complete state, so the newest one is always
enough to catch up.
"""

import json
import queue
import threading
import time


def chunk(payload):
    return b"%x\r\n%s\r\n" % (len(payload), payload)


def frame_bytes(kind, raw):
    return chunk(b"event: " + kind + b"\ndata: " + raw + b"\n\n")


class Sub(object):
    __slots__ = ("role", "team_id", "q")

    def __init__(self, role, team_id):
        self.role = role
        self.team_id = team_id
        self.q = queue.Queue(maxsize=6)

    def push(self, data):
        try:
            self.q.put_nowait(data)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(data)
            except queue.Full:
                pass


class Hub(object):
    ROLES = ("team", "screen", "board", "scorer")

    def __init__(self, max_subs=600):
        self._subs = []
        self._lock = threading.Lock()
        self.max_subs = max_subs
        self.seq = 0
        self.latest = {}        # role -> (seq, json bytes, sse chunk)

    def subscribe(self, role, team_id=None):
        sub = Sub(role, team_id)
        with self._lock:
            if len(self._subs) >= self.max_subs:
                return None
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def count(self, role):
        with self._lock:
            return sum(1 for s in self._subs if s.role == role)

    def counts(self):
        with self._lock:
            out = {r: 0 for r in self.ROLES}
            for s in self._subs:
                out[s.role] = out.get(s.role, 0) + 1
            return out

    def online_team_ids(self):
        with self._lock:
            return {s.team_id for s in self._subs if s.role == "team"}

    def publish(self, role, frame):
        with self._lock:
            self.seq += 1
            seq = self.seq
            subs = [s for s in self._subs if s.role == role]
        frame["seq"] = seq
        raw = json.dumps(frame, separators=(",", ":"), default=str).encode("utf-8")
        data = frame_bytes(b"state", raw)
        self.latest[role] = (seq, raw, data)
        for s in subs:
            s.push(data)

    def ping(self):
        raw = json.dumps({"type": "ping", "t": time.time()}).encode()
        data = frame_bytes(b"ping", raw)
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.push(data)
