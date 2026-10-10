"""Collectors: periodically sample active sessions and persist them to the local store.

PostgreSQL source: pg_stat_activity is polled every `interval` seconds (like ASH Viewer does for
V$SESSION). Every active backend becomes one sample with its wait event (wait_event_type / wait_event,
"ON CPU" when it is not waiting), its query and the sessions blocking it (pg_blocking_pids).

A query is identified by query_id (PostgreSQL 14+ with compute_query_id) or by a fingerprint of the
normalised text (literals and $n replaced by "?"), so different values of one statement fold together;
the same fingerprint of pg_stat_statements.query links a sampled query to its statistics.

A synthetic `demo` source is available for trying the UI without a database.
"""
from __future__ import annotations
import hashlib
import logging
import math
import os
import random
import re
import threading
import time
import zlib

from .. import store

log = logging.getLogger("pgash.collector")

APP_NAME = "PG ASH Viewer"
DEFAULT_PORT = 5432
LOGIN_TIMEOUT = 20  # seconds for the whole connect + first query from the login form


class Collector(threading.Thread):
    def __init__(self, cfg: dict):
        super().__init__(daemon=True, name=f"collector-{cfg['name']}")
        self.cfg = cfg
        self.name_ = cfg["name"]
        self.interval = float(cfg.get("interval", 1))
        self.status = "starting"
        self.error = None
        self.last_sample = None
        self.info = {}
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def describe(self):
        return {
            "name": self.name_,
            "type": self.cfg.get("type"),
            "source": "pg_stat_activity",
            "dsn": self.cfg.get("dsn"),
            "interval": self.interval,
            "status": self.status,
            "error": self.error,
            "last_sample": self.last_sample,
            "info": self.info,
        }

    # ---- hooks for ad-hoc queries from the API (overridden by PostgreSQL) ----
    def sql_details(self, sql_id: str) -> dict:
        return {}

    def take_snapshot(self, manual: bool = False) -> int:
        raise RuntimeError("Снимки статистики поддерживаются только для PostgreSQL")

    def native_awr(self) -> dict:
        return {"available": False, "reason": "в PostgreSQL нет AWR: отчёт строится по снимкам статистики"}


# --------------------------------------------------------------------------- connection parameters

_DSN = re.compile(r"^\s*(?:\[([0-9a-fA-F:]+)\]|([\w.\-]+))(?::(\d+))?/([\w.\-$]+)\s*$")


def parse_dsn(dsn: str) -> dict:
    """host[:port]/database -> keyword arguments for psycopg.connect."""
    m = _DSN.match(dsn or "")
    if not m:
        raise ValueError(f"Неверный адрес базы «{dsn}»: нужен хост:порт/база")
    host6, host, port, db = m.groups()
    return {"host": host6 or host, "port": int(port or DEFAULT_PORT), "dbname": db}


def connect(dsn: str, user: str, password: str, dbname: str | None = None, timeout: int = 15,
            statement_timeout_ms: int = 10000, app: str = APP_NAME):
    import psycopg
    kw = parse_dsn(dsn)
    if dbname:
        kw["dbname"] = dbname
    return psycopg.connect(user=user, password=password, connect_timeout=timeout, autocommit=True,
                           application_name=app, options=f"-c statement_timeout={int(statement_timeout_ms)}",
                           **kw)


def _local_host(host: str) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1", "") or host.startswith("/")


# --------------------------------------------------------------------------- query fingerprints

_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)
_STRING = re.compile(r"(?:[eE]|[uU]&)?'(?:[^']|'')*'")
_DOLLAR = re.compile(r"\$([A-Za-z_]\w*|)\$.*?\$\1\$", re.S)
_PARAM = re.compile(r"\$\d+")
_NUMBER = re.compile(r"(?<![\w.$])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w])")
_LIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"\s*([^\w\s?])\s*")      # "id = ?" and "id=?" are the same statement


def normalize(query: str) -> str:
    q = _COMMENT.sub(" ", query or "")
    q = _DOLLAR.sub("?", q)
    q = _STRING.sub("?", q)
    q = _PARAM.sub("?", q)
    q = _NUMBER.sub("?", q)
    q = _SPACE.sub(" ", q).strip().rstrip(";").strip().lower()
    q = _PUNCT.sub(r"\1", q)
    return _LIST.sub("(...)", q)


def fingerprint(query: str) -> str:
    return hashlib.md5(normalize(query).encode()).hexdigest()[:16]


def opname(query: str) -> str | None:
    m = re.match(r"\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*(\w+)", query or "", re.S)
    if not m:
        return None
    w = m.group(1).upper()
    if w == "WITH":
        for k in ("INSERT", "UPDATE", "DELETE", "MERGE"):
            if re.search(rf"\)\s*{k}\b", query, re.I):
                return k
        return "SELECT"
    return w


def _sql_id(query_id, query: str):
    if query_id not in (None, 0):
        return str(query_id)
    if not query or not query.strip():
        return None
    return fingerprint(query)


# --------------------------------------------------------------------------- PostgreSQL

class PostgresCollector(Collector):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.conn = None
        self.include_bg = bool(cfg.get("include_background", True))
        self.snapshot_minutes = float(cfg.get("snapshot_minutes", 30))
        self.last_snapshot = None
        self._snap_lock = threading.Lock()
        self._known_sql: set = set()
        self.version_num = 0
        self.has_query_id = False

    def _password(self):
        if self.cfg.get("password_env"):
            return os.environ.get(self.cfg["password_env"], "")
        return self.cfg.get("password", "")

    def new_connection(self, dbname: str | None = None, statement_timeout_ms: int = 15000):
        return connect(self.cfg["dsn"], self.cfg["user"], self._password(), dbname=dbname,
                       statement_timeout_ms=statement_timeout_ms)

    def _connect(self):
        self.conn = self.new_connection(statement_timeout_ms=5000)
        with self.conn.cursor() as cur:
            cur.execute("""SELECT current_setting('server_version_num')::int, current_setting('server_version'),
                                  current_database(), coalesce(nullif(current_setting('cluster_name', true), ''), 'postgres'),
                                  coalesce(host(inet_server_addr()), 'local'), pg_postmaster_start_time()::text,
                                  current_setting('max_connections')::int""")
            vnum, ver, db, cluster, host, started, maxconn = cur.fetchone()
        self.version_num = vnum
        self.has_query_id = vnum >= 140000
        cpus = int(self.cfg.get("cpu_count") or 0)
        if not cpus and _local_host(parse_dsn(self.cfg["dsn"])["host"]):
            cpus = os.cpu_count() or 0          # the database runs on this very computer
        self.info = {"instance": cluster, "host": host if host != "local" else parse_dsn(self.cfg["dsn"])["host"],
                     "version": ver, "db_name": db, "cpu_count": cpus, "started": started,
                     "max_connections": maxconn}
        log.info("[%s] connected: %s", self.name_, self.info)

    def run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                if self.conn is None or self.conn.closed:
                    self.status = "connecting"
                    self._connect()
                    backoff = 5
                self.status = "running"
                self.error = None
                t0 = time.time()
                self._sample()
                self._maybe_snapshot()
                self._stop.wait(max(0.05, self.interval - (time.time() - t0)))
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.error = explain(str(e).strip().splitlines()[0][:400] if str(e).strip() else repr(e))
                log.warning("[%s] %s", self.name_, self.error)
                self._close()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)
        self._close()
        self.status = "stopped"

    def _close(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None

    def _sample(self):
        qid = "a.query_id" if self.has_query_id else "NULL::bigint"
        bg = """OR (a.backend_type <> 'client backend' AND a.state IS DISTINCT FROM 'idle'
                    AND coalesce(a.wait_event_type, '') <> 'Activity')""" if self.include_bg else ""
        sql = f"""
            SELECT a.pid, extract(epoch FROM a.backend_start)::bigint, a.usename, a.application_name,
                   a.client_hostname, host(a.client_addr), a.datname, a.backend_type,
                   a.wait_event_type, a.wait_event, left(a.query, 4000), pg_blocking_pids(a.pid), {qid}
              FROM pg_stat_activity a
             WHERE a.pid <> pg_backend_pid()
               AND ((a.backend_type IN ('client backend', 'parallel worker') AND a.state = 'active') {bg})"""
        ts = int(time.time())
        with self.conn.cursor() as cur:
            cur.execute(sql)
            data = cur.fetchall()
        rows, texts = [], []
        for (pid, started, user, app, chost, caddr, db, btype, wtype, wevent, query, blockers, query_id) in data:
            client = btype in ("client backend", "parallel worker")
            sql_id = _sql_id(query_id, query) if client else None
            op = opname(query) if sql_id else None
            if sql_id and sql_id not in self._known_sql:
                self._known_sql.add(sql_id)
                texts.append((sql_id, query, op))
            blocker = blockers[0] if blockers else None
            rows.append({
                "target": self.name_, "ts": ts, "inst_id": 1, "sid": pid, "serial": int(started or 0) % 100000,
                "username": user or f"({btype})", "program": app or btype, "module": db,
                "machine": chost or caddr or ("local" if client else None), "sql_id": sql_id,
                "sql_opname": op, "plan_hash": None,
                "event": wevent if wtype else "ON CPU", "wait_class": wtype or "CPU",
                "blocking_inst": 1 if blocker else None, "blocking_sid": blocker,
                "session_type": "USER" if client else "BACKGROUND",
            })
        store.write_samples(self.name_, [ts], rows)
        if texts:
            store.save_sqltext(self.name_, texts)
        self.last_sample = ts

    # ---- statistics snapshots (AWR-like report)

    def _maybe_snapshot(self):
        if self.snapshot_minutes <= 0:
            return
        if self.last_snapshot is None:
            self.last_snapshot = store.last_snapshot_ts(self.name_)
        if time.time() - self.last_snapshot >= self.snapshot_minutes * 60 and not self._snap_lock.locked():
            self.last_snapshot = time.time()
            threading.Thread(target=self._snapshot_bg, daemon=True, name=f"snap-{self.name_}").start()

    def _snapshot_bg(self):
        try:
            self.take_snapshot()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] snapshot failed: %s", self.name_, str(e).splitlines()[0][:300])

    def take_snapshot(self, manual: bool = False) -> int:
        from . import awr
        if self.conn is None:
            raise RuntimeError("нет подключения к БД")
        with self._snap_lock:
            with self.new_connection(statement_timeout_ms=60000) as c:
                snap = awr.capture(c, int(self.info.get("cpu_count") or 0))
            sid = awr.save(self.name_, snap, manual)
            self.last_snapshot = time.time()
            log.info("[%s] statistics snapshot %s taken%s", self.name_, sid, " (manual)" if manual else "")
            return sid

    # ---- one statement

    def _pgss(self, cur):
        """pg_stat_statements columns of this version, or None when the extension is not usable."""
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
        if not cur.fetchone():
            return None
        new = self.version_num >= 130000
        return {"total": "total_exec_time" if new else "total_time",
                "mean": "mean_exec_time" if new else "mean_time"}

    def sql_details(self, sql_id: str) -> dict:
        if self.conn is None:
            return {"error": "нет подключения к БД"}
        row = store.conn().execute("SELECT text FROM sqltext WHERE target=? AND sql_id=?",
                                   (self.name_, sql_id)).fetchone()
        text = row[0] if row else None
        db = store.conn().execute("SELECT module FROM samples WHERE target=? AND sql_id=? AND module IS NOT NULL "
                                  "ORDER BY ts DESC LIMIT 1", (self.name_, sql_id)).fetchone()
        dbname = db[0] if db else None
        out = {"text": text, "schema": dbname}
        with self.new_connection() as c, c.cursor() as cur:
            try:
                cols = self._pgss(cur)
            except Exception:  # noqa: BLE001
                cols = None
            if cols:
                try:
                    cur.execute(f"""SELECT s.queryid, s.query, s.calls, s.{cols['total']}, s.{cols['mean']}, s.rows,
                                           s.shared_blks_hit, s.shared_blks_read, s.temp_blks_written,
                                           s.blk_read_time + s.blk_write_time, d.datname, r.rolname
                                      FROM pg_stat_statements s
                                      LEFT JOIN pg_database d ON d.oid = s.dbid
                                      LEFT JOIN pg_roles r ON r.oid = s.userid""")
                    stats = cur.fetchall()
                except Exception as e:  # noqa: BLE001 - e.g. not in shared_preload_libraries
                    stats = []
                    out["note"] = f"pg_stat_statements недоступно: {str(e).strip().splitlines()[0]}"
                best = None
                for q in stats:
                    if str(q[0]) == sql_id or fingerprint(q[1] or "") == sql_id:
                        if best is None or (q[2] or 0) > (best[2] or 0):
                            best = q
                if best:
                    calls = best[2] or 0
                    hit, read = best[6] or 0, best[7] or 0
                    out.update({
                        "normalized": best[1], "executions": calls, "elapsed_s": round((best[3] or 0) / 1e3, 3),
                        "elapsed_per_exec_ms": round(best[4] or 0, 3), "rows": best[5],
                        "buffer_gets": hit + read, "disk_reads": read,
                        "hit_pct": round(100.0 * hit / (hit + read), 2) if hit + read else None,
                        "temp_blocks": best[8], "io_s": round((best[9] or 0) / 1e3, 3),
                        "schema": best[10] or dbname, "module": best[11], "queryid": str(best[0]),
                    })
                    if not text:
                        out["text"] = best[1]
            else:
                out["note"] = ("Расширение pg_stat_statements не подключено: статистика выполнений недоступна "
                               "(см. рекомендации отчёта).")
        out["plan"] = self._explain(out.get("text"), dbname)
        return out

    def _explain(self, text: str | None, dbname: str | None) -> str:
        """EXPLAIN without executing. Statements with $1, $2… get the generic plan (no parameter values):
        EXPLAIN (GENERIC_PLAN) on 16+, PREPARE with plan_cache_mode = force_generic_plan on 12–15."""
        if not text:
            return "Текст запроса неизвестен."
        if opname(text) not in ("SELECT", "INSERT", "UPDATE", "DELETE", "VALUES", "TABLE"):
            return "План строится только для SELECT/INSERT/UPDATE/DELETE."
        params = [int(n) for n in re.findall(r"\$(\d+)", _STRING.sub("", text))]
        if params and self.version_num < 120000:
            return "Запрос с параметрами ($1, $2…): общий план строится на PostgreSQL 12 и новее."
        try:
            with self.new_connection(dbname=dbname, statement_timeout_ms=5000) as c, c.cursor() as cur:
                cur.execute("BEGIN")
                try:
                    cur.execute("SET LOCAL transaction_read_only = on")
                    if not params:
                        cur.execute("EXPLAIN (FORMAT TEXT) " + text)
                        head = ""
                    elif self.version_num >= 160000:
                        cur.execute("EXPLAIN (GENERIC_PLAN, FORMAT TEXT) " + text)
                        head = "Общий план (без значений параметров):\n"
                    else:
                        cur.execute("SET LOCAL plan_cache_mode = force_generic_plan")
                        cur.execute("PREPARE pgash_explain AS " + text)
                        nulls = ", ".join(["NULL"] * max(params))
                        cur.execute(f"EXPLAIN (FORMAT TEXT) EXECUTE pgash_explain({nulls})")
                        head = "Общий план (без значений параметров):\n"
                    return head + "\n".join(r[0] for r in cur.fetchall())
                finally:
                    cur.execute("ROLLBACK")
                    try:
                        cur.execute("DEALLOCATE ALL")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            return f"План недоступен: {str(e).strip().splitlines()[0] if str(e).strip() else e}"


# --------------------------------------------------------------------------- login check and errors

_HINTS = [
    ("password authentication failed", "неверное имя пользователя или пароль"),
    ("no pg_hba.conf entry", "сервер не разрешает подключение с этого адреса: добавьте строку в pg_hba.conf"),
    ("connection refused", "на этом хосте и порту не запущен PostgreSQL"),
    ("could not translate host name", "хост не найден"),
    ("nodename nor servname", "хост не найден"),
    ("timeout expired", "истекло время подключения: хост недоступен"),
    ("timed out", "истекло время подключения: хост недоступен"),
    ("remaining connection slots", "исчерпан лимит подключений сервера (max_connections)"),
    ("too many connections", "исчерпан лимит подключений сервера (max_connections)"),
    ("permission denied", "недостаточно прав: для полной картины нужна роль pg_monitor"),
]


def explain(err: str) -> str:
    low = (err or "").lower()
    for needle, hint in _HINTS:
        if needle in low:
            return f"{hint} — {err.strip()}"
    if "does not exist" in low and low.lstrip().startswith(("database", "fatal:  database", "fatal: database")):
        return f"такой базы данных нет — {err.strip()}"
    if "does not exist" in low and "role" in low:
        return f"такого пользователя нет — {err.strip()}"
    return err


def auth_failure(msg: str) -> bool:
    low = (msg or "").lower()
    return "password authentication failed" in low or "неверное имя пользователя или пароль" in low


def test_login(dsn: str, user: str, password: str, mode: str = "") -> dict:
    """Open a single connection with the given credentials; raise on failure or timeout."""
    import concurrent.futures as cf

    def attempt():
        with connect(dsn, user, password, timeout=15) as c, c.cursor() as cur:
            cur.execute("SELECT current_database(), current_setting('server_version'), "
                        "pg_has_role(current_user, 'pg_monitor', 'MEMBER') OR "
                        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)")
            db, ver, monitor = cur.fetchone()
            return {"instance": db, "version": ver, "monitor": bool(monitor)}

    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(attempt).result(timeout=LOGIN_TIMEOUT)
    except cf.TimeoutError:
        raise TimeoutError(f"Сервер БД не ответил за {LOGIN_TIMEOUT} с: проверьте хост и порт") from None
    finally:
        ex.shutdown(wait=False)


# --------------------------------------------------------------------------- Demo

_DEMO_SQL = [
    ("a1f3c09e7b2d4410", "SELECT", "SELECT o.order_id, o.status, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.created_at > $1"),
    ("b7e2d1c4a9f03311", "UPDATE", "UPDATE accounts SET balance = balance - $1 WHERE account_id = $2"),
    ("c0d9e8f7a6b51122", "INSERT", "INSERT INTO audit_log (ts, user_id, action, payload) VALUES (now(), $1, $2, $3)"),
    ("d4c3b2a1f0e98833", "SELECT", "SELECT sum(amount) FROM transactions t WHERE tx_date::date = current_date"),
    ("e5f6a7b8c9d04455", "SELECT", "SELECT billing.close_period($1)"),
    ("f1e2d3c4b5a69966", "SELECT", "SELECT * FROM products WHERE sku = $1"),
    ("0a9b8c7d6e5f7788", "DELETE", "DELETE FROM session_cache WHERE expires_at < now()"),
    ("1b2c3d4e5f60aa99", "SELECT", "SELECT count(*) FROM inventory i JOIN warehouses w ON i.wh_id = w.id WHERE w.region = $1"),
]
_DEMO_PROFILES = {
    "a1f3c09e7b2d4410": [("CPU", "ON CPU", 5), ("IO", "DataFileRead", 4)],
    "b7e2d1c4a9f03311": [("CPU", "ON CPU", 2), ("Lock", "transactionid", 3), ("IO", "WALSync", 2)],
    "c0d9e8f7a6b51122": [("CPU", "ON CPU", 2), ("IO", "WALSync", 3), ("LWLock", "WALWrite", 1)],
    "d4c3b2a1f0e98833": [("IO", "DataFileRead", 6), ("CPU", "ON CPU", 3)],
    "e5f6a7b8c9d04455": [("CPU", "ON CPU", 4), ("Client", "ClientWrite", 1), ("LWLock", "BufferMapping", 1)],
    "f1e2d3c4b5a69966": [("CPU", "ON CPU", 6), ("LWLock", "lock_manager", 1)],
    "0a9b8c7d6e5f7788": [("IO", "DataFileRead", 3), ("IO", "DataFileExtend", 1)],
    "1b2c3d4e5f60aa99": [("CPU", "ON CPU", 3), ("IO", "DataFilePrefetch", 3)],
}
_DEMO_USERS = [("app", "order-service", "shop", "10.0.0.11"),
               ("app", "billing-service", "shop", "10.0.0.12"),
               ("report", "psql", "dwh", "10.0.0.21"),
               ("etl", "airflow", "dwh", "10.0.0.31")]


def _h(s: str) -> int:
    return zlib.crc32(s.encode())


class DemoCollector(Collector):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.info = {"instance": "demo", "host": "demo-host", "version": "16.4", "db_name": "shop",
                     "cpu_count": 8}
        self.rng = random.Random(42)

    def run(self):
        now = int(time.time())
        hours = float(self.cfg.get("backfill_hours", 3))
        for ts in range(now - int(hours * 3600), now):
            self._tick(ts)
        self.status = "running"
        while not self._stop.is_set():
            t0 = time.time()
            self._tick(int(t0))
            self._stop.wait(max(0.05, self.interval - (time.time() - t0)))

    def _load(self, ts):
        base = 4 + 3 * math.sin(ts / 900.0) + 2 * math.sin(ts / 137.0)
        spike = 9 if (ts // 600) % 6 == 0 and (ts % 600) < 180 else 0  # periodic incident
        return max(0.5, base + spike), spike > 0

    def _tick(self, ts):
        rng = self.rng
        load, incident = self._load(ts)
        n = max(0, int(rng.gauss(load, 1.2)))
        rows = []
        for i in range(n):
            sql_id = "b7e2d1c4a9f03311" if incident and rng.random() < 0.6 else rng.choice(_DEMO_SQL)[0]
            op = next(o for s, o, _ in _DEMO_SQL if s == sql_id)
            prof = _DEMO_PROFILES[sql_id]
            wc, ev, _ = rng.choices(prof, weights=[p[2] for p in prof])[0]
            pid = 20000 + (_h(f"{sql_id}{i % 3}") % 4000)
            user, app, db, host = _DEMO_USERS[_h(sql_id) % len(_DEMO_USERS)]
            blocker = 18731 if wc == "Lock" else None
            rows.append({"target": self.name_, "ts": ts, "inst_id": 1, "sid": pid, "serial": pid % 977,
                         "username": user, "program": app, "module": db, "machine": host,
                         "sql_id": sql_id, "sql_opname": op, "plan_hash": None,
                         "event": ev, "wait_class": wc, "blocking_inst": 1 if blocker else None,
                         "blocking_sid": blocker, "session_type": "USER"})
        if rng.random() < 0.3:
            rows.append({"target": self.name_, "ts": ts, "inst_id": 1, "sid": 61, "serial": 1,
                         "username": "(walwriter)", "program": "walwriter", "event": "WALWrite",
                         "wait_class": "IO", "session_type": "BACKGROUND"})
        if rng.random() < 0.05:
            rows.append({"target": self.name_, "ts": ts, "inst_id": 1, "sid": 77, "serial": 1,
                         "username": "(autovacuum worker)", "program": "autovacuum worker", "module": "shop",
                         "event": "VacuumDelay", "wait_class": "Timeout", "session_type": "BACKGROUND"})
        store.write_samples(self.name_, [ts], rows)
        if not hasattr(self, "_texts_saved"):
            store.save_sqltext(self.name_, [(s, t, o) for s, o, t in _DEMO_SQL])
            self._texts_saved = True
        self.last_sample = ts

    def sql_details(self, sql_id):
        for s, o, t in _DEMO_SQL:
            if s == sql_id:
                return {"text": t, "schema": "shop", "executions": 123456, "elapsed_s": 987.6,
                        "elapsed_per_exec_ms": 8.0, "buffer_gets": 98765432, "disk_reads": 123456,
                        "hit_pct": 99.87, "rows": 654321,
                        "plan": "Демо-режим: план выполнения доступен только для реальной БД."}
        return {}


def make(cfg: dict) -> Collector:
    t = cfg.get("type", "postgres")
    if t == "demo":
        return DemoCollector(cfg)
    if t in ("postgres", "postgresql"):
        return PostgresCollector(cfg)
    raise ValueError(f"unknown target type {t}")
