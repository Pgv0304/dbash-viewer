"""Load testing for the synthetic demo databases.

A real load generator (app/ora/loadgen.py, app/pg/loadgen.py) connects to the database and runs scenarios
in their own sessions. The demo databases have no connection, so this one adds the sessions of those
scenarios straight into the demo collector's samples: the chart, Top SQL and Top Sessions show the load
going through its phases the same way they would on a real database.
"""
from __future__ import annotations

import random
import time

PHASE_SECONDS = 90
PAUSE_SECONDS = 30

# kind -> [(phase name, sql_id, operation, sql text, [(wait class, event, weight)])]
SCENARIOS = {
    "oracle": [
        ("массовая вставка", "lg0ins7a2b1c9", "INSERT",
         "INSERT INTO lg_data (id, grp, payload, created) SELECT lg_seq.NEXTVAL, :1, RPAD('x', 400, 'x'), SYSDATE FROM dual CONNECT BY LEVEL <= 500",
         [("CPU", "ON CPU", 3), ("Commit", "log file sync", 4), ("Configuration", "log buffer space", 1)]),
        ("полные сканирования", "lg0scn5d8e3f1", "SELECT",
         "SELECT /*+ FULL(d) */ grp, COUNT(*), SUM(LENGTH(payload)) FROM lg_data d GROUP BY grp",
         [("User I/O", "direct path read", 6), ("CPU", "ON CPU", 2)]),
        ("горячие строки", "lg0hot2k4m6n8", "UPDATE",
         "UPDATE lg_hot SET counter = counter + 1 WHERE id = :1",
         [("Application", "enq: TX - row lock contention", 6), ("Concurrency", "buffer busy waits", 2), ("CPU", "ON CPU", 1)]),
        ("разбор без связанных переменных", "lg0prs9q1r3s5", "SELECT",
         "SELECT payload FROM lg_data WHERE id = 1234567",
         [("CPU", "ON CPU", 5), ("Concurrency", "library cache: mutex X", 2), ("Concurrency", "cursor: pin S wait on X", 1)]),
    ],
    "postgres": [
        ("массовая вставка", "f0a1b2c3d4e5ld01", "INSERT",
         "INSERT INTO pgash_load.data (grp, payload, created) SELECT $1, repeat('x', 400), now() FROM generate_series(1, 500)",
         [("CPU", "ON CPU", 3), ("IO", "WALSync", 4), ("LWLock", "WALWrite", 1)]),
        ("полные сканирования", "f0a1b2c3d4e5ld02", "SELECT",
         "SELECT grp, count(*), sum(length(payload)) FROM pgash_load.data GROUP BY grp",
         [("IO", "DataFileRead", 6), ("CPU", "ON CPU", 2)]),
        ("горячие строки", "f0a1b2c3d4e5ld03", "UPDATE",
         "UPDATE pgash_load.hot SET counter = counter + 1 WHERE id = $1",
         [("Lock", "transactionid", 6), ("LWLock", "BufferContent", 2), ("CPU", "ON CPU", 1)]),
        ("разбор без связанных переменных", "f0a1b2c3d4e5ld04", "SELECT",
         "SELECT payload FROM pgash_load.data WHERE id = 1234567",
         [("CPU", "ON CPU", 5), ("LWLock", "lock_manager", 1)]),
    ],
}
USER = {"oracle": "DEMO_LOAD", "postgres": "pgash_load"}
MODULE = {"oracle": "ASHV demo load", "postgres": "shop"}
BLOCKER = {"oracle": 977, "postgres": 29977}


class DemoLoad:
    """Same interface as the real LoadGenerator for main.py and the UI: start/stop/describe, status, phase,
    cfg["user"], max_sessions."""

    def __init__(self, collector, kind: str, max_sessions: int = 4):
        self.collector = collector
        self.kind = kind
        self.cfg = {"user": USER[kind]}
        self.password = None
        self.max_sessions = max_sessions
        self.status = "stopped"
        self.phase = None
        self.error = None
        self.started = None
        self.rng = random.Random(7)

    def start(self):
        self.started = time.time()
        self.status = "running"
        self.collector.demo_load = self
        texts = [(s, t, op) for _, s, op, t, _ in SCENARIOS[self.kind]]
        from . import store
        store.save_sqltext(self.collector.name_, texts)

    def stop(self):
        self.status = "stopped"
        self.phase = None
        if getattr(self.collector, "demo_load", None) is self:
            self.collector.demo_load = None

    def describe(self):
        self._phase_at(time.time())
        return {"status": self.status, "phase": self.phase, "error": self.error}

    def _phase_at(self, ts: float):
        """Current scenario, or None during the pause between scenarios."""
        if self.status != "running":
            return None
        cycle = PHASE_SECONDS + PAUSE_SECONDS
        pos = int(ts - self.started)
        n = (pos // cycle) % len(SCENARIOS[self.kind])
        if pos % cycle >= PHASE_SECONDS:
            self.phase = "pause"
            return None
        sc = SCENARIOS[self.kind][n]
        self.phase = sc[0]
        return sc

    def rows(self, ts: int, target: str) -> list:
        """Samples of the load sessions at second ts, added to the demo collector's own samples."""
        sc = self._phase_at(ts)
        if sc is None:
            return []
        _, sql_id, op, _, prof = sc
        out = []
        for i in range(self.max_sessions):
            if self.rng.random() < 0.15:          # a session is briefly idle between calls
                continue
            wc, ev, _ = self.rng.choices(prof, weights=[p[2] for p in prof])[0]
            blocked = wc in ("Application", "Lock") and i > 0
            sid = BLOCKER[self.kind] + i
            out.append({"target": target, "ts": ts, "inst_id": 1, "sid": sid, "serial": 7000 + i,
                        "username": USER[self.kind], "program": "dbash load" if self.kind == "oracle" else "dbash-load",
                        "module": MODULE[self.kind], "machine": "load-gen", "sql_id": sql_id, "sql_opname": op,
                        "plan_hash": None, "event": ev, "wait_class": wc,
                        "blocking_inst": 1 if blocked else None, "blocking_sid": BLOCKER[self.kind] if blocked else None,
                        "session_type": "USER"})
        return out
