"""Local SQLite storage for sampled session activity (the "ASH" history)."""
from __future__ import annotations
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("ASHV_DB", "/data/ashv.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks(
    target TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    PRIMARY KEY (target, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples(
    target        TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    inst_id       INTEGER,
    sid           INTEGER,
    serial        INTEGER,
    username      TEXT,
    program       TEXT,
    module        TEXT,
    machine       TEXT,
    sql_id        TEXT,
    sql_opname    TEXT,
    plan_hash     INTEGER,
    event         TEXT,
    wait_class    TEXT,
    blocking_inst INTEGER,
    blocking_sid  INTEGER,
    session_type  TEXT
);
CREATE INDEX IF NOT EXISTS samples_target_ts ON samples(target, ts);

CREATE TABLE IF NOT EXISTS snapshots(
    snap_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    target    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    startup   TEXT,
    manual    INTEGER NOT NULL DEFAULT 0,
    data      BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_target_ts ON snapshots(target, ts);

CREATE TABLE IF NOT EXISTS sqltext(
    target  TEXT NOT NULL,
    sql_id  TEXT NOT NULL,
    text    TEXT,
    opname  TEXT,
    PRIMARY KEY (target, sql_id)
) WITHOUT ROWID;
"""

SAMPLE_COLS = (
    "target", "ts", "inst_id", "sid", "serial", "username", "program", "module",
    "machine", "sql_id", "sql_opname", "plan_hash", "event", "wait_class",
    "blocking_inst", "blocking_sid", "session_type",
)

CONN_SCHEMA = """
-- Desktop version: the saved databases of the entry list (the web version keeps them in the browser)
CREATE TABLE IF NOT EXISTS connections(
    id           TEXT PRIMARY KEY,
    pos          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    host         TEXT,
    port         INTEGER,
    service      TEXT,
    username     TEXT,
    password     TEXT,
    mode         TEXT,
    source       TEXT,
    load_enabled INTEGER NOT NULL DEFAULT 0,
    target       TEXT,
    extra        TEXT,
    updated      INTEGER NOT NULL
);
"""

# The saved databases may live in a separate SQLite file (ASHV_CONN_DB); by default — in the history database.
CONN_DB_PATH = os.environ.get("ASHV_CONN_DB", "")

_local = threading.local()
_write_lock = threading.Lock()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA cache_size=-65536")
        _local.conn = c
    return c


def conn_db() -> sqlite3.Connection:
    """Database of the saved connections: its own file when ASHV_CONN_DB is set, else the history database."""
    if not CONN_DB_PATH:
        return conn()
    c = getattr(_local, "conn_db", None)
    if c is None:
        os.makedirs(os.path.dirname(CONN_DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(CONN_DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn_db = c
    return c


def init():
    with _write_lock:
        conn().executescript(SCHEMA)
        conn().commit()
        conn_db().executescript(CONN_SCHEMA)
        conn_db().commit()


def write_samples(target: str, ticks: list[int], rows: list[dict]):
    sql = f"INSERT INTO samples({','.join(SAMPLE_COLS)}) VALUES ({','.join('?' * len(SAMPLE_COLS))})"
    data = [tuple(r.get(c) for c in SAMPLE_COLS) for r in rows]
    with _write_lock:
        c = conn()
        c.executemany("INSERT OR IGNORE INTO ticks(target, ts) VALUES (?, ?)", [(target, t) for t in ticks])
        if data:
            c.executemany(sql, data)
        c.commit()


def save_sqltext(target: str, items: list[tuple[str, str, str]]):
    with _write_lock:
        c = conn()
        c.executemany(
            "INSERT OR REPLACE INTO sqltext(target, sql_id, text, opname) VALUES (?, ?, ?, ?)",
            [(target, s, t, o) for s, t, o in items],
        )
        c.commit()


def missing_sqltext(target: str, since: int, limit: int = 200) -> list[str]:
    rows = conn().execute(
        """SELECT DISTINCT s.sql_id FROM samples s
           WHERE s.target = ? AND s.ts >= ? AND s.sql_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM sqltext t WHERE t.target = s.target AND t.sql_id = s.sql_id)
           LIMIT ?""",
        (target, since, limit),
    ).fetchall()
    return [r[0] for r in rows]


def save_snapshot(target: str, ts: int, startup: str, blob: bytes, manual: bool = False) -> int:
    with _write_lock:
        c = conn()
        cur = c.execute("INSERT INTO snapshots(target, ts, startup, manual, data) VALUES (?, ?, ?, ?, ?)",
                        (target, ts, startup, int(manual), blob))
        c.commit()
        return cur.lastrowid


def get_snapshot(target: str, snap_id: int):
    return conn().execute("SELECT snap_id, ts, startup, data FROM snapshots WHERE target = ? AND snap_id = ?",
                          (target, snap_id)).fetchone()


def list_snapshots(target: str):
    return [dict(r) for r in conn().execute(
        "SELECT snap_id, ts, startup, manual, length(data) size FROM snapshots WHERE target = ? ORDER BY ts",
        (target,))]


def last_snapshot_ts(target: str) -> int:
    r = conn().execute("SELECT MAX(ts) FROM snapshots WHERE target = ?", (target,)).fetchone()
    return r[0] or 0


def purge(retention_days: float):
    cutoff = int(time.time() - retention_days * 86400)
    with _write_lock:
        c = conn()
        c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        c.commit()


# ----------------------------------------------------------------------------- saved connections (desktop)

_CONN_FIELDS = ("id", "name", "host", "port", "service", "user", "password", "mode", "source", "load", "target")


def load_connections() -> list[dict]:
    out = []
    for r in conn_db().execute("SELECT * FROM connections ORDER BY pos"):
        c = json.loads(r["extra"]) if r["extra"] else {}
        c.update({"id": r["id"], "name": r["name"], "host": r["host"] or "", "port": r["port"] or 1521,
                  "service": r["service"] or "", "user": r["username"] or "", "password": r["password"] or "",
                  "mode": r["mode"] or "", "source": r["source"] or "vsession",
                  "load": {"enabled": bool(r["load_enabled"])}})
        if r["target"]:
            c["target"] = r["target"]
        out.append(c)
    return out


def save_connections(items: list[dict]):
    """Replace the whole list (the page sends it complete after every add, edit or delete)."""
    now = int(time.time())
    rows = []
    for pos, c in enumerate(i for i in items if isinstance(i, dict) and i.get("id")):
        load = c.get("load") if isinstance(c.get("load"), dict) else {}
        extra = {k: v for k, v in c.items() if k not in _CONN_FIELDS}
        try:
            port = int(c.get("port") or 1521)
        except (TypeError, ValueError):
            port = 1521
        rows.append((str(c["id"]), pos, str(c.get("name") or ""), c.get("host"), port, c.get("service"),
                     c.get("user"), c.get("password"), c.get("mode"), c.get("source"),
                     1 if load.get("enabled") else 0, c.get("target"),
                     json.dumps(extra, ensure_ascii=False) if extra else None, now))
    with _write_lock:
        db = conn_db()
        with db:                                   # one transaction: the list is never half written
            db.execute("DELETE FROM connections")
            db.executemany("INSERT INTO connections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def count_connections() -> int:
    return conn_db().execute("SELECT COUNT(*) FROM connections").fetchone()[0]


def adopt_connections_from_history() -> int:
    """A separate connections database starts empty: take over the list the history database already holds
    (the previous desktop release kept it there). The history copy is left as it is."""
    if not CONN_DB_PATH or count_connections():
        return 0
    if not conn().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connections'").fetchone():
        return 0
    rows = conn().execute("SELECT * FROM connections ORDER BY pos").fetchall()
    if not rows:
        return 0
    with _write_lock:
        db = conn_db()
        with db:
            db.executemany("INSERT OR IGNORE INTO connections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [tuple(r) for r in rows])
    return len(rows)
