"""AWR-like reports without the Diagnostics Pack (the Statspack approach).

Snapshots of cumulative V$ statistics are taken periodically and on demand, stored compressed in
the local SQLite store, and a report is built from the difference between two snapshots:
load profile, instance efficiency, top timed events, time model, top SQL, segments, I/O, memory
and advisories - followed by rule-based tuning recommendations.

Only V$ views are used (no DBA_HIST_*, no DBMS_WORKLOAD_REPOSITORY), so this works on every
edition including XE and does not require the Diagnostics Pack licence.
"""
from __future__ import annotations
import json
import time
import zlib
from datetime import datetime, timedelta

from .. import store

SQL_TOP = 100      # SQL kept per ranking in a snapshot (elapsed, cpu, gets, reads, execs, parses)
SEG_TOP = 25       # segments kept per statistic
SYS_SCHEMAS = ("SYS", "SYSTEM", "XDB", "MDSYS", "CTXSYS", "OUTLN", "DBSNMP", "APEX_040000", "APEX_040200",
               "FLOWS_FILES", "ORDSYS", "EXFSYS", "WMSYS", "ANONYMOUS", "APPQOSSYS", "ORACLE_OCM")

PARAMS = ("sga_target", "sga_max_size", "memory_target", "memory_max_target", "db_cache_size",
          "shared_pool_size", "pga_aggregate_target", "log_buffer", "processes", "sessions",
          "open_cursors", "session_cached_cursors", "cursor_sharing", "optimizer_mode",
          "optimizer_index_cost_adj", "db_file_multiblock_read_count", "undo_retention",
          "undo_management", "statistics_level", "timed_statistics", "fast_start_mttr_target",
          "filesystemio_options", "disk_asynch_io", "parallel_max_servers", "job_queue_processes",
          "control_management_pack_access", "db_block_size", "commit_write", "commit_logging",
          "commit_wait", "workarea_size_policy", "result_cache_max_size", "compatible",
          "resource_manager_plan", "cpu_count")


# --------------------------------------------------------------------------- capture

def _rows(cur, sql, **binds):
    cur.execute(sql, binds)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _try(cur, sql, fallback=None, **binds):
    try:
        return _rows(cur, sql, **binds)
    except Exception:  # noqa: BLE001 - missing view/column on this version or no privilege
        if fallback:
            return _try(cur, fallback, **binds)
        return []


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    if hasattr(v, "read"):
        return v.read()
    return v


def capture(conn) -> dict:
    """Collect one snapshot of cumulative statistics through an open Oracle connection."""
    cur = conn.cursor()
    snap = {"taken_at": time.time()}
    inst = _rows(cur, """SELECT i.instance_number, i.instance_name, i.host_name, i.version, i.startup_time,
                                d.dbid, d.name db_name, SYSDATE db_now,
                                (SELECT banner FROM v$version WHERE ROWNUM = 1) banner
                           FROM v$instance i, v$database d""")[0]
    snap["instance"] = inst
    snap["sysstat"] = {r["name"]: r["value"] for r in _rows(cur, "SELECT name, value FROM v$sysstat WHERE value <> 0")}
    snap["events"] = _try(
        cur,
        """SELECT event, wait_class, total_waits_fg total_waits, time_waited_micro_fg time_us,
                  total_waits all_waits, time_waited_micro all_time_us
             FROM v$system_event WHERE total_waits > 0""",
        """SELECT event, wait_class, total_waits, time_waited_micro time_us,
                  total_waits all_waits, time_waited_micro all_time_us
             FROM v$system_event WHERE total_waits > 0""")
    snap["time_model"] = {r["stat_name"]: r["value"] for r in _rows(cur, "SELECT stat_name, value FROM v$sys_time_model")}
    snap["osstat"] = {r["stat_name"]: r["value"] for r in _try(cur, "SELECT stat_name, value FROM v$osstat")}
    snap["sql"] = _try(cur, f"""
        SELECT sql_id, plan_hash_value, executions, elapsed_time, cpu_time, buffer_gets, disk_reads,
               rows_processed, parse_calls, user_io_wait_time, application_wait_time,
               concurrency_wait_time, cluster_wait_time, version_count, sharable_mem,
               module, parsing_schema_name, SUBSTR(sql_text, 1, 200) sql_text,
               TO_CHAR(last_active_time, 'YYYY-MM-DD HH24:MI:SS') last_active
          FROM (SELECT s.*,
                       ROW_NUMBER() OVER (ORDER BY elapsed_time DESC) r1,
                       ROW_NUMBER() OVER (ORDER BY cpu_time DESC) r2,
                       ROW_NUMBER() OVER (ORDER BY buffer_gets DESC) r3,
                       ROW_NUMBER() OVER (ORDER BY disk_reads DESC) r4,
                       ROW_NUMBER() OVER (ORDER BY executions DESC) r5,
                       ROW_NUMBER() OVER (ORDER BY parse_calls DESC) r6
                  FROM v$sqlarea s)
         WHERE LEAST(r1, r2, r3, r4, r5, r6) <= {SQL_TOP}""")
    sys_list = ",".join(f"'{s}'" for s in SYS_SCHEMAS)
    snap["segments"] = _try(cur, f"""
        SELECT owner, object_name, subobject_name, object_type, tablespace_name, statistic_name, value
          FROM (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY statistic_name ORDER BY value DESC) rn
                  FROM v$segment_statistics s
                 WHERE statistic_name IN ('logical reads', 'physical reads', 'physical writes',
                                          'row lock waits', 'buffer busy waits', 'ITL waits',
                                          'db block changes', 'physical reads direct')
                   AND value > 0)
         WHERE rn <= {SEG_TOP * 4}""")
    snap["files"] = _try(cur, """
        SELECT t.name tablespace, f.name file_name, s.phyrds, s.phywrts, s.readtim, s.writetim,
               s.phyblkrd, s.phyblkwrt, 'DATA' kind
          FROM v$filestat s JOIN v$datafile f ON f.file# = s.file# JOIN v$tablespace t ON t.ts# = f.ts#
        UNION ALL
        SELECT t.name, f.name, s.phyrds, s.phywrts, s.readtim, s.writetim, s.phyblkrd, s.phyblkwrt, 'TEMP'
          FROM v$tempstat s JOIN v$tempfile f ON f.file# = s.file# JOIN v$tablespace t ON t.ts# = f.ts#""")
    in_list = ",".join(f"'{p}'" for p in PARAMS)
    snap["params"] = {r["name"]: {"value": r["value"], "default": r["isdefault"] == "TRUE"} for r in _rows(
        cur, f"SELECT name, value, isdefault FROM v$parameter WHERE name IN ({in_list}) OR isdefault = 'FALSE'")}
    snap["sga"] = {r["name"]: r["bytes"] for r in _try(cur, "SELECT name, bytes FROM v$sgainfo")}
    snap["pga"] = {r["name"]: r["value"] for r in _try(cur, "SELECT name, value FROM v$pgastat")}
    snap["advice"] = {
        "db_cache": _try(cur, """SELECT size_for_estimate size_mb, size_factor, estd_physical_read_factor read_factor,
                                        estd_physical_reads reads
                                   FROM v$db_cache_advice
                                  WHERE name = 'DEFAULT' AND advice_status = 'ON'
                                    AND block_size = (SELECT value FROM v$parameter WHERE name = 'db_block_size')
                                  ORDER BY size_factor"""),
        "shared_pool": _try(cur, """SELECT shared_pool_size_for_estimate size_mb, shared_pool_size_factor size_factor,
                                           estd_lc_time_saved time_saved_s, estd_lc_load_time load_time_s
                                      FROM v$shared_pool_advice ORDER BY size_factor"""),
        "pga": _try(cur, """SELECT ROUND(pga_target_for_estimate / 1048576) size_mb, pga_target_factor size_factor,
                                   estd_pga_cache_hit_percentage hit_pct, estd_overalloc_count overalloc
                              FROM v$pga_target_advice ORDER BY pga_target_factor"""),
        "sga": _try(cur, """SELECT sga_size size_mb, sga_size_factor size_factor, estd_db_time db_time,
                                   estd_physical_reads reads
                              FROM v$sga_target_advice ORDER BY sga_size_factor"""),
        "memory": _try(cur, """SELECT memory_size size_mb, memory_size_factor size_factor, estd_db_time db_time
                                 FROM v$memory_target_advice ORDER BY memory_size_factor"""),
    }
    snap["limits"] = _try(cur, """SELECT resource_name, current_utilization cur, max_utilization max_used,
                                         TRIM(limit_value) limit_value
                                    FROM v$resource_limit
                                   WHERE resource_name IN ('processes', 'sessions', 'transactions', 'enqueue_locks')""")
    snap["undo"] = _try(cur, """SELECT TO_CHAR(begin_time, 'YYYY-MM-DD HH24:MI:SS') begin_time, undoblks,
                                       maxquerylen, ssolderrcnt, nospaceerrcnt
                                  FROM v$undostat WHERE begin_time > SYSDATE - 2""")
    snap["redo_logs"] = _try(cur, "SELECT group#, thread#, bytes, members, status FROM v$log")
    snap["log_switches"] = [r["t"] for r in _try(
        cur, "SELECT TO_CHAR(first_time, 'YYYY-MM-DD HH24:MI:SS') t FROM v$log_history WHERE first_time > SYSDATE - 2")]
    snap["stale_stats"] = _try(cur, f"""
        SELECT owner, SUM(CASE WHEN stale_stats = 'YES' THEN 1 ELSE 0 END) stale,
               SUM(CASE WHEN last_analyzed IS NULL THEN 1 ELSE 0 END) never
          FROM dba_tab_statistics
         WHERE owner NOT IN ({sys_list}) AND object_type = 'TABLE'
         GROUP BY owner
        HAVING SUM(CASE WHEN stale_stats = 'YES' OR last_analyzed IS NULL THEN 1 ELSE 0 END) > 0""")
    snap["sessions"] = _try(cur, """SELECT COUNT(*) total, SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END) active
                                      FROM v$session WHERE type = 'USER'""")
    return json.loads(json.dumps(snap, default=_jsonable))


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

STAT_PROFILE = [
    # (label, sysstat name or callable, unit)
    ("DB time, с", "tm:DB time", "s"),
    ("DB CPU, с", "tm:DB CPU", "s"),
    ("Redo, байт", "redo size", ""),
    ("Логические чтения (блоки)", "session logical reads", ""),
    ("Изменения блоков", "db block changes", ""),
    ("Физические чтения (блоки)", "physical reads", ""),
    ("Физические записи (блоки)", "physical writes", ""),
    ("Вызовы пользователей", "user calls", ""),
    ("Разборы (всего)", "parse count (total)", ""),
    ("Жёсткие разборы", "parse count (hard)", ""),
    ("Сортировки (память)", "sorts (memory)", ""),
    ("Сортировки (диск)", "sorts (disk)", ""),
    ("Входы в систему", "logons cumulative", ""),
    ("Выполнения SQL", "execute count", ""),
    ("Транзакции", "txn", ""),
]


def _delta_map(b: dict, e: dict) -> dict:
    return {k: max(0, (e.get(k) or 0) - (b.get(k) or 0)) for k in e}


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else None


class Report:
    def __init__(self, b: dict, e: dict):
        self.b, self.e = b, e
        self.elapsed = max(1.0, e["taken_at"] - b["taken_at"])
        self.ss = _delta_map(b["sysstat"], e["sysstat"])
        self.tm = _delta_map(b["time_model"], e["time_model"])
        self.os = _delta_map(b.get("osstat", {}), e.get("osstat", {}))
        self.db_time = self.tm.get("DB time", 0) / 1e6
        self.db_cpu = self.tm.get("DB CPU", 0) / 1e6
        self.txn = self.ss.get("user commits", 0) + self.ss.get("user rollbacks", 0)
        self.cpus = int(e.get("osstat", {}).get("NUM_CPUS") or self.param("cpu_count") or 1)
        self.recs = []
        # interval boundaries on the database clock (SQL/redo/undo times come from SYSDATE)
        self.db_begin = str(b["instance"]["db_now"])[:19]
        self.db_end = str(e["instance"]["db_now"])[:19]

    def param(self, name, default=None):
        p = self.e.get("params", {}).get(name)
        return p["value"] if p else default

    def iparam(self, name, default=0):
        try:
            return int(self.param(name, default) or 0)
        except (TypeError, ValueError):
            return default

    def stat(self, key):
        if key == "txn":
            return self.txn
        if key.startswith("tm:"):
            return self.tm.get(key[3:], 0) / 1e6
        return self.ss.get(key, 0)

    # ---- sections

    def header(self):
        i = self.e["instance"]
        sess = (self.e.get("sessions") or [{}])[0]
        return {
            "db_name": i["db_name"], "dbid": i["dbid"], "instance": i["instance_name"], "host": i["host_name"],
            "version": i["version"], "banner": i.get("banner"), "startup_time": i["startup_time"],
            "cpus": self.cpus,
            "begin": {"id": self.b["snap_id"], "ts": self.b["taken_at"]},
            "end": {"id": self.e["snap_id"], "ts": self.e["taken_at"]},
            "elapsed_min": round(self.elapsed / 60, 1), "db_time_min": round(self.db_time / 60, 2),
            "aas": round(self.db_time / self.elapsed, 2),
            "sessions": sess.get("total"), "active_sessions": sess.get("active"),
        }

    def load_profile(self):
        out = []
        for label, key, _ in STAT_PROFILE:
            v = self.stat(key)
            out.append({"name": label, "per_sec": round(v / self.elapsed, 2),
                        "per_txn": round(v / self.txn, 2) if self.txn else None, "total": round(v, 2)})
        return out

    def efficiency(self):
        ss = self.ss
        lr = ss.get("session logical reads", 0)
        pr_cache = ss.get("physical reads cache", ss.get("physical reads", 0) - ss.get("physical reads direct", 0))
        parses, hard = ss.get("parse count (total)", 0), ss.get("parse count (hard)", 0)
        execs = ss.get("execute count", 0)
        sorts_m, sorts_d = ss.get("sorts (memory)", 0), ss.get("sorts (disk)", 0)
        parse_cpu, parse_ela = ss.get("parse time cpu", 0), ss.get("parse time elapsed", 0)
        redo_entries, redo_req = ss.get("redo entries", 0), ss.get("redo log space requests", 0)
        latch = {}
        return [
            {"name": "Buffer Hit %", "value": _pct(lr - pr_cache, lr) if lr else None, "good": 90},
            {"name": "Soft Parse %", "value": _pct(parses - hard, parses) if parses else None, "good": 95},
            {"name": "Execute to Parse %", "value": _pct(execs - parses, execs) if execs else None, "good": 50},
            {"name": "In-memory Sort %", "value": _pct(sorts_m, sorts_m + sorts_d) if sorts_m + sorts_d else None,
             "good": 99},
            {"name": "Parse CPU to Parse Elapsd %", "value": _pct(parse_cpu, parse_ela) if parse_ela else None,
             "good": 70},
            {"name": "Redo NoWait %", "value": _pct(redo_entries - redo_req, redo_entries) if redo_entries else None,
             "good": 99},
            {"name": "% Non-Parse CPU", "value": _pct(self.db_cpu * 100 - parse_cpu, self.db_cpu * 100)
             if self.db_cpu else None, "good": 90},
        ] + list(latch.values())

    def events(self):
        bev = {r["event"]: r for r in self.b.get("events", [])}
        out = []
        for r in self.e.get("events", []):
            if r["wait_class"] == "Idle":
                continue
            p = bev.get(r["event"], {})
            waits = (r["total_waits"] or 0) - (p.get("total_waits") or 0)
            us = (r["time_us"] or 0) - (p.get("time_us") or 0)
            if waits <= 0 and us <= 0:
                continue
            out.append({"event": r["event"], "wait_class": r["wait_class"], "waits": waits,
                        "time_s": round(us / 1e6, 3), "avg_ms": round(us / 1e3 / waits, 2) if waits else None})
        out.sort(key=lambda x: x["time_s"], reverse=True)
        return out

    def top_events(self, events):
        rows = [{"event": "DB CPU", "wait_class": "CPU", "waits": None, "time_s": round(self.db_cpu, 3),
                 "avg_ms": None}] + events
        rows.sort(key=lambda x: x["time_s"], reverse=True)
        for r in rows:
            r["pct_db_time"] = _pct(r["time_s"], self.db_time)
        return rows[:10]

    def wait_classes(self, events):
        agg = {"CPU": self.db_cpu}
        for r in events:
            agg[r["wait_class"]] = agg.get(r["wait_class"], 0) + r["time_s"]
        rows = [{"wait_class": k, "time_s": round(v, 3), "pct_db_time": _pct(v, self.db_time)} for k, v in agg.items()]
        return sorted(rows, key=lambda x: x["time_s"], reverse=True)

    def time_model(self):
        rows = [{"name": k, "time_s": round(v / 1e6, 3), "pct_db_time": _pct(v / 1e6, self.db_time)}
                for k, v in self.tm.items() if v > 0 and k not in ("DB time",)]
        return sorted(rows, key=lambda x: x["time_s"], reverse=True)

    def sql(self):
        prev = {(r["sql_id"], r["plan_hash_value"]): r for r in self.b.get("sql", [])}
        begin_txt = self.db_begin
        rows = []
        for r in self.e.get("sql", []):
            p = prev.get((r["sql_id"], r["plan_hash_value"]))
            if p is None and (r.get("last_active") or "") < begin_txt:
                continue  # not active in the interval and not seen at the beginning
            d = {k: max(0, (r.get(k) or 0) - ((p or {}).get(k) or 0)) for k in
                 ("executions", "elapsed_time", "cpu_time", "buffer_gets", "disk_reads", "rows_processed",
                  "parse_calls", "user_io_wait_time", "application_wait_time", "concurrency_wait_time")}
            if not any(d.values()):
                continue
            ex = d["executions"]
            rows.append({
                "sql_id": r["sql_id"], "plan_hash": r["plan_hash_value"], "module": r.get("module"),
                "schema": r.get("parsing_schema_name"), "text": r.get("sql_text"),
                "versions": r.get("version_count"),
                "executions": ex, "elapsed_s": round(d["elapsed_time"] / 1e6, 3),
                "cpu_s": round(d["cpu_time"] / 1e6, 3), "io_s": round(d["user_io_wait_time"] / 1e6, 3),
                "app_s": round(d["application_wait_time"] / 1e6, 3),
                "conc_s": round(d["concurrency_wait_time"] / 1e6, 3),
                "gets": d["buffer_gets"], "reads": d["disk_reads"], "rows": d["rows_processed"],
                "parses": d["parse_calls"],
                "ela_per_exec_ms": round(d["elapsed_time"] / 1e3 / ex, 2) if ex else None,
                "gets_per_exec": round(d["buffer_gets"] / ex, 1) if ex else None,
                "pct_db_time": _pct(d["elapsed_time"] / 1e6, self.db_time),
            })
        lr, pr = self.ss.get("session logical reads", 0), self.ss.get("physical reads", 0)
        for r in rows:
            r["pct_gets"] = _pct(r["gets"], lr)
            r["pct_reads"] = _pct(r["reads"], pr)

        def top(key):
            return [r for r in sorted(rows, key=lambda x: x[key] or 0, reverse=True) if r[key]][:15]
        return {"elapsed": top("elapsed_s"), "cpu": top("cpu_s"), "gets": top("gets"), "reads": top("reads"),
                "executions": top("executions"), "parses": top("parses"), "io": top("io_s"),
                "application": top("app_s")}

    def segments(self):
        prev = {(r["owner"], r["object_name"], r.get("subobject_name"), r["statistic_name"]): r["value"]
                for r in self.b.get("segments", [])}
        by = {}
        for r in self.e.get("segments", []):
            k = (r["owner"], r["object_name"], r.get("subobject_name"), r["statistic_name"])
            v = (r["value"] or 0) - prev.get(k, 0)
            if v <= 0:
                continue
            by.setdefault(r["statistic_name"], []).append({
                "owner": r["owner"], "object": r["object_name"] + (f".{r['subobject_name']}" if r.get("subobject_name") else ""),
                "type": r["object_type"], "tablespace": r.get("tablespace_name"), "value": v})
        tot = {"logical reads": self.ss.get("session logical reads", 0),
               "physical reads": self.ss.get("physical reads", 0)}
        out = {}
        for k, rows in by.items():
            rows.sort(key=lambda x: x["value"], reverse=True)
            for r in rows:
                r["pct"] = _pct(r["value"], tot.get(k, 0)) if tot.get(k) else None
            out[k] = rows[:SEG_TOP]
        return out

    def io(self):
        prev = {r["file_name"]: r for r in self.b.get("files", [])}
        agg = {}
        for r in self.e.get("files", []):
            p = prev.get(r["file_name"], {})
            rd = (r["phyrds"] or 0) - (p.get("phyrds") or 0)
            wr = (r["phywrts"] or 0) - (p.get("phywrts") or 0)
            rt = (r["readtim"] or 0) - (p.get("readtim") or 0)   # centiseconds
            wt = (r["writetim"] or 0) - (p.get("writetim") or 0)
            a = agg.setdefault(r["tablespace"], {"tablespace": r["tablespace"], "kind": r["kind"],
                                                 "reads": 0, "writes": 0, "rt": 0, "wt": 0, "blk_rd": 0})
            a["reads"] += max(0, rd)
            a["writes"] += max(0, wr)
            a["rt"] += max(0, rt)
            a["wt"] += max(0, wt)
            a["blk_rd"] += max(0, (r["phyblkrd"] or 0) - (p.get("phyblkrd") or 0))
        rows = []
        for a in agg.values():
            rows.append({"tablespace": a["tablespace"], "kind": a["kind"], "reads": a["reads"],
                         "reads_per_s": round(a["reads"] / self.elapsed, 2),
                         "avg_read_ms": round(a["rt"] * 10 / a["reads"], 2) if a["reads"] else None,
                         "blocks_per_read": round(a["blk_rd"] / a["reads"], 1) if a["reads"] else None,
                         "writes": a["writes"], "writes_per_s": round(a["writes"] / self.elapsed, 2),
                         "avg_write_ms": round(a["wt"] * 10 / a["writes"], 2) if a["writes"] else None})
        return sorted(rows, key=lambda x: x["reads"] + x["writes"], reverse=True)

    def memory(self):
        e = self.e
        mb = lambda v: round((v or 0) / 1048576, 1)  # noqa: E731
        sga = [{"name": k, "mb": mb(v)} for k, v in e.get("sga", {}).items()
               if k in ("Fixed SGA Size", "Redo Buffers", "Buffer Cache Size", "Shared Pool Size",
                        "Large Pool Size", "Java Pool Size", "Streams Pool Size", "Maximum SGA Size",
                        "Free SGA Memory Available")]
        pga = e.get("pga", {})
        pga_rows = [{"name": k, "value": v} for k, v in pga.items()
                    if k in ("aggregate PGA target parameter", "total PGA allocated", "maximum PGA allocated",
                             "cache hit percentage", "over allocation count", "total PGA inuse")]
        return {"sga": sga, "pga": pga_rows, "advice": e.get("advice", {})}

    def os_stats(self):
        busy, idle = self.os.get("BUSY_TIME", 0), self.os.get("IDLE_TIME", 0)
        user, sysv, iow = self.os.get("USER_TIME", 0), self.os.get("SYS_TIME", 0), self.os.get("IOWAIT_TIME", 0)
        tot = busy + idle
        osv = self.e.get("osstat", {})
        return {"num_cpus": self.cpus, "busy_pct": _pct(busy, tot), "user_pct": _pct(user, tot),
                "sys_pct": _pct(sysv, tot), "iowait_pct": _pct(iow, tot), "load": osv.get("LOAD"),
                "memory_mb": round((osv.get("PHYSICAL_MEMORY_BYTES") or 0) / 1048576)}

    def log_switches(self):
        b, e = self.db_begin, self.db_end
        n = sum(1 for t in self.e.get("log_switches", []) if b <= t <= e)
        return {"count": n, "per_hour": round(n * 3600 / self.elapsed, 1),
                "logs": self.e.get("redo_logs", [])}

    def undo(self):
        b = (datetime.fromisoformat(self.db_begin) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        rows = [r for r in self.e.get("undo", []) if r["begin_time"] >= b]
        return {"max_query_s": max((r["maxquerylen"] or 0 for r in rows), default=0),
                "snapshot_too_old": sum(r["ssolderrcnt"] or 0 for r in rows),
                "no_space": sum(r["nospaceerrcnt"] or 0 for r in rows)}

    def params(self):
        return [{"name": k, "value": v["value"], "default": v["default"]}
                for k, v in sorted(self.e.get("params", {}).items())]

    # ---- build

    def build(self):
        from .awr_advisor import advise
        events = self.events()
        rep = {
            "header": self.header(), "load_profile": self.load_profile(), "efficiency": self.efficiency(),
            "top_events": self.top_events(events), "wait_classes": self.wait_classes(events),
            "events": events[:40], "time_model": self.time_model(), "sql": self.sql(),
            "segments": self.segments(), "io": self.io(), "memory": self.memory(), "os": self.os_stats(),
            "log_switches": self.log_switches(), "undo": self.undo(),
            "limits": self.e.get("limits", []), "stale_stats": self.e.get("stale_stats", []),
            "params": self.params(),
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
        raise ValueError("Между снимками экземпляр перезапускался — накопленная статистика обнулилась. "
                         "Выберите снимки после последнего запуска.")
    return Report(b, e).build()
