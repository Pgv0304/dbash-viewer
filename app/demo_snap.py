"""Synthetic statistics snapshots for the demo databases, so the performance report works in the demo.

Cumulative counters grow with the same load curve that the demo collectors draw on the activity chart
(4 + 3·sin(t/900) + 2·sin(t/137) active sessions), so a report between two snapshots matches the picture:
DB time is the integral of that curve, and everything else is derived from it with fixed proportions.
Snapshots have the same shape as the real ones (app/ora/awr.py and app/pg/awr.py capture()), so the
report and the advisor need no changes.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

SNAPSHOT_MINUTES = 20            # automatic demo snapshots
BACKFILL_SNAPSHOTS = 3           # snapshots placed over the backfilled history at start


def busy_seconds(t: float) -> float:
    """Integral of the demo load curve: active-session-seconds since an arbitrary origin."""
    return 4 * t - 2700 * math.cos(t / 900.0) - 274 * math.cos(t / 137.0)


def _txt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _share(profiles: dict) -> dict:
    """Share of DB time per (wait_class, event) when every demo statement gets the same time."""
    out: dict = {}
    for prof in profiles.values():
        total = sum(w for _, _, w in prof)
        for wc, ev, w in prof:
            out[(wc, ev)] = out.get((wc, ev), 0.0) + w / total / len(profiles)
    return out


def _sql_share(profiles: dict, sql_id: str, wait_class: str) -> float:
    prof = profiles[sql_id]
    total = sum(w for _, _, w in prof)
    return sum(w for wc, _, w in prof if wc == wait_class) / total


# --------------------------------------------------------------------------- Oracle

def oracle(startup: float, now: float, sql: list, profiles: dict, users: list) -> dict:
    """One snapshot of the demo Oracle instance at the moment now (seconds)."""
    busy = busy_seconds(now) - busy_seconds(startup)          # DB time, s
    up = now - startup
    share = _share(profiles)
    cpu = busy * sum(v for (wc, _), v in share.items() if wc == "CPU")
    io_wait = busy * sum(v for (wc, _), v in share.items() if wc == "User I/O")

    reads_logical = int(busy * 21000)
    reads_phys = int(io_wait * 950)
    execs = int(busy * 380)
    commits = int(busy * 41)
    parses = int(execs * 0.31)
    sysstat = {
        "redo size": int(commits * 5200), "redo entries": int(commits * 14), "redo log space requests": int(up / 3600),
        "session logical reads": reads_logical, "db block changes": int(commits * 22),
        "physical reads": reads_phys, "physical reads cache": int(reads_phys * 0.82),
        "physical reads direct": int(reads_phys * 0.18), "physical writes": int(commits * 3),
        "user calls": int(execs * 1.6), "parse count (total)": parses, "parse count (hard)": int(parses * 0.04),
        "parse time cpu": int(parses * 0.02), "parse time elapsed": int(parses * 0.03),
        "sorts (memory)": int(execs * 0.12), "sorts (disk)": int(execs * 0.00004),
        "logons cumulative": int(up / 6), "execute count": execs, "user commits": commits,
        "user rollbacks": int(commits * 0.01),
    }
    events = []
    for (wc, ev), v in sorted(share.items()):
        if wc == "CPU":
            continue
        us = int(busy * v * 1e6)
        avg_ms = {"User I/O": 4.0, "Commit": 2.5, "Application": 180.0, "Concurrency": 1.2,
                  "Configuration": 40.0, "Network": 0.3, "Other": 0.8}.get(wc, 2.0)
        waits = max(1, int(us / 1000 / avg_ms))
        events.append({"event": ev, "wait_class": wc, "total_waits": waits, "time_us": us,
                       "all_waits": waits, "all_time_us": us})
    lgwr = int(commits * 0.9)
    events.append({"event": "log file parallel write", "wait_class": "System I/O", "total_waits": 0,
                   "time_us": 0, "all_waits": lgwr, "all_time_us": int(lgwr * 1100)})
    time_model = {"DB time": int(busy * 1e6), "DB CPU": int(cpu * 1e6),
                  "sql execute elapsed time": int(busy * 0.93e6), "parse time elapsed": int(busy * 0.012e6),
                  "hard parse elapsed time": int(busy * 0.006e6), "PL/SQL execution elapsed time": int(busy * 0.11e6),
                  "background elapsed time": int(up * 0.08e6), "background cpu time": int(up * 0.03e6)}
    cpus = 8
    busy_cs = int(cpu * 100 * 1.15)
    osstat = {"NUM_CPUS": cpus, "BUSY_TIME": busy_cs, "IDLE_TIME": max(0, int(up * cpus * 100) - busy_cs),
              "USER_TIME": int(busy_cs * 0.85), "SYS_TIME": int(busy_cs * 0.15), "IOWAIT_TIME": int(io_wait * 40),
              "LOAD": round(4 + 3 * math.sin(now / 900.0), 2), "PHYSICAL_MEMORY_BYTES": 32 * 1024 ** 3}
    per = busy / len(sql)
    sql_rows = []
    for k, (sid, op, text) in enumerate(sql):
        ex = int(per * (900 if op == "SELECT" else 260) / (1 + k % 3))
        user = users[(_crc(sid)) % len(users)]
        sql_rows.append({
            "sql_id": sid, "plan_hash_value": _crc(sid), "executions": ex, "elapsed_time": int(per * 1e6),
            "cpu_time": int(per * _sql_share(profiles, sid, "CPU") * 1e6),
            "buffer_gets": int(reads_logical / len(sql) * (0.4 + k / 5)), "disk_reads": int(reads_phys / len(sql) * (0.3 + k / 6)),
            "rows_processed": int(ex * (1 if op != "SELECT" else 12)), "parse_calls": int(ex * 0.3),
            "user_io_wait_time": int(per * _sql_share(profiles, sid, "User I/O") * 1e6),
            "application_wait_time": int(per * _sql_share(profiles, sid, "Application") * 1e6),
            "concurrency_wait_time": int(per * _sql_share(profiles, sid, "Concurrency") * 1e6),
            "cluster_wait_time": 0, "version_count": 1 + k % 2, "sharable_mem": 24000 + 1300 * k,
            "module": user[2], "parsing_schema_name": user[0], "sql_text": text[:200], "last_active": _txt(now)})
    tables = ["ORDERS", "CUSTOMERS", "ACCOUNTS", "AUDIT_LOG", "TRANSACTIONS", "PRODUCTS", "SESSION_CACHE", "INVENTORY"]
    seg_stats = {"logical reads": reads_logical, "physical reads": reads_phys, "physical writes": int(commits * 3),
                 "db block changes": int(commits * 22), "row lock waits": int(commits * 0.02),
                 "buffer busy waits": int(commits * 0.01)}
    segments = []
    for stat, total in seg_stats.items():
        for k, t in enumerate(tables):
            segments.append({"owner": "APP", "object_name": t, "subobject_name": None, "object_type": "TABLE",
                             "tablespace_name": "USERS", "statistic_name": stat,
                             "value": int(total * (0.32 / (k + 1)))})
    files = []
    for name, ts_, kind, frac in (("/u02/oradata/DEMO/system01.dbf", "SYSTEM", "DATA", 0.05),
                                  ("/u02/oradata/DEMO/sysaux01.dbf", "SYSAUX", "DATA", 0.07),
                                  ("/u02/oradata/DEMO/users01.dbf", "USERS", "DATA", 0.78),
                                  ("/u02/oradata/DEMO/undotbs01.dbf", "UNDOTBS1", "DATA", 0.06),
                                  ("/u02/oradata/DEMO/temp01.dbf", "TEMP", "TEMP", 0.04)):
        rd = int(reads_phys * frac)
        files.append({"tablespace": ts_, "file_name": name, "phyrds": rd, "phywrts": int(commits * 3 * frac),
                      "readtim": int(rd * 0.4), "writetim": int(commits * frac * 0.5),
                      "phyblkrd": int(rd * 1.6), "phyblkwrt": int(commits * 3 * frac * 1.2), "kind": kind})
    params = {
        "sga_target": ("6442450944", False), "sga_max_size": ("6442450944", True), "memory_target": ("0", True),
        "memory_max_target": ("0", True), "db_cache_size": ("0", True), "shared_pool_size": ("0", True),
        "pga_aggregate_target": ("2147483648", False), "log_buffer": ("16777216", True),
        "processes": ("400", False), "sessions": ("624", True), "open_cursors": ("300", False),
        "session_cached_cursors": ("50", True), "cursor_sharing": ("EXACT", True),
        "optimizer_mode": ("ALL_ROWS", True), "optimizer_index_cost_adj": ("100", True),
        "db_file_multiblock_read_count": ("128", True), "undo_retention": ("900", True),
        "undo_management": ("AUTO", True), "statistics_level": ("TYPICAL", True), "timed_statistics": ("TRUE", True),
        "fast_start_mttr_target": ("0", True), "filesystemio_options": ("none", True),
        "disk_asynch_io": ("TRUE", True), "parallel_max_servers": ("80", True), "job_queue_processes": ("40", True),
        "control_management_pack_access": ("NONE", False), "db_block_size": ("8192", False),
        "workarea_size_policy": ("AUTO", True), "compatible": ("19.0.0", False), "cpu_count": (str(cpus), True),
    }
    return {
        "taken_at": now,
        "instance": {"instance_number": 1, "instance_name": "DEMO1", "host_name": "demo-host",
                     "version": "19.0.0.0.0", "startup_time": _txt(startup), "dbid": 1234567890,
                     "db_name": "DEMO", "db_now": _txt(now),
                     "banner": "Oracle Database 19c Enterprise Edition — демо-база DB ASH Viewer"},
        "sysstat": sysstat, "events": events, "time_model": time_model, "osstat": osstat, "sql": sql_rows,
        "segments": segments, "files": files,
        "params": {k: {"value": v, "default": d} for k, (v, d) in params.items()},
        "sga": {"Fixed SGA Size": 8_900_000, "Redo Buffers": 16_777_216, "Buffer Cache Size": 4_294_967_296,
                "Shared Pool Size": 1_744_830_464, "Large Pool Size": 67_108_864, "Java Pool Size": 67_108_864,
                "Maximum SGA Size": 6_442_450_944, "Free SGA Memory Available": 0},
        "pga": {"aggregate PGA target parameter": 2_147_483_648, "total PGA allocated": 1_200_000_000,
                "total PGA inuse": 900_000_000, "maximum PGA allocated": 1_600_000_000,
                "cache hit percentage": 97.4, "over allocation count": 0},
        "advice": {"db_cache": [], "shared_pool": [], "pga": [], "sga": [], "memory": []},
        "limits": [{"resource_name": "processes", "cur": 72, "max_used": 118, "limit_value": "400"},
                   {"resource_name": "sessions", "cur": 84, "max_used": 131, "limit_value": "624"}],
        "undo": [], "redo_logs": [{"group#": g, "thread#": 1, "bytes": 1024 ** 3, "members": 2,
                                   "status": "CURRENT" if g == 1 else "INACTIVE"} for g in (1, 2, 3)],
        "log_switches": [_txt(now - h * 3600) for h in range(1, 48)],
        "stale_stats": [], "sessions": [{"total": 84, "active": round(4 + 3 * math.sin(now / 900.0))}],
    }


# --------------------------------------------------------------------------- PostgreSQL

PG_SETTINGS = {  # name -> (setting, unit, source, boot_val)
    "shared_buffers": ("524288", "8kB", "configuration file", "16384"),
    "effective_cache_size": ("2097152", "8kB", "configuration file", "524288"),
    "work_mem": ("16384", "kB", "configuration file", "4096"),
    "maintenance_work_mem": ("524288", "kB", "configuration file", "65536"),
    "max_connections": ("200", None, "configuration file", "100"),
    "max_wal_size": ("4096", "MB", "configuration file", "1024"), "min_wal_size": ("1024", "MB", "configuration file", "80"),
    "checkpoint_timeout": ("900", "s", "configuration file", "300"),
    "checkpoint_completion_target": ("0.9", None, "default", "0.9"), "wal_buffers": ("2048", "8kB", "override", "-1"),
    "random_page_cost": ("1.1", None, "configuration file", "4"), "seq_page_cost": ("1", None, "default", "1"),
    "effective_io_concurrency": ("200", None, "configuration file", "1"),
    "default_statistics_target": ("100", None, "default", "100"), "autovacuum": ("on", None, "default", "on"),
    "autovacuum_max_workers": ("3", None, "default", "3"), "autovacuum_naptime": ("60", "s", "default", "60"),
    "autovacuum_vacuum_scale_factor": ("0.2", None, "default", "0.2"),
    "autovacuum_analyze_scale_factor": ("0.1", None, "default", "0.1"),
    "autovacuum_vacuum_cost_limit": ("-1", None, "default", "-1"),
    "autovacuum_vacuum_cost_delay": ("2", "ms", "default", "2"), "track_io_timing": ("on", None, "configuration file", "off"),
    "track_activity_query_size": ("1024", "B", "default", "1024"),
    "shared_preload_libraries": ("pg_stat_statements", None, "configuration file", ""),
    "synchronous_commit": ("on", None, "default", "on"), "wal_level": ("replica", None, "default", "replica"),
    "max_worker_processes": ("8", None, "default", "8"), "max_parallel_workers": ("8", None, "default", "8"),
    "max_parallel_workers_per_gather": ("2", None, "default", "2"), "jit": ("on", None, "default", "on"),
    "huge_pages": ("try", None, "default", "try"),
    "idle_in_transaction_session_timeout": ("0", "ms", "default", "0"), "statement_timeout": ("0", "ms", "default", "0"),
    "log_min_duration_statement": ("1000", "ms", "configuration file", "-1"),
    "compute_query_id": ("auto", None, "default", "auto"), "temp_buffers": ("1024", "8kB", "default", "1024"),
    "bgwriter_lru_maxpages": ("100", None, "default", "100"), "bgwriter_delay": ("200", "ms", "default", "200"),
    "fsync": ("on", None, "default", "on"), "full_page_writes": ("on", None, "default", "on"),
    "wal_compression": ("off", None, "default", "off"), "log_autovacuum_min_duration": ("600000", "ms", "default", "600000"),
    "server_version": ("16.4", None, "default", "16.4"), "block_size": ("8192", None, "default", "8192"),
    "lock_timeout": ("0", "ms", "default", "0"), "deadlock_timeout": ("1000", "ms", "default", "1000"),
    "max_locks_per_transaction": ("64", None, "default", "64"),
}


def postgres(startup: float, now: float, sql: list, profiles: dict, users: list, fingerprint) -> dict:
    """One snapshot of the demo PostgreSQL cluster at the moment now (seconds)."""
    busy = busy_seconds(now) - busy_seconds(startup)
    up = now - startup
    share = _share(profiles)
    io_wait = busy * sum(v for (wc, _), v in share.items() if wc == "IO")
    calls = int(busy * 420)
    commits = int(busy * 45)
    blks_read = int(io_wait * 1100)
    blks_hit = int(busy * 26000)
    dbs = []
    for name, frac, size in (("shop", 0.78, 6_400_000_000), ("dwh", 0.2, 21_000_000_000), ("postgres", 0.02, 8_000_000)):
        dbs.append({"datname": name, "numbackends": {"shop": 46, "dwh": 9, "postgres": 1}[name],
                    "xact_commit": int(commits * frac), "xact_rollback": int(commits * frac * 0.008),
                    "blks_read": int(blks_read * frac), "blks_hit": int(blks_hit * frac),
                    "tup_returned": int(calls * frac * 160), "tup_fetched": int(calls * frac * 24),
                    "tup_inserted": int(commits * frac * 1.3), "tup_updated": int(commits * frac * 0.9),
                    "tup_deleted": int(commits * frac * 0.4), "conflicts": 0,
                    "temp_files": int(up / 3600 * frac * 4), "temp_bytes": int(up / 3600 * frac * 4 * 48_000_000),
                    "deadlocks": int(up / 86400), "blk_read_time": io_wait * frac * 1000 * 0.6,
                    "blk_write_time": io_wait * frac * 1000 * 0.1, "stats_reset": None, "size": size,
                    "xid_age": int(40_000_000 + up * 25)})
    ckpt = up / 900
    bgwriter = {"checkpoints_timed": int(ckpt * 0.95), "checkpoints_req": int(ckpt * 0.05),
                "checkpoint_write_time": ckpt * 380_000.0, "checkpoint_sync_time": ckpt * 900.0,
                "buffers_checkpoint": int(commits * 1.8), "buffers_clean": int(commits * 0.6), "maxwritten_clean": int(ckpt / 3),
                "buffers_backend": int(commits * 0.15), "buffers_backend_fsync": 0, "buffers_alloc": blks_read}
    wal_bytes = int(commits * 6100)
    statements = []
    per = busy / len(sql)
    for k, (qid, op, text) in enumerate(sql):
        db = users[_crc(qid) % len(users)][2]
        role = users[_crc(qid) % len(users)][0]
        c = int(per * (950 if op == "SELECT" else 280) / (1 + k % 3))
        rd = int(blks_read / len(sql) * (0.3 + k / 6))
        statements.append({"queryid": str(int(qid[:15], 16)), "datname": db, "rolname": role, "query": text[:300],
                           "calls": c, "total_ms": per * 1000, "rows": c * (12 if op == "SELECT" else 1),
                           "shared_blks_hit": int(blks_hit / len(sql) * (0.4 + k / 5)), "shared_blks_read": rd,
                           "shared_blks_dirtied": int(commits / len(sql) * 1.2), "shared_blks_written": int(commits / len(sql) * 0.1),
                           "temp_blks_read": int(rd * 0.02 if "sum(" in text else 0),
                           "temp_blks_written": int(rd * 0.02 if "sum(" in text else 0),
                           "io_ms": per * 1000 * _sql_share(profiles, qid, "IO"), "fingerprint": fingerprint(text)})
    tables = []
    for k, (t, live, size) in enumerate((("orders", 4_800_000, 1_900_000_000), ("customers", 610_000, 230_000_000),
                                         ("accounts", 820_000, 310_000_000), ("audit_log", 31_000_000, 7_600_000_000),
                                         ("transactions", 12_400_000, 3_100_000_000), ("products", 94_000, 40_000_000),
                                         ("session_cache", 1_300_000, 520_000_000), ("inventory", 2_100_000, 600_000_000),
                                         ("warehouses", 120, 16_384))):
        scans = int(calls / 9 / (k + 1))
        tables.append({"relid": 16384 + k, "schemaname": "public", "relname": t,
                       "seq_scan": int(scans * (0.6 if t in ("transactions", "session_cache") else 0.02)),
                       "seq_tup_read": int(scans * 400), "idx_scan": scans, "idx_tup_fetch": scans * 3,
                       "n_tup_ins": int(commits / 9), "n_tup_upd": int(commits / 12), "n_tup_hot_upd": int(commits / 30),
                       "n_tup_del": int(commits / 40), "n_live_tup": live,
                       "n_dead_tup": int(live * (0.24 if t == "session_cache" else 0.02)), "n_mod_since_analyze": int(live * 0.03),
                       "last_vacuum": None, "last_autovacuum": _txt(now - 3600 * (k + 1)) + "+03",
                       "last_analyze": None, "last_autoanalyze": _txt(now - 5400 * (k + 1)) + "+03",
                       "autovacuum_count": int(up / 3600 / (k + 1)), "autoanalyze_count": int(up / 7200 / (k + 1)),
                       "heap_blks_read": int(blks_read / 9 / (k + 1)), "heap_blks_hit": int(blks_hit / 9 / (k + 1)),
                       "idx_blks_read": int(blks_read / 30 / (k + 1)), "idx_blks_hit": int(blks_hit / 20 / (k + 1)),
                       "size": size, "total_size": int(size * 1.4)})
    load = round(4 + 3 * math.sin(now / 900.0))
    return {
        "taken_at": now,
        "instance": {"version_num": 160004, "version": "16.4", "banner": "PostgreSQL 16.4 — демо-база DB ASH Viewer",
                     "db_name": "shop", "cluster": "demo", "host": "demo-host", "startup_time": _txt(startup) + "+03",
                     "db_now": _txt(now) + "+03", "in_recovery": False, "max_connections": 200, "reserved": 3,
                     "block_size": 8192, "cpu_count": 8},
        "databases": dbs, "bgwriter": bgwriter,
        "wal": {"bytes": wal_bytes, "wal_records": int(commits * 14), "wal_fpi": int(commits * 0.6),
                "wal_bytes": wal_bytes, "wal_buffers_full": int(commits * 0.0005)},
        "settings": {n: {"name": n, "setting": s, "unit": u, "source": src, "boot_val": b, "short_desc": ""}
                     for n, (s, u, src, b) in PG_SETTINGS.items()},
        "extensions": ["plpgsql", "pg_stat_statements"], "statements": statements, "statements_note": None,
        "tables": tables,
        "unused_indexes": [{"schemaname": "public", "table_name": "audit_log", "index_name": "audit_log_payload_idx",
                            "idx_scan": 0, "size": 412_000_000}],
        "activity": [{"state": "active", "sessions": load, "lock_waits": 1, "oldest_xact_s": 3, "longest_in_state_s": 2},
                     {"state": "idle", "sessions": 52, "lock_waits": 0, "oldest_xact_s": None, "longest_in_state_s": 1800},
                     {"state": "idle in transaction", "sessions": 1, "lock_waits": 0, "oldest_xact_s": 95,
                      "longest_in_state_s": 95}],
        "long_xacts": [], "slots": [], "replication": [],
    }


def _crc(s: str) -> int:
    import zlib
    return zlib.crc32(s.encode())


def startup_for(now: float | None = None) -> float:
    """Startup moment of a demo instance: the same for a whole week, so snapshots taken before and after
    a restart of the application still belong to one «instance run» and can be compared."""
    now = time.time() if now is None else now
    week = 7 * 86400
    return float((now - 86400) // week * week)
