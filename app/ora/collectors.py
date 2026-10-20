"""Collectors: periodically sample active sessions and persist them to the local store.

Two Oracle sources are supported:
  * vsession - poll (G)V$SESSION every `interval` seconds, like ASH Viewer does.
               Works on any edition, no Diagnostics Pack licence needed.
  * ash      - incrementally copy (G)V$ACTIVE_SESSION_HISTORY (requires Diagnostics Pack).
A synthetic `demo` source is available for trying the UI without a database.
"""
from __future__ import annotations
import logging
import math
import os
import random
import re
import threading
import time
import zlib

from .. import demo_snap, store

log = logging.getLogger("ashv.collector")

# V$SESSION.COMMAND -> operation name (most common codes)
COMMANDS = {
    1: "CREATE TABLE", 2: "INSERT", 3: "SELECT", 6: "UPDATE", 7: "DELETE", 9: "CREATE INDEX",
    11: "ALTER INDEX", 12: "DROP TABLE", 15: "ALTER TABLE", 26: "LOCK TABLE", 44: "COMMIT",
    45: "ROLLBACK", 47: "PL/SQL EXECUTE", 62: "ANALYZE TABLE", 85: "TRUNCATE TABLE",
    170: "CALL METHOD", 189: "UPSERT",
}


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
            "source": self.cfg.get("source", "vsession"),
            "dsn": self.cfg.get("dsn"),
            "interval": self.interval,
            "status": self.status,
            "error": self.error,
            "last_sample": self.last_sample,
            "info": self.info,
        }

    # ---- hooks for ad-hoc queries from the API (overridden by Oracle) ----
    def sql_details(self, sql_id: str) -> dict:
        return {}

    def take_snapshot(self, manual: bool = False) -> int:
        raise RuntimeError("Снимки статистики поддерживаются только для Oracle")

    def native_awr(self) -> dict:
        return {"available": False, "reason": "только для Oracle"}


# --------------------------------------------------------------------------- Oracle

_ORACLE_INIT_DONE = False


def _oracle():
    global _ORACLE_INIT_DONE
    import oracledb
    if not _ORACLE_INIT_DONE:
        if os.environ.get("ORACLE_THICK", "0") == "1":
            oracledb.init_oracle_client(lib_dir=os.environ.get("ORACLE_CLIENT_LIB") or None)
        _ORACLE_INIT_DONE = True
    return oracledb


class OracleCollector(Collector):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.pool = None
        self.rac = bool(cfg.get("rac", False))
        self.source = cfg.get("source", "vsession")
        self.include_bg = bool(cfg.get("include_background", True))
        self.ash_last = None          # DB-local DATE of last copied ASH sample
        self.tz_offset = 0            # db_local_epoch - real_epoch (seconds)
        self.snapshot_minutes = float(cfg.get("snapshot_minutes", 30))
        self.last_snapshot = None
        self._snap_lock = threading.Lock()

    def _password(self):
        if self.cfg.get("password_env"):
            return os.environ.get(self.cfg["password_env"], "")
        return self.cfg.get("password", "")

    def _connect(self):
        oracledb = _oracle()
        params = {}
        mode = (self.cfg.get("mode") or "").upper()
        if mode == "SYSDBA":
            params["mode"] = oracledb.AUTH_MODE_SYSDBA
        self.pool = oracledb.create_pool(
            user=self.cfg["user"], password=self._password(), dsn=with_timeouts(self.cfg["dsn"]),
            min=1, max=3, increment=1, **params,
        )
        with self.pool.acquire() as c:
            cur = c.cursor()
            cur.execute("""SELECT i.instance_name, i.host_name, i.version, d.name,
                                  (SELECT value FROM v$parameter WHERE name = 'cpu_count'),
                                  ROUND((SYSDATE - DATE '1970-01-01') * 86400)
                           FROM v$instance i, v$database d""")
            inst, host, ver, dbname, cpus, db_epoch = cur.fetchone()
            self.tz_offset = int(round((int(db_epoch) - time.time()) / 900.0) * 900)
            self.info = {"instance": inst, "host": host, "version": ver, "db_name": dbname,
                         "cpu_count": int(cpus or 0)}
            if self.rac:
                cur.execute("SELECT SUM(value) FROM gv$parameter WHERE name = 'cpu_count'")
                self.info["cpu_count"] = int(cur.fetchone()[0] or 0)
        log.info("[%s] connected: %s", self.name_, self.info)

    def run(self):
        backoff = 5
        last_text = 0
        while not self._stop.is_set():
            try:
                if self.pool is None:
                    self.status = "connecting"
                    self._connect()
                    backoff = 5
                self.status = "running"
                self.error = None
                t0 = time.time()
                if self.source == "ash":
                    self._copy_ash()
                else:
                    self._sample_vsession()
                if t0 - last_text > 10:
                    self._fetch_sqltext()
                    last_text = t0
                self._maybe_snapshot()
                period = self.interval if self.source != "ash" else max(self.interval, 10)
                self._stop.wait(max(0.05, period - (time.time() - t0)))
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.error = str(e).splitlines()[0][:400]
                log.warning("[%s] %s", self.name_, self.error)
                try:
                    if self.pool:
                        self.pool.close(force=True)
                except Exception:  # noqa: BLE001
                    pass
                self.pool = None
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)
        if self.pool:
            try:
                self.pool.close(force=True)
            except Exception:  # noqa: BLE001
                pass
            self.pool = None
        self.status = "stopped"

    def _sample_vsession(self):
        v = "gv$session" if self.rac else "v$session"
        inst = "s.inst_id" if self.rac else "SYS_CONTEXT('USERENV','INSTANCE')"
        bg = "" if self.include_bg else "AND s.type = 'USER'"
        sql = f"""
            SELECT {inst}, s.sid, s.serial#, s.username, s.program, s.module, s.machine,
                   s.sql_id, s.command,
                   CASE WHEN s.state = 'WAITING' THEN s.event ELSE 'ON CPU' END,
                   CASE WHEN s.state = 'WAITING' THEN s.wait_class ELSE 'CPU' END,
                   s.blocking_instance, s.blocking_session, s.type
              FROM {v} s
             WHERE s.status = 'ACTIVE'
               AND (s.state <> 'WAITING' OR s.wait_class <> 'Idle')
               AND s.sid <> SYS_CONTEXT('USERENV','SID')
               {bg}"""
        ts = int(time.time())
        with self.pool.acquire() as c:
            cur = c.cursor()
            cur.execute(sql)
            data = cur.fetchall()
        rows = []
        for (inst_id, sid, serial, user, prog, mod, mach, sql_id, cmd, event, wclass,
             binst, bsid, stype) in data:
            rows.append({
                "target": self.name_, "ts": ts, "inst_id": int(inst_id) if inst_id else 1,
                "sid": sid, "serial": serial, "username": user or (prog and f"({_bg_name(prog)})"),
                "program": prog, "module": mod, "machine": mach, "sql_id": sql_id,
                "sql_opname": COMMANDS.get(cmd) if cmd else None, "event": event,
                "wait_class": wclass, "blocking_inst": binst, "blocking_sid": bsid,
                "session_type": stype,
            })
        store.write_samples(self.name_, [ts], rows)
        self.last_sample = ts

    def _copy_ash(self):
        v = "gv$active_session_history" if self.rac else "v$active_session_history"
        inst = "h.inst_id" if self.rac else "SYS_CONTEXT('USERENV','INSTANCE')"
        with self.pool.acquire() as c:
            cur = c.cursor()
            if self.ash_last is None:
                cur.execute("SELECT SYSDATE - :m / 1440 FROM dual",
                            m=float(self.cfg.get("ash_backfill_minutes", 60)))
                self.ash_last = cur.fetchone()[0]
            cur.execute(f"""
                SELECT {inst}, ROUND((CAST(h.sample_time AS DATE) - DATE '1970-01-01') * 86400),
                       CAST(h.sample_time AS DATE),
                       h.session_id, h.session_serial#, u.username, h.program, h.module, h.machine,
                       h.sql_id, h.sql_opname, h.sql_plan_hash_value,
                       CASE WHEN h.session_state = 'WAITING' THEN h.event ELSE 'ON CPU' END,
                       CASE WHEN h.session_state = 'WAITING' THEN h.wait_class ELSE 'CPU' END,
                       h.blocking_inst_id, h.blocking_session, h.session_type
                  FROM {v} h LEFT JOIN dba_users u ON u.user_id = h.user_id
                 WHERE CAST(h.sample_time AS DATE) > :last""", last=self.ash_last)
            data = cur.fetchall()
            cur.execute("SELECT SYSDATE - 2/86400 FROM dual")
            upto = cur.fetchone()[0]
        rows, newest = [], self.ash_last
        for (inst_id, db_ts, dt, sid, serial, user, prog, mod, mach, sql_id, opname, phv, event,
             wclass, binst, bsid, stype) in data:
            if dt > upto:
                continue  # sample second may still be in progress; take it next round
            newest = max(newest, dt)
            rows.append({
                "target": self.name_, "ts": int(db_ts) - self.tz_offset,
                "inst_id": int(inst_id) if inst_id else 1, "sid": sid, "serial": serial,
                "username": user or (prog and f"({_bg_name(prog)})"), "program": prog,
                "module": mod, "machine": mach, "sql_id": sql_id, "sql_opname": opname,
                "plan_hash": phv, "event": event, "wait_class": wclass, "blocking_inst": binst,
                "blocking_sid": bsid, "session_type": stype,
            })
        start = int((self.ash_last - _EPOCH).total_seconds()) - self.tz_offset + 1
        end = int((upto - _EPOCH).total_seconds()) - self.tz_offset
        ticks = list(range(start, end + 1))
        store.write_samples(self.name_, ticks, rows)
        self.ash_last = max(newest, upto)
        if ticks:
            self.last_sample = ticks[-1]

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
        if self.pool is None:
            raise RuntimeError("нет подключения к БД")
        with self._snap_lock:
            with self.pool.acquire() as c:
                snap = awr.capture(c)
            sid = awr.save(self.name_, snap, manual)
            self.last_snapshot = time.time()
            log.info("[%s] statistics snapshot %s taken%s", self.name_, sid, " (manual)" if manual else "")
            return sid

    def native_awr(self) -> dict:
        """Oracle's own AWR - only when the Diagnostics Pack is enabled (licensed) for the database."""
        if self.pool is None:
            return {"available": False, "reason": "нет подключения к БД"}
        with self.pool.acquire() as c:
            cur = c.cursor()
            cur.execute("SELECT value FROM v$parameter WHERE name = 'control_management_pack_access'")
            r = cur.fetchone()
            access = (r[0] if r else "") or ""
            if "DIAGNOSTIC" not in access.upper():
                return {"available": False, "reason": f"CONTROL_MANAGEMENT_PACK_ACCESS = {access or 'NONE'}: "
                        "Diagnostics Pack не включён, отчёт AWR Oracle использовать нельзя по лицензии"}
            cur.execute("""SELECT s.snap_id, s.dbid, s.instance_number,
                                  TO_CHAR(s.end_interval_time, 'YYYY-MM-DD HH24:MI') end_time
                             FROM dba_hist_snapshot s, v$database d, v$instance i
                            WHERE s.dbid = d.dbid AND s.instance_number = i.instance_number
                            ORDER BY s.snap_id DESC""")
            snaps = [{"snap_id": a, "dbid": b, "inst": c_, "end_time": d} for a, b, c_, d in cur.fetchmany(500)]
            return {"available": True, "snapshots": snaps}

    def native_awr_report(self, begin: int, end: int) -> str:
        info = self.native_awr()
        if not info.get("available"):
            raise RuntimeError(info.get("reason"))
        s = next((x for x in info["snapshots"] if x["snap_id"] == end), None) or info["snapshots"][0]
        with self.pool.acquire() as c:
            cur = c.cursor()
            cur.execute("SELECT output FROM TABLE(DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_HTML(:d, :i, :b, :e))",
                        d=s["dbid"], i=s["inst"], b=begin, e=end)
            return "\n".join(r[0] or "" for r in cur)

    def _fetch_sqltext(self):
        ids = store.missing_sqltext(self.name_, int(time.time()) - 3600)
        if not ids:
            return
        v = "gv$sqlarea" if self.rac else "v$sqlarea"
        found = {}
        with self.pool.acquire() as c:
            cur = c.cursor()
            for i in range(0, len(ids), 100):
                chunk = ids[i:i + 100]
                binds = ",".join(f":b{j}" for j in range(len(chunk)))
                cur.execute(
                    f"SELECT sql_id, sql_text, command_type FROM {v} WHERE sql_id IN ({binds})",
                    {f"b{j}": s for j, s in enumerate(chunk)})
                for sid, txt, cmd in cur:
                    found[sid] = (sid, txt, COMMANDS.get(cmd))
        # remember misses too so we don't re-query them forever
        items = [found.get(s, (s, None, None)) for s in ids]
        store.save_sqltext(self.name_, items)

    def sql_details(self, sql_id: str) -> dict:
        if self.pool is None:
            return {"error": "нет подключения к БД"}
        out = {}
        v = "gv$sqlarea" if self.rac else "v$sqlarea"
        with self.pool.acquire() as c:
            cur = c.cursor()
            cur.execute(f"""SELECT sql_fulltext, parsing_schema_name, executions, elapsed_time,
                                   cpu_time, buffer_gets, disk_reads, rows_processed,
                                   plan_hash_value, module, first_load_time, last_active_time
                              FROM {v} WHERE sql_id = :s AND ROWNUM = 1""", s=sql_id)
            r = cur.fetchone()
            if r:
                text = r[0].read() if hasattr(r[0], "read") else r[0]
                ex = r[2] or 0
                out = {
                    "text": text, "schema": r[1], "executions": ex,
                    "elapsed_s": round((r[3] or 0) / 1e6, 3),
                    "elapsed_per_exec_ms": round((r[3] or 0) / 1e3 / ex, 3) if ex else None,
                    "cpu_s": round((r[4] or 0) / 1e6, 3), "buffer_gets": r[5], "disk_reads": r[6],
                    "rows": r[7], "plan_hash_value": r[8], "module": r[9],
                    "first_load_time": r[10], "last_active_time": str(r[11]) if r[11] else None,
                }
            try:
                cur.execute("SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:s, NULL, 'TYPICAL'))",
                            s=sql_id)
                out["plan"] = "\n".join(x[0] or "" for x in cur)
            except Exception as e:  # noqa: BLE001
                out["plan"] = f"План недоступен: {str(e).splitlines()[0]}"
        return out


LOGIN_TIMEOUT = 20  # seconds for the whole connect + first query from the login form

_HINTS = {
    "ORA-01017": "неверное имя пользователя или пароль",
    "ORA-28000": "учётная запись заблокирована",
    "ORA-01031": "недостаточно привилегий (для режима SYSDBA у пользователя должна быть роль SYSDBA)",
    "ORA-00942": "у пользователя нет доступа к V$-представлениям: выдайте SELECT_CATALOG_ROLE",
    "ORA-12514": "listener не знает такого сервиса: проверьте имя сервиса",
    "ORA-12505": "listener не знает такого SID: проверьте имя сервиса",
    "ORA-12541": "на этом хосте и порту нет listener'а Oracle",
    "ORA-12537": "соединение закрыто сервером: на этом порту не Oracle или доступ запрещён",
    "ORA-12170": "истекло время подключения: хост недоступен",
    "ORA-12545": "хост не найден",
    "ORA-12543": "хост недоступен",
    "DPY-3010": "для Oracle 11g нужен Oracle Client (толстый режим), а он не загружен: установите Oracle Instant "
                "Client 11.2/12.2 или укажите его папку в oracle_client_lib файла config.json",
}


def explain(err: str) -> str:
    for code, hint in _HINTS.items():
        if code in err:
            first = err.strip().splitlines()[0] if err.strip() else ""
            return f"{code}: {hint}" + (f" — {first}" if first and first != code else "")
    return err


_EZ = re.compile(r"^\s*(?://)?\[?([\w.\-:]+?)\]?(?::(\d+))?/([\w.\-$#]+)\s*$")
_SID = re.compile(r"^\s*([\w.\-]+):(\d+):([\w$#]+)\s*$")


def with_timeouts(dsn: str) -> str:
    """Connect timeouts, so an unresponsive host fails fast instead of hanging.

    host[:port]/service becomes a full descriptor: every client from 11.2 on and thin mode understand it,
    while Easy Connect Plus parameters (host/service?connect_timeout=…) need a 19c+ client; an older
    installed client (Windows 7) would take "service?connect_timeout=…" as the service name (ORA-12514)."""
    if dsn.startswith("(") or "?" in dsn:
        return dsn
    m, sid = _EZ.match(dsn), _SID.match(dsn)
    if m:
        host, port, service = m.groups()
        data = f"SERVICE_NAME={service}"
    elif sid:                                   # host:port:SID, as in SQL Developer
        host, port, name = sid.groups()
        data = f"SID={name}"
    else:
        return dsn
    return ("(DESCRIPTION=(CONNECT_TIMEOUT=15)(TRANSPORT_CONNECT_TIMEOUT=8)(RETRY_COUNT=0)"
            f"(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={port or 1521}))"
            f"(CONNECT_DATA=({data})))")


def test_login(dsn: str, user: str, password: str, mode: str = "") -> dict:
    """Open a single connection with the given credentials; raise on failure or timeout."""
    import concurrent.futures as cf
    oracledb = _oracle()
    params = {}
    if (mode or "").upper() == "SYSDBA":
        params["mode"] = oracledb.AUTH_MODE_SYSDBA

    def attempt():
        with oracledb.connect(user=user, password=password, dsn=with_timeouts(dsn), **params) as c:
            cur = c.cursor()
            cur.execute("SELECT i.instance_name, i.version FROM v$instance i")
            inst, ver = cur.fetchone()
            return {"instance": inst, "version": ver}

    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(attempt).result(timeout=LOGIN_TIMEOUT)
    except cf.TimeoutError:
        raise TimeoutError(f"Сервер БД не ответил за {LOGIN_TIMEOUT} с: проверьте хост и порт") from None
    finally:
        ex.shutdown(wait=False)


import datetime as _dt  # noqa: E402
_EPOCH = _dt.datetime(1970, 1, 1)


def _bg_name(program: str) -> str:
    # "oracle@host (DBW0)" -> "DBW0"
    if "(" in program and program.endswith(")"):
        return program[program.rfind("(") + 1:-1]
    return program


# --------------------------------------------------------------------------- Demo

_DEMO_SQL = [
    ("8gf2a1k9x0pqs", "SELECT", "SELECT o.order_id, o.status, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.created_at > :1"),
    ("1zq7b3mmc0rt2", "UPDATE", "UPDATE accounts SET balance = balance - :1 WHERE account_id = :2"),
    ("4kd9s0vvn1w8h", "INSERT", "INSERT INTO audit_log (ts, user_id, action, payload) VALUES (SYSTIMESTAMP, :1, :2, :3)"),
    ("9hx2c5pll3e6y", "SELECT", "SELECT /*+ FULL(t) */ SUM(amount) FROM transactions t WHERE trunc(tx_date) = trunc(SYSDATE)"),
    ("6ab0d7rrt4y1u", "PL/SQL EXECUTE", "BEGIN billing_pkg.close_period(:1); END;"),
    ("2mn8f6qwe5r3t", "SELECT", "SELECT * FROM products WHERE sku = :1"),
    ("7pp3g4zxc6v2b", "DELETE", "DELETE FROM session_cache WHERE expires_at < SYSDATE"),
    ("3yt5h8jkl7m0n", "SELECT", "SELECT COUNT(*) FROM inventory i, warehouses w WHERE i.wh_id = w.id AND w.region = :1"),
]
_DEMO_PROFILES = {
    # sql_id -> [(wait_class, event, weight)]
    "8gf2a1k9x0pqs": [("CPU", "ON CPU", 5), ("User I/O", "db file sequential read", 4)],
    "1zq7b3mmc0rt2": [("CPU", "ON CPU", 2), ("Application", "enq: TX - row lock contention", 3), ("Commit", "log file sync", 2)],
    "4kd9s0vvn1w8h": [("CPU", "ON CPU", 2), ("Commit", "log file sync", 3), ("Concurrency", "buffer busy waits", 1)],
    "9hx2c5pll3e6y": [("User I/O", "direct path read", 6), ("CPU", "ON CPU", 3)],
    "6ab0d7rrt4y1u": [("CPU", "ON CPU", 4), ("Network", "SQL*Net more data to client", 1), ("Other", "latch free", 1)],
    "2mn8f6qwe5r3t": [("CPU", "ON CPU", 6), ("Concurrency", "cursor: pin S wait on X", 1)],
    "7pp3g4zxc6v2b": [("User I/O", "db file scattered read", 3), ("Configuration", "log file switch completion", 1)],
    "3yt5h8jkl7m0n": [("CPU", "ON CPU", 3), ("User I/O", "db file parallel read", 3)],
}
_DEMO_USERS = [("APP", "JDBC Thin Client", "OrderService", "app-srv-01"),
               ("APP", "JDBC Thin Client", "BillingService", "app-srv-02"),
               ("REPORTS", "sqlplus@rep01", "SQL*Plus", "rep01"),
               ("ETL", "python3.11", "nightly_load", "etl-node")]


def _h(s: str) -> int:
    return zlib.crc32(s.encode())


class DemoCollector(Collector):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.info = {"instance": "DEMO1", "host": "demo-host", "version": "19.0.0.0.0",
                     "db_name": "DEMO", "cpu_count": 8}
        self.sessions = {}
        self.rng = random.Random(42)

    def run(self):
        # backfill some history so the UI has something to show
        now = int(time.time())
        hours = float(self.cfg.get("backfill_hours", 3))
        # история пачками по 5 минут: по записи на секунду медленный диск (PythonAnywhere) пишет её часами
        start = now - int(hours * 3600)
        for chunk in range(start, now, 300):
            ticks, rows = [], []
            for ts in range(chunk, min(chunk + 300, now)):
                self._tick(ts, (ticks, rows))
            store.write_samples(self.name_, ticks, rows)
            self.last_sample = ticks[-1]
        self._demo_snapshots_at_start(now, hours)
        self.status = "running"
        buf, flushed = ([], []), time.time()
        while not self._stop.is_set():
            t0 = time.time()
            ts = int(t0)
            self._tick(ts, buf)
            load = getattr(self, "demo_load", None)      # имитация нагрузочного теста (app/demo_load.py)
            if load is not None:
                buf[1].extend(load.rows(ts, self.name_))
            if t0 - flushed >= 5:                         # запись пачкой раз в 5 с: на медленном диске
                self._flush(buf)                          # запись каждую секунду не успевает
                buf, flushed = ([], []), t0
            self._maybe_demo_snapshot(t0)
            self._stop.wait(max(0.05, self.interval - (time.time() - t0)))
        self._flush(buf)

    def _flush(self, buf):
        if not buf[0]:
            return
        store.write_samples(self.name_, buf[0], buf[1])
        self.last_sample = buf[0][-1]
        if not hasattr(self, "_texts_saved"):
            store.save_sqltext(self.name_, [(s, t, o) for s, o, t in _DEMO_SQL])
            self._texts_saved = True

    # --- synthetic statistics snapshots, so the performance report works in the demo
    snapshot_minutes = demo_snap.SNAPSHOT_MINUTES

    def _demo_snapshots_at_start(self, now: int, hours: float):
        if store.last_snapshot_ts(self.name_) >= now - 600:
            return
        span = hours * 3600
        for i in range(demo_snap.BACKFILL_SNAPSHOTS):
            self._save_demo_snapshot(now - span + span * i / demo_snap.BACKFILL_SNAPSHOTS, False)
        self._next_snap = now + self.snapshot_minutes * 60

    def _maybe_demo_snapshot(self, now: float):
        if now >= getattr(self, "_next_snap", 0):
            self._next_snap = now + self.snapshot_minutes * 60
            self._save_demo_snapshot(now, False)

    def take_snapshot(self, manual: bool = False) -> int:
        return self._save_demo_snapshot(time.time(), manual)

    def _save_demo_snapshot(self, ts: float, manual: bool) -> int:
        from . import awr
        return awr.save(self.name_, demo_snap.oracle(demo_snap.startup_for(ts), ts, _DEMO_SQL, _DEMO_PROFILES, _DEMO_USERS), manual)

    def _load(self, ts):
        base = 4 + 3 * math.sin(ts / 900.0) + 2 * math.sin(ts / 137.0)
        spike = 9 if (ts // 600) % 6 == 0 and (ts % 600) < 180 else 0  # periodic incident
        return max(0.5, base + spike), spike > 0

    def _tick(self, ts, out=None):
        """Выборка за секунду ts; out=(ticks, rows) — накопить для записи пачкой (история при старте)."""
        rng = self.rng
        load, incident = self._load(ts)
        n = max(0, int(rng.gauss(load, 1.2)))
        rows = []
        for i in range(n):
            if incident and rng.random() < 0.6:
                sql_id = "1zq7b3mmc0rt2"
            else:
                sql_id = rng.choice(_DEMO_SQL)[0]
            opname = next(o for s, o, _ in _DEMO_SQL if s == sql_id)
            prof = _DEMO_PROFILES[sql_id]
            wc, ev, _ = rng.choices(prof, weights=[p[2] for p in prof])[0]
            sid = 100 + (_h(f'{sql_id}{i % 3}') % 400)
            user, prog, mod, mach = _DEMO_USERS[_h(sql_id) % len(_DEMO_USERS)]
            blocker = 37 if ev.startswith("enq: TX") else None
            rows.append({"target": self.name_, "ts": ts, "inst_id": 1, "sid": sid, "serial": 1000 + sid,
                         "username": user, "program": prog, "module": mod, "machine": mach,
                         "sql_id": sql_id, "sql_opname": opname, "plan_hash": _h(sql_id),
                         "event": ev, "wait_class": wc, "blocking_inst": 1 if blocker else None,
                         "blocking_sid": blocker, "session_type": "USER"})
        if rng.random() < 0.3:
            rows.append({"target": self.name_, "ts": ts, "inst_id": 1, "sid": 2, "serial": 1,
                         "username": "(LGWR)", "program": "oracle@demo-host (LGWR)", "event": "log file parallel write",
                         "wait_class": "System I/O", "session_type": "BACKGROUND"})
        if out is not None:                          # история при старте: нагрузочного теста в прошлом не было
            out[0].append(ts)
            out[1].extend(rows)
            return
        load = getattr(self, "demo_load", None)      # имитация нагрузочного теста (app/demo_load.py)
        if load is not None:
            rows += load.rows(ts, self.name_)
        store.write_samples(self.name_, [ts], rows)
        if not hasattr(self, "_texts_saved"):
            store.save_sqltext(self.name_, [(s, t, o) for s, o, t in _DEMO_SQL])
            self._texts_saved = True
        self.last_sample = ts

    def sql_details(self, sql_id):
        for s, o, t in _DEMO_SQL:
            if s == sql_id:
                return {"text": t, "schema": "APP", "executions": 123456, "elapsed_s": 987.6,
                        "elapsed_per_exec_ms": 8.0, "cpu_s": 512.3, "buffer_gets": 98765432,
                        "disk_reads": 123456, "rows": 654321, "plan_hash_value": _h(s),
                        "plan": "Демо-режим: план выполнения доступен только для реальной БД."}
        return {}


def make(cfg: dict) -> Collector:
    t = cfg.get("type", "oracle")
    if t == "demo":
        return DemoCollector(cfg)
    if t == "oracle":
        return OracleCollector(cfg)
    raise ValueError(f"unknown target type {t}")
