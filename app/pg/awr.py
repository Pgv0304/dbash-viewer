"""AWR-like performance reports for PostgreSQL.

Snapshots of the cumulative statistics views (pg_stat_database, pg_stat_bgwriter/checkpointer, WAL position,
pg_stat_statements, pg_stat_user_tables/indexes of the monitored database) are taken periodically and on demand,
stored compressed in the local SQLite store, and a report is built from the difference between two snapshots:
load profile, efficiency, top waits (from the sampled session history - PostgreSQL itself does not time
its waits), top SQL, tables and indexes, databases, instance state and settings - followed by rule-based
tuning recommendations (awr_advisor.py).
"""
from __future__ import annotations
import json
import time
import zlib

from . import collectors
from .. import store

SQL_TOP = 150       # statements kept per ranking in a snapshot
TABLE_TOP = 400     # tables of the monitored database kept in a snapshot (largest first)

SETTINGS = ("shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem", "max_connections",
            "max_wal_size", "min_wal_size", "checkpoint_timeout", "checkpoint_completion_target", "wal_buffers",
            "random_page_cost", "seq_page_cost", "effective_io_concurrency", "default_statistics_target",
            "autovacuum", "autovacuum_max_workers", "autovacuum_naptime", "autovacuum_vacuum_scale_factor",
            "autovacuum_analyze_scale_factor", "autovacuum_vacuum_cost_limit", "autovacuum_vacuum_cost_delay",
            "track_io_timing", "track_activity_query_size", "shared_preload_libraries", "synchronous_commit",
            "wal_level", "max_worker_processes", "max_parallel_workers", "max_parallel_workers_per_gather",
            "jit", "huge_pages", "idle_in_transaction_session_timeout", "statement_timeout",
            "log_min_duration_statement", "compute_query_id", "temp_buffers", "bgwriter_lru_maxpages",
            "bgwriter_delay", "fsync", "full_page_writes", "wal_compression", "log_autovacuum_min_duration",
            "server_version", "block_size", "lock_timeout", "deadlock_timeout", "max_locks_per_transaction")


# --------------------------------------------------------------------------- capture

def _rows(cur, sql, params=None):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _try(cur, sql, fallback=None, params=None):
    try:
        return _rows(cur, sql, params)
    except Exception:  # noqa: BLE001 - missing view/column on this version or no privilege
        if fallback:
            return _try(cur, fallback, params=params)
        return []


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    if hasattr(v, "total_seconds"):
        return v.total_seconds()
    return str(v)


def capture(conn, cpu_count: int = 0) -> dict:
    """Collect one snapshot of cumulative statistics through an open psycopg connection (autocommit)."""
    cur = conn.cursor()
    snap = {"taken_at": time.time()}
    inst = _rows(cur, """SELECT current_setting('server_version_num')::int AS version_num,
                                current_setting('server_version') AS version, version() AS banner,
                                current_database() AS db_name,
                                coalesce(nullif(current_setting('cluster_name', true), ''), 'postgres') AS cluster,
                                coalesce(host(inet_server_addr()), 'local') AS host,
                                pg_postmaster_start_time()::text AS startup_time, now()::text AS db_now,
                                pg_is_in_recovery() AS in_recovery,
                                current_setting('max_connections')::int AS max_connections,
                                current_setting('superuser_reserved_connections')::int AS reserved,
                                current_setting('block_size')::int AS block_size""")[0]
    inst["cpu_count"] = cpu_count
    vnum = inst["version_num"]
    snap["instance"] = inst
    snap["databases"] = _rows(cur, """
        SELECT d.datname, d.numbackends, d.xact_commit, d.xact_rollback, d.blks_read, d.blks_hit,
               d.tup_returned, d.tup_fetched, d.tup_inserted, d.tup_updated, d.tup_deleted, d.conflicts,
               d.temp_files, d.temp_bytes, d.deadlocks, d.blk_read_time, d.blk_write_time,
               d.stats_reset::text AS stats_reset,
               CASE WHEN has_database_privilege(d.datname, 'CONNECT') THEN pg_database_size(d.datname) END AS size,
               age(db.datfrozenxid) AS xid_age
          FROM pg_stat_database d JOIN pg_database db ON db.oid = d.datid
         WHERE d.datname IS NOT NULL AND NOT db.datistemplate""")
    bg = _try(cur, "SELECT * FROM pg_stat_bgwriter")
    snap["bgwriter"] = bg[0] if bg else {}
    cp = _try(cur, "SELECT * FROM pg_stat_checkpointer") if vnum >= 170000 else []   # PostgreSQL 17+
    if cp:
        c = cp[0]
        snap["bgwriter"].update({"checkpoints_timed": c.get("num_timed"), "checkpoints_req": c.get("num_requested"),
                                 "checkpoint_write_time": c.get("write_time"),
                                 "checkpoint_sync_time": c.get("sync_time"),
                                 "buffers_checkpoint": c.get("buffers_written")})
    io = _try(cur, """SELECT sum(writes) FILTER (WHERE backend_type = 'client backend') AS backend_writes,
                             sum(fsyncs) FILTER (WHERE backend_type = 'client backend') AS backend_fsyncs
                        FROM pg_stat_io""") if vnum >= 160000 else []
    if io and snap["bgwriter"].get("buffers_backend") is None:
        snap["bgwriter"]["buffers_backend"] = io[0]["backend_writes"]
        snap["bgwriter"]["buffers_backend_fsync"] = io[0]["backend_fsyncs"]
    wal = _try(cur, """SELECT pg_wal_lsn_diff(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn()
                                                    ELSE pg_current_wal_lsn() END, '0/0')::bigint AS bytes""")
    snap["wal"] = {"bytes": wal[0]["bytes"] if wal else None}
    wst = _try(cur, "SELECT * FROM pg_stat_wal")                       # PostgreSQL 14+
    if wst:
        snap["wal"].update({k: wst[0].get(k) for k in ("wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full")})
    snap["settings"] = {r["name"]: r for r in _rows(cur, """
        SELECT name, setting, unit, source, boot_val, short_desc
          FROM pg_settings
         WHERE name = ANY(%s) OR source NOT IN ('default', 'override')""", (list(SETTINGS),))}
    snap["extensions"] = [r["extname"] for r in _try(cur, "SELECT extname FROM pg_extension")]
    snap["statements"], snap["statements_note"] = _statements(cur, vnum, snap["extensions"])
    snap["tables"] = _try(cur, f"""
        SELECT s.relid, s.schemaname, s.relname, s.seq_scan, s.seq_tup_read, s.idx_scan, s.idx_tup_fetch,
               s.n_tup_ins, s.n_tup_upd, s.n_tup_hot_upd, s.n_tup_del, s.n_live_tup, s.n_dead_tup,
               s.n_mod_since_analyze, s.last_vacuum::text AS last_vacuum, s.last_autovacuum::text AS last_autovacuum,
               s.last_analyze::text AS last_analyze, s.last_autoanalyze::text AS last_autoanalyze,
               s.autovacuum_count, s.autoanalyze_count,
               io.heap_blks_read, io.heap_blks_hit, io.idx_blks_read, io.idx_blks_hit,
               pg_relation_size(s.relid) AS size, pg_total_relation_size(s.relid) AS total_size
          FROM pg_stat_user_tables s JOIN pg_statio_user_tables io ON io.relid = s.relid
         WHERE s.schemaname NOT LIKE 'pg_temp%%' AND s.schemaname NOT LIKE 'pg_toast_temp%%'
         ORDER BY pg_total_relation_size(s.relid) DESC
         LIMIT {TABLE_TOP}""")
    snap["unused_indexes"] = _try(cur, """
        SELECT s.schemaname, s.relname AS table_name, s.indexrelname AS index_name, s.idx_scan,
               pg_relation_size(s.indexrelid) AS size
          FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid
         WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
           AND pg_relation_size(s.indexrelid) > 1048576
         ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT 30""")
    snap["activity"] = _rows(cur, """
        SELECT coalesce(state, backend_type) AS state, count(*) AS sessions,
               count(*) FILTER (WHERE wait_event_type = 'Lock') AS lock_waits,
               max(extract(epoch FROM now() - xact_start))::int AS oldest_xact_s,
               max(extract(epoch FROM now() - state_change))::int AS longest_in_state_s
          FROM pg_stat_activity
         WHERE backend_type = 'client backend'
         GROUP BY 1""")
    snap["long_xacts"] = _rows(cur, """
        SELECT pid, usename, datname, application_name, state,
               extract(epoch FROM now() - xact_start)::int AS xact_s, left(query, 200) AS query
          FROM pg_stat_activity
         WHERE xact_start IS NOT NULL AND backend_type = 'client backend' AND pid <> pg_backend_pid()
           AND now() - xact_start > interval '5 minutes'
         ORDER BY xact_start LIMIT 10""")
    snap["slots"] = _try(cur, """
        SELECT slot_name, slot_type, active,
               pg_wal_lsn_diff(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn()
                                    ELSE pg_current_wal_lsn() END, restart_lsn)::bigint AS retained_bytes
          FROM pg_replication_slots""")
    snap["replication"] = _try(cur, """
        SELECT application_name, client_addr::text AS client, state, sync_state,
               pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint AS lag_bytes
          FROM pg_stat_replication""")
    return json.loads(json.dumps(snap, default=_jsonable))


def _statements(cur, vnum, extensions):
    if "pg_stat_statements" not in extensions:
        return [], "Расширение pg_stat_statements не установлено в этой базе"
    total = "total_exec_time" if vnum >= 130000 else "total_time"
    io_time = ("shared_blk_read_time + shared_blk_write_time" if vnum >= 170000
               else "blk_read_time + blk_write_time")
    try:
        rows = _rows(cur, f"""
            SELECT s.queryid::text AS queryid, d.datname, r.rolname, left(s.query, 300) AS query,
                   s.calls, s.{total} AS total_ms, s.rows, s.shared_blks_hit, s.shared_blks_read,
                   s.shared_blks_dirtied, s.shared_blks_written, s.temp_blks_read, s.temp_blks_written,
                   {io_time} AS io_ms
              FROM (SELECT *, row_number() OVER (ORDER BY {total} DESC) r1,
                              row_number() OVER (ORDER BY calls DESC) r2,
                              row_number() OVER (ORDER BY shared_blks_read DESC) r3,
                              row_number() OVER (ORDER BY temp_blks_written DESC) r4,
                              row_number() OVER (ORDER BY shared_blks_hit DESC) r5
                      FROM pg_stat_statements) s
              LEFT JOIN pg_database d ON d.oid = s.dbid
              LEFT JOIN pg_roles r ON r.oid = s.userid
             WHERE least(r1, r2, r3, r4, r5) <= {SQL_TOP}""")
    except Exception as e:  # noqa: BLE001 - installed, but not in shared_preload_libraries
        return [], "pg_stat_statements не работает: " + (str(e).strip().splitlines() or ["?"])[0]
    for r in rows:
        r["fingerprint"] = collectors.fingerprint(r["query"] or "")
    return rows, None


def save(target: str, snap: dict, manual: bool = False) -> int:
    blob = zlib.compress(json.dumps(snap, ensure_ascii=False).encode(), 6)
    return store.save_snapshot(target, int(snap["taken_at"]), str(snap["instance"]["startup_time"]), blob, manual)


def load(target: str, snap_id: int) -> dict | None:
    row = store.get_snapshot(target, snap_id)
    if row is None:
        return None
    d = json.loads(zlib.decompress(row["data"]).decode())
    d["snap_id"] = row["snap_id"]
    return d


# --------------------------------------------------------------------------- report

def _pct(a, b):
    return round(100.0 * a / b, 2) if b else None


def _d(e, b, k):
    return max(0, (e.get(k) or 0) - (b.get(k) or 0))


def _mb(v):
    return round((v or 0) / 1048576, 1)


class Report:
    def __init__(self, target: str, b: dict, e: dict):
        self.target, self.b, self.e = target, b, e
        self.elapsed = max(1.0, e["taken_at"] - b["taken_at"])
        self.inst = e["instance"]
        self.block = int(self.inst.get("block_size") or 8192)
        bdb = {r["datname"]: r for r in b.get("databases", [])}
        self.db = {}                                       # per-database deltas
        for r in e.get("databases", []):
            p = bdb.get(r["datname"], {})
            if p and p.get("stats_reset") != r.get("stats_reset"):
                p = {}                                     # statistics of this database were reset in between
            self.db[r["datname"]] = {k: _d(r, p, k) for k in (
                "xact_commit", "xact_rollback", "blks_read", "blks_hit", "tup_returned", "tup_fetched",
                "tup_inserted", "tup_updated", "tup_deleted", "conflicts", "temp_files", "temp_bytes", "deadlocks",
                "blk_read_time", "blk_write_time")}
            self.db[r["datname"]].update(size=r.get("size"), xid_age=r.get("xid_age"),
                                         numbackends=r.get("numbackends"))
        self.tot = {k: sum(v[k] for v in self.db.values()) for k in (
            "xact_commit", "xact_rollback", "blks_read", "blks_hit", "tup_returned", "tup_fetched",
            "tup_inserted", "tup_updated", "tup_deleted", "temp_files", "temp_bytes", "deadlocks",
            "blk_read_time", "blk_write_time", "conflicts")}
        self.txn = self.tot["xact_commit"] + self.tot["xact_rollback"]
        self.bg = {k: _d(e.get("bgwriter", {}), b.get("bgwriter", {}), k) for k in (
            "checkpoints_timed", "checkpoints_req", "checkpoint_write_time", "checkpoint_sync_time",
            "buffers_checkpoint", "buffers_clean", "maxwritten_clean", "buffers_backend",
            "buffers_backend_fsync", "buffers_alloc")}
        self.wal_bytes = _d(e.get("wal", {}), b.get("wal", {}), "bytes")
        self.cpus = int(self.inst.get("cpu_count") or 0)
        # sampled session history of the interval: PostgreSQL does not time waits, the samples do
        db = store.conn()
        t0, t1 = int(b["taken_at"]), int(e["taken_at"])
        self.ticks = db.execute("SELECT COUNT(*) FROM ticks WHERE target=? AND ts>=? AND ts<?",
                                (target, t0, t1)).fetchone()[0] or 0
        self.samples = db.execute("SELECT COUNT(*) FROM samples WHERE target=? AND ts>=? AND ts<? "
                                  "AND session_type='USER'", (target, t0, t1)).fetchone()[0] or 0
        self.ash_events = [dict(r) for r in db.execute(
            """SELECT event, wait_class, COUNT(*) samples FROM samples
                WHERE target=? AND ts>=? AND ts<? AND session_type='USER'
                GROUP BY event, wait_class ORDER BY samples DESC""", (target, t0, t1))]
        self.ash_sql = [dict(r) for r in db.execute(
            """SELECT s.sql_id, MAX(s.sql_opname) op, MAX(s.module) db, COUNT(*) samples,
                      SUM(CASE WHEN s.wait_class = 'CPU' THEN 1 ELSE 0 END) cpu,
                      SUM(CASE WHEN s.wait_class = 'IO' THEN 1 ELSE 0 END) io,
                      SUM(CASE WHEN s.wait_class = 'Lock' THEN 1 ELSE 0 END) lock,
                      (SELECT text FROM sqltext t WHERE t.target = s.target AND t.sql_id = s.sql_id) text
                 FROM samples s
                WHERE s.target=? AND s.ts>=? AND s.ts<? AND s.sql_id IS NOT NULL
                GROUP BY s.sql_id ORDER BY samples DESC LIMIT 25""", (target, t0, t1))]
        # "DB time" of the interval: every sample of an active session stands for ~1 second
        tick_len = self.elapsed / self.ticks if self.ticks else 1.0
        self.db_time = self.samples * min(tick_len, 60)
        self.recs = []

    def setting(self, name, default=None):
        s = self.e.get("settings", {}).get(name)
        return s["setting"] if s else default

    def setting_bytes(self, name):
        """Value of a memory setting in bytes (pg_settings gives it in units such as 8kB or kB)."""
        s = self.e.get("settings", {}).get(name)
        if not s:
            return None
        try:
            v = float(s["setting"])
        except (TypeError, ValueError):
            return None
        unit = (s.get("unit") or "").strip()
        mult = {"B": 1, "kB": 1024, "MB": 1048576, "GB": 1073741824, "8kB": 8192, "16kB": 16384,
                "32kB": 32768, "16MB": 16 * 1048576}.get(unit)
        if mult is None:
            return None
        return v * mult if v >= 0 else v

    def header(self):
        i = self.inst
        act = {r["state"]: r for r in self.e.get("activity", [])}
        total = sum(r["sessions"] for r in act.values())
        return {
            "db_name": i["db_name"], "instance": i["cluster"], "host": i["host"], "version": i["version"],
            "banner": i.get("banner"), "startup_time": i["startup_time"], "cpus": self.cpus or None,
            "begin": {"id": self.b["snap_id"], "ts": self.b["taken_at"]},
            "end": {"id": self.e["snap_id"], "ts": self.e["taken_at"]},
            "elapsed_min": round(self.elapsed / 60, 1), "db_time_min": round(self.db_time / 60, 2),
            "aas": round(self.samples / self.ticks, 2) if self.ticks else None,
            "sampled_pct": round(100.0 * self.ticks * (self.elapsed / max(1, self.ticks)) / self.elapsed)
            if self.ticks else 0,
            "ticks": self.ticks,
            "sessions": total, "active_sessions": (act.get("active") or {}).get("sessions", 0),
            "max_connections": i.get("max_connections"), "in_recovery": i.get("in_recovery"),
        }

    def load_profile(self):
        t, bg = self.tot, self.bg
        items = [
            ("Транзакции", self.txn), ("Фиксации (commit)", t["xact_commit"]), ("Откаты (rollback)", t["xact_rollback"]),
            ("Блоки из кэша (hit)", t["blks_hit"]), ("Блоки с диска (read)", t["blks_read"]),
            ("Строк возвращено (seq/bitmap)", t["tup_returned"]), ("Строк выбрано по индексам", t["tup_fetched"]),
            ("Строк вставлено", t["tup_inserted"]), ("Строк изменено", t["tup_updated"]),
            ("Строк удалено", t["tup_deleted"]), ("WAL, байт", self.wal_bytes),
            ("Временные файлы", t["temp_files"]), ("Временные файлы, байт", t["temp_bytes"]),
            ("Взаимоблокировки", t["deadlocks"]),
            ("Контрольные точки по времени", bg["checkpoints_timed"]),
            ("Контрольные точки по требованию", bg["checkpoints_req"]),
            ("Буферов записано: checkpointer", bg["buffers_checkpoint"]),
            ("Буферов записано: bgwriter", bg["buffers_clean"]),
            ("Буферов записано: обслуживающими процессами", bg["buffers_backend"]),
        ]
        return [{"name": n, "per_sec": round(v / self.elapsed, 2), "per_txn": round(v / self.txn, 2) if self.txn else None,
                 "total": v} for n, v in items]

    def efficiency(self):
        t, bg = self.tot, self.bg
        hit, read = t["blks_hit"], t["blks_read"]
        tb = self.tables_delta()
        seq = sum(r["seq_scan"] for r in tb)
        idx = sum(r["idx_scan"] for r in tb)
        upd = sum(r["n_tup_upd"] for r in tb)
        hot = sum(r["n_tup_hot_upd"] for r in tb)
        cps = bg["checkpoints_timed"] + bg["checkpoints_req"]
        written = bg["buffers_checkpoint"] + bg["buffers_clean"] + bg["buffers_backend"]
        return [
            {"name": "Попадание в кэш (Cache Hit %)", "value": _pct(hit, hit + read), "good": 99},
            {"name": "Фиксации транзакций (Commit %)", "value": _pct(t["xact_commit"], self.txn), "good": 95},
            {"name": "Доступ по индексам (Index Scan %)", "value": _pct(idx, idx + seq), "good": 90},
            {"name": "HOT-обновления (HOT Update %)", "value": _pct(hot, upd), "good": 50},
            {"name": "Контрольные точки по времени %", "value": _pct(bg["checkpoints_timed"], cps), "good": 90},
            {"name": "Запись фоновыми процессами %", "value": _pct(written - bg["buffers_backend"], written),
             "good": 90},
        ]

    def top_events(self):
        rows = []
        for r in self.ash_events:
            rows.append({"event": r["event"], "wait_class": r["wait_class"], "samples": r["samples"],
                         "time_s": round(r["samples"] * (self.db_time / self.samples), 1) if self.samples else 0,
                         "pct_db_time": _pct(r["samples"], self.samples)})
        return rows

    def wait_classes(self):
        agg = {}
        for r in self.ash_events:
            agg[r["wait_class"]] = agg.get(r["wait_class"], 0) + r["samples"]
        return sorted([{"wait_class": k, "samples": v, "time_s": round(v * (self.db_time / self.samples), 1)
                        if self.samples else 0, "pct_db_time": _pct(v, self.samples)} for k, v in agg.items()],
                      key=lambda x: x["samples"], reverse=True)

    def sql(self):
        if not self.e.get("statements"):
            return {"source": "ash", "note": self.e.get("statements_note"), "ash": self._ash_sql()}
        use_qid = int(self.inst["version_num"]) >= 140000 and (self.setting("compute_query_id") or "off") != "off"
        prev = {(r["queryid"], r["datname"], r["rolname"]): r for r in self.b.get("statements", [])}
        keys = ("calls", "total_ms", "rows", "shared_blks_hit", "shared_blks_read", "shared_blks_dirtied",
                "shared_blks_written", "temp_blks_read", "temp_blks_written", "io_ms")
        rows = []
        for r in self.e.get("statements", []):
            p = prev.get((r["queryid"], r["datname"], r["rolname"]), {})
            d = {k: max(0.0, (r.get(k) or 0) - (p.get(k) or 0)) for k in keys}
            if not d["calls"] and not d["total_ms"]:
                continue
            calls = d["calls"]
            gets = d["shared_blks_hit"] + d["shared_blks_read"]
            rows.append({
                "sql_id": r["queryid"] if use_qid else r["fingerprint"], "queryid": r["queryid"],
                "module": r.get("datname"), "schema": r.get("rolname"), "text": r.get("query"),
                "executions": int(calls), "elapsed_s": round(d["total_ms"] / 1e3, 3),
                "ela_per_exec_ms": round(d["total_ms"] / calls, 3) if calls else None,
                "rows": int(d["rows"]), "gets": int(gets), "reads": int(d["shared_blks_read"]),
                "hit_pct": _pct(d["shared_blks_hit"], gets), "temp_blks": int(d["temp_blks_written"]),
                "io_s": round(d["io_ms"] / 1e3, 3), "dirtied": int(d["shared_blks_dirtied"]),
            })
        total_ms = sum(r["elapsed_s"] for r in rows)
        reads = sum(r["reads"] for r in rows)
        for r in rows:
            r["pct_time"] = _pct(r["elapsed_s"], total_ms)
            r["pct_reads"] = _pct(r["reads"], reads)

        def top(key):
            return [r for r in sorted(rows, key=lambda x: x[key] or 0, reverse=True) if r[key]][:15]
        return {"source": "pg_stat_statements", "elapsed": top("elapsed_s"), "executions": top("executions"),
                "reads": top("reads"), "gets": top("gets"), "temp": top("temp_blks"),
                "mean": top("ela_per_exec_ms"), "ash": self._ash_sql()}

    def _ash_sql(self):
        tot = self.samples or 1
        return [{**r, "pct": _pct(r["samples"], tot),
                 "aas": round(r["samples"] / self.ticks, 3) if self.ticks else None} for r in self.ash_sql]

    def tables_delta(self):
        if hasattr(self, "_tables"):
            return self._tables
        prev = {r["relid"]: r for r in self.b.get("tables", [])}
        out = []
        for r in self.e.get("tables", []):
            p = prev.get(r["relid"], {})
            d = {k: _d(r, p, k) for k in ("seq_scan", "seq_tup_read", "idx_scan", "idx_tup_fetch", "n_tup_ins",
                                          "n_tup_upd", "n_tup_hot_upd", "n_tup_del", "heap_blks_read",
                                          "heap_blks_hit", "idx_blks_read", "idx_blks_hit")}
            live, dead = r.get("n_live_tup") or 0, r.get("n_dead_tup") or 0
            d.update({"table": f'{r["schemaname"]}.{r["relname"]}', "size_mb": _mb(r.get("size")),
                      "total_mb": _mb(r.get("total_size")), "live": live, "dead": dead,
                      "dead_pct": _pct(dead, live + dead), "mod_since_analyze": r.get("n_mod_since_analyze"),
                      "last_autovacuum": r.get("last_autovacuum") or r.get("last_vacuum"),
                      "last_autoanalyze": r.get("last_autoanalyze") or r.get("last_analyze"),
                      "modified": d["n_tup_ins"] + d["n_tup_upd"] + d["n_tup_del"]})
            out.append(d)
        self._tables = out
        return out

    def tables(self):
        rows = self.tables_delta()

        def top(key, cond=lambda r: True):
            return [r for r in sorted(rows, key=lambda x: x[key] or 0, reverse=True) if r[key] and cond(r)][:15]
        return {"db": self.inst["db_name"],
                "seq_tup_read": top("seq_tup_read"), "heap_blks_read": top("heap_blks_read"),
                "modified": top("modified"), "dead": top("dead", lambda r: r["dead"] >= 1000),
                "unused_indexes": self.e.get("unused_indexes", [])}

    def databases(self):
        out = []
        for name, d in self.db.items():
            txn = d["xact_commit"] + d["xact_rollback"]
            out.append({"datname": name, "size_mb": _mb(d.get("size")), "backends": d.get("numbackends"),
                        "tps": round(txn / self.elapsed, 2), "commits": d["xact_commit"],
                        "rollbacks": d["xact_rollback"], "hit_pct": _pct(d["blks_hit"], d["blks_hit"] + d["blks_read"]),
                        "reads": d["blks_read"], "temp_mb": _mb(d["temp_bytes"]), "deadlocks": d["deadlocks"],
                        "xid_age": d.get("xid_age")})
        return sorted(out, key=lambda x: (x["tps"], x["size_mb"]), reverse=True)

    def instance(self):
        bg = self.bg
        cps = bg["checkpoints_timed"] + bg["checkpoints_req"]
        act = self.e.get("activity", [])
        return {
            "connections": sum(r["sessions"] for r in act), "max_connections": self.inst.get("max_connections"),
            "by_state": act, "long_xacts": self.e.get("long_xacts", []),
            "checkpoints": {"timed": bg["checkpoints_timed"], "requested": bg["checkpoints_req"],
                            "per_hour": round(cps * 3600 / self.elapsed, 1),
                            "write_s": round(bg["checkpoint_write_time"] / 1e3, 1),
                            "sync_s": round(bg["checkpoint_sync_time"] / 1e3, 1),
                            "maxwritten_clean": bg["maxwritten_clean"],
                            "backend_fsync": bg["buffers_backend_fsync"]},
            "wal_mb_per_hour": round(self.wal_bytes / 1048576 * 3600 / self.elapsed, 1),
            "slots": self.e.get("slots", []), "replication": self.e.get("replication", []),
            "extensions": self.e.get("extensions", []),
        }

    def params(self):
        out = []
        for name, s in sorted(self.e.get("settings", {}).items()):
            out.append({"name": name, "value": s["setting"] + (f' {s["unit"]}' if s.get("unit") else ""),
                        "default": s.get("source") in ("default", "override"), "source": s.get("source"),
                        "desc": s.get("short_desc")})
        return out

    def build(self):
        from .awr_advisor import advise
        rep = {
            "header": self.header(), "load_profile": self.load_profile(), "efficiency": self.efficiency(),
            "top_events": self.top_events()[:10], "events": self.top_events()[:40],
            "wait_classes": self.wait_classes(), "sql": self.sql(), "tables": self.tables(),
            "databases": self.databases(), "instance": self.instance(), "params": self.params(),
        }
        rep["recommendations"] = advise(self, rep)
        return rep


def report(target: str, begin_id: int, end_id: int) -> dict:
    b, e = load(target, begin_id), load(target, end_id)
    if b is None or e is None:
        raise ValueError("Снимок не найден")
    if b["taken_at"] > e["taken_at"]:
        b, e = e, b
    if b["snap_id"] == e["snap_id"]:
        raise ValueError("Выберите два разных снимка")
    if str(b["instance"]["startup_time"]) != str(e["instance"]["startup_time"]):
        raise ValueError("Между снимками сервер PostgreSQL перезапускался — накопленная статистика обнулилась. "
                         "Выберите снимки после последнего запуска.")
    return Report(target, b, e).build()
