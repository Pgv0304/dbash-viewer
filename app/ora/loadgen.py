"""Demo-mode load generator: keeps a small, random, cyclic workload running on the monitored
Oracle database so that the ASH charts always have something to show.

Everything happens in a dedicated schema (default ASHV_LOAD) with three small tables, so real
application data is never touched. The workload is deliberately modest (see max_sessions) because
the demo database also serves real applications.

Scenarios (each runs in its own database session, MODULE = 'ASHV demo load', ACTION = scenario):
  cpu_burn     PL/SQL arithmetic loop                         -> ON CPU
  full_scan    full scans with serial direct reads             -> User I/O (direct path read) / CPU
  commit_storm single-row inserts, commit after each           -> Commit (log file sync)
  row_lock     one session holds a row, others wait for it     -> Application (enq: TX), blocking
  hard_parse   literal SQL, a new cursor every time            -> CPU / Concurrency
  big_sort     sorts with a tiny sort area                     -> User I/O (direct path write/read temp)
  update_mix   batch updates + commits                         -> CPU / Commit / Configuration
"""
import logging
import random
import threading
import time

from . import collectors

log = logging.getLogger("ashv.loadgen")

MODULE = "ASHV demo load"
DATA_ROWS = 50_000
LOG_ROWS_MAX = 100_000

SETUP = [
    """CREATE TABLE lg_data (id NUMBER PRIMARY KEY, grp NUMBER NOT NULL, val NUMBER NOT NULL,
                             pad VARCHAR2(300))""",
    f"""INSERT /*+ APPEND */ INTO lg_data
        SELECT LEVEL, MOD(LEVEL, 97), TRUNC(DBMS_RANDOM.VALUE(1, 1e6)), RPAD('x', 300, 'x')
          FROM dual CONNECT BY LEVEL <= {DATA_ROWS}""",
    "CREATE INDEX lg_data_grp ON lg_data (grp)",
    "CREATE TABLE lg_log (id NUMBER, ts DATE, payload VARCHAR2(200))",
    "CREATE TABLE lg_hot (id NUMBER PRIMARY KEY, val NUMBER)",
    "INSERT INTO lg_hot SELECT LEVEL, 0 FROM dual CONNECT BY LEVEL <= 10",
]


class LoadGenerator(threading.Thread):
    def __init__(self, cfg: dict, password: str):
        super().__init__(daemon=True, name="demo-load")
        self.cfg = cfg
        self.password = password
        self.max_sessions = max(1, int(cfg.get("max_sessions", 3)))
        self._stop = threading.Event()
        self.rng = random.Random()
        self.phase = None
        self.status = "starting"
        self.error = None

    def stop(self):
        self._stop.set()

    def describe(self):
        return {"status": self.status, "phase": self.phase, "error": self.error}

    # ------------------------------------------------------------------ helpers

    def _connect(self, action):
        oracledb = collectors._oracle()
        c = oracledb.connect(user=self.cfg["user"], password=self.password,
                             dsn=collectors.with_timeouts(self.cfg["dsn"]))
        c.module = MODULE
        c.action = action
        c.client_identifier = "ashv-demo"
        return c

    def _ensure_schema(self):
        with self._connect("setup") as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name IN ('LG_DATA','LG_LOG','LG_HOT')")
            if cur.fetchone()[0] == 3:
                return
            for t in ("LG_DATA", "LG_LOG", "LG_HOT"):
                try:
                    cur.execute(f"DROP TABLE {t} PURGE")
                except Exception:  # noqa: BLE001 - table did not exist
                    pass
            for stmt in SETUP:
                cur.execute(stmt)
                c.commit()
            cur.callproc("DBMS_STATS.GATHER_TABLE_STATS", [self.cfg["user"].upper(), "LG_DATA"])
            log.info("demo load schema created")

    def _housekeeping(self):
        with self._connect("housekeeping") as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM lg_log")
            if cur.fetchone()[0] > LOG_ROWS_MAX:
                cur.execute("TRUNCATE TABLE lg_log")

    # ------------------------------------------------------------------ scenarios

    def cpu_burn(self, until):
        with self._connect("cpu_burn") as c:
            cur = c.cursor()
            while time.time() < until and not self._stop.is_set():
                secs = min(8, max(1, until - time.time()))
                cur.execute("""DECLARE x NUMBER := 0; t DATE := SYSDATE;
                               BEGIN WHILE SYSDATE < t + :s / 86400 LOOP
                                 x := MOD(x + SQRT(DBMS_RANDOM.VALUE * 1e6), 1e9);
                               END LOOP; END;""", s=secs)

    def full_scan(self, until):
        with self._connect("full_scan") as c:
            cur = c.cursor()
            try:
                cur.execute("""ALTER SESSION SET "_serial_direct_read" = ALWAYS""")
            except Exception:  # noqa: BLE001 - without it the scans are simply buffered
                pass
            while time.time() < until and not self._stop.is_set():
                cur.execute("""SELECT /*+ FULL(d) */ grp, COUNT(*), SUM(val), MAX(pad)
                                 FROM lg_data d WHERE val > :v GROUP BY grp""",
                            v=self.rng.randint(0, 900_000))
                cur.fetchall()

    def commit_storm(self, until):
        with self._connect("commit_storm") as c:
            cur = c.cursor()
            n = 0
            while time.time() < until and not self._stop.is_set():
                cur.execute("INSERT INTO lg_log (id, ts, payload) VALUES (:i, SYSDATE, :p)",
                            i=n, p=f"event {self.rng.random():.6f}")
                c.commit()
                n += 1
                if n % 50 == 0:
                    time.sleep(0.05)

    def row_lock_holder(self, until, row_id):
        with self._connect("row_lock_holder") as c:
            cur = c.cursor()
            while time.time() < until and not self._stop.is_set():
                cur.execute("UPDATE lg_hot SET val = val + 1 WHERE id = :i", i=row_id)
                # hold the lock (idle in transaction) - the waiters show up as enq: TX
                self._stop.wait(self.rng.uniform(4, 10))
                c.commit()
                self._stop.wait(self.rng.uniform(0.5, 2))

    def row_lock_waiter(self, until, row_id):
        with self._connect("row_lock_waiter") as c:
            c.call_timeout = 20_000
            cur = c.cursor()
            while time.time() < until and not self._stop.is_set():
                try:
                    cur.execute("UPDATE lg_hot SET val = val + 1 WHERE id = :i", i=row_id)
                    c.commit()
                except Exception:  # noqa: BLE001 - call timeout while waiting: just retry
                    try:
                        c.rollback()
                    except Exception:  # noqa: BLE001
                        return
                time.sleep(self.rng.uniform(0.1, 0.5))

    def hard_parse(self, until):
        with self._connect("hard_parse") as c:
            cur = c.cursor()
            while time.time() < until and not self._stop.is_set():
                k = self.rng.randint(1, 10**9)
                cur.execute(f"SELECT COUNT(*) FROM lg_data WHERE grp = {k % 97} AND val > {k % 1000} "
                            f"AND id <> {k}")  # literals on purpose: every statement is a new cursor
                cur.fetchall()

    def big_sort(self, until):
        with self._connect("big_sort") as c:
            cur = c.cursor()
            for stmt in ("ALTER SESSION SET workarea_size_policy = MANUAL",
                         "ALTER SESSION SET sort_area_size = 65536"):
                try:
                    cur.execute(stmt)
                except Exception:  # noqa: BLE001
                    pass
            while time.time() < until and not self._stop.is_set():
                cur.execute("""SELECT id, pad FROM lg_data WHERE grp < :g ORDER BY pad DESC, val""",
                            g=self.rng.randint(20, 60))
                cur.fetchmany(10)

    def update_mix(self, until):
        with self._connect("update_mix") as c:
            cur = c.cursor()
            while time.time() < until and not self._stop.is_set():
                cur.execute("UPDATE lg_data SET val = val + 1 WHERE grp = :g", g=self.rng.randint(0, 96))
                c.commit()
                time.sleep(self.rng.uniform(0.05, 0.3))

    SIMPLE = ("cpu_burn", "full_scan", "commit_storm", "hard_parse", "big_sort", "update_mix")

    # ------------------------------------------------------------------ scheduler

    def _plan(self):
        """Random phase: list of (callable, args). Total sessions <= max_sessions."""
        rng = self.rng
        if rng.random() < 0.25 and self.max_sessions >= 2:
            row = rng.randint(1, 10)
            waiters = rng.randint(1, self.max_sessions - 1)
            return "row_lock", [(self.row_lock_holder, (row,))] + [(self.row_lock_waiter, (row,))] * waiters
        n = rng.randint(1, self.max_sessions)
        names = [rng.choice(self.SIMPLE) for _ in range(n)]
        return "+".join(sorted(set(names))), [(getattr(self, name), ()) for name in names]

    def _worker(self, fn, until, args):
        try:
            fn(until, *args)
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                log.warning("demo load %s: %s", fn.__name__, str(e).splitlines()[0][:200])

    def run(self):
        backoff = 10
        while not self._stop.is_set():
            try:
                self._ensure_schema()
                self.status, self.error = "running", None
                backoff = 10
                while not self._stop.is_set():
                    self._housekeeping()
                    name, plan = self._plan()
                    until = time.time() + self.rng.uniform(20, 90)
                    self.phase = name
                    log.info("demo load phase %s (%d sessions)", name, len(plan))
                    threads = [threading.Thread(target=self._worker, args=(fn, until, args), daemon=True)
                               for fn, args in plan]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(timeout=max(1, until - time.time()) + 30)
                    self.phase = "pause"
                    self._stop.wait(self.rng.uniform(10, 45))
            except Exception as e:  # noqa: BLE001
                self.status, self.error = "error", str(e).splitlines()[0][:300]
                log.warning("demo load: %s", self.error)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)
        self.status = "stopped"
