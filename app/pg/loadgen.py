"""Load generator: keeps a small, random, cyclic workload running on the monitored PostgreSQL database
so that the ASH charts always have something to show.

Everything happens in a dedicated schema (pgash_load) with three small tables, so real application data is
never touched. The workload is deliberately modest (see max_sessions).

Scenarios (each runs in its own connection, application_name = 'PG ASH demo load: <scenario>'):
  cpu_burn     arithmetic over generate_series              -> CPU
  seq_scan     full scans with aggregation                   -> CPU / IO DataFileRead
  commit_storm single-row inserts, commit after each         -> IO WALSync / LWLock WALWrite
  row_lock     one session holds a row, others wait for it   -> Lock transactionid, blocking
  big_sort     sorts with a tiny work_mem                    -> IO BufFileWrite/BufFileRead (temp files)
  update_mix   batch updates + commits                       -> CPU / IO
  idle_tx      a transaction left open for a while           -> "idle in transaction" in the report
"""
import logging
import random
import threading
import time

from . import collectors

log = logging.getLogger("pgash.loadgen")

APP = "PG ASH demo load"
SCHEMA = "pgash_load"
DATA_ROWS = 200_000
LOG_ROWS_MAX = 200_000

SETUP = [
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"DROP TABLE IF EXISTS {SCHEMA}.lg_data, {SCHEMA}.lg_log, {SCHEMA}.lg_hot",
    f"""CREATE TABLE {SCHEMA}.lg_data (id bigint PRIMARY KEY, grp int NOT NULL, val bigint NOT NULL, pad text)""",
    f"""INSERT INTO {SCHEMA}.lg_data
        SELECT g, g % 97, (random() * 1e6)::bigint, repeat(md5(g::text), 6)
          FROM generate_series(1, {DATA_ROWS}) g""",
    f"CREATE INDEX ON {SCHEMA}.lg_data (grp)",
    f"CREATE TABLE {SCHEMA}.lg_log (id bigint, ts timestamptz, payload text)",
    f"CREATE TABLE {SCHEMA}.lg_hot (id int PRIMARY KEY, val bigint)",
    f"INSERT INTO {SCHEMA}.lg_hot SELECT g, 0 FROM generate_series(1, 10) g",
    f"ANALYZE {SCHEMA}.lg_data",
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

    def _connect(self, action, statement_timeout_ms=120000):
        return collectors.connect(self.cfg["dsn"], self.cfg["user"], self.password, app=f"{APP}: {action}",
                                  statement_timeout_ms=statement_timeout_ms)

    def _ensure_schema(self):
        with self._connect("setup", 600000) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM information_schema.tables
                            WHERE table_schema = %s AND table_name IN ('lg_data', 'lg_log', 'lg_hot')""", (SCHEMA,))
            if cur.fetchone()[0] == 3:
                return
            for stmt in SETUP:
                cur.execute(stmt)
            log.info("demo load schema %s created", SCHEMA)

    def _housekeeping(self):
        with self._connect("housekeeping") as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.lg_log")
            if cur.fetchone()[0] > LOG_ROWS_MAX:
                cur.execute(f"TRUNCATE {SCHEMA}.lg_log")

    # ------------------------------------------------------------------ scenarios

    def cpu_burn(self, until):
        with self._connect("cpu_burn") as c, c.cursor() as cur:
            while time.time() < until and not self._stop.is_set():
                cur.execute("SELECT sum(sqrt(g::float8) * ln(g + 1)) FROM generate_series(1, %s) g",
                            (self.rng.randint(2_000_000, 5_000_000),))
                cur.fetchall()

    def seq_scan(self, until):
        with self._connect("seq_scan") as c, c.cursor() as cur:
            cur.execute("SET enable_indexscan = off")
            cur.execute("SET enable_bitmapscan = off")
            while time.time() < until and not self._stop.is_set():
                cur.execute(f"SELECT grp, count(*), sum(val), max(pad) FROM {SCHEMA}.lg_data "
                            "WHERE val > %s GROUP BY grp", (self.rng.randint(0, 900_000),))
                cur.fetchall()

    def commit_storm(self, until):
        with self._connect("commit_storm") as c, c.cursor() as cur:
            n = 0
            while time.time() < until and not self._stop.is_set():
                cur.execute(f"INSERT INTO {SCHEMA}.lg_log (id, ts, payload) VALUES (%s, now(), %s)",
                            (n, f"event {self.rng.random():.6f}"))       # autocommit: one commit per row
                n += 1
                if n % 50 == 0:
                    time.sleep(0.05)

    def row_lock_holder(self, until, row_id):
        with self._connect("row_lock_holder") as c, c.cursor() as cur:
            while time.time() < until and not self._stop.is_set():
                cur.execute("BEGIN")
                cur.execute(f"UPDATE {SCHEMA}.lg_hot SET val = val + 1 WHERE id = %s", (row_id,))
                self._stop.wait(self.rng.uniform(4, 10))          # hold the row: the others wait on it
                cur.execute("COMMIT")
                self._stop.wait(self.rng.uniform(0.5, 2))

    def row_lock_waiter(self, until, row_id):
        with self._connect("row_lock_waiter", statement_timeout_ms=20000) as c, c.cursor() as cur:
            while time.time() < until and not self._stop.is_set():
                try:
                    cur.execute(f"UPDATE {SCHEMA}.lg_hot SET val = val + 1 WHERE id = %s", (row_id,))
                except Exception:  # noqa: BLE001 - statement timeout while waiting: just retry
                    if c.closed:
                        return
                time.sleep(self.rng.uniform(0.1, 0.5))

    def big_sort(self, until):
        with self._connect("big_sort") as c, c.cursor() as cur:
            cur.execute("SET work_mem = '64kB'")
            while time.time() < until and not self._stop.is_set():
                cur.execute(f"SELECT id, pad FROM {SCHEMA}.lg_data WHERE grp < %s ORDER BY pad DESC, val LIMIT 10",
                            (self.rng.randint(20, 60),))
                cur.fetchall()

    def update_mix(self, until):
        with self._connect("update_mix") as c, c.cursor() as cur:
            while time.time() < until and not self._stop.is_set():
                cur.execute(f"UPDATE {SCHEMA}.lg_data SET val = val + 1 WHERE grp = %s", (self.rng.randint(0, 96),))
                time.sleep(self.rng.uniform(0.05, 0.3))

    def idle_tx(self, until):
        with self._connect("idle_tx") as c, c.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.lg_hot")
            self._stop.wait(max(1, until - time.time()))           # an open, idle transaction
            cur.execute("ROLLBACK")

    SIMPLE = ("cpu_burn", "seq_scan", "commit_storm", "big_sort", "update_mix", "idle_tx")

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
                log.warning("demo load %s: %s", fn.__name__, str(e).strip().splitlines()[0][:200] if str(e).strip() else e)

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
                self.status, self.error = "error", (str(e).strip().splitlines() or [repr(e)])[0][:300]
                log.warning("demo load: %s", self.error)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)
        self.status = "stopped"
