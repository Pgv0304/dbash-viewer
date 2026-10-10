"""Database kinds: Oracle (app.ora) and PostgreSQL (app.pg).

Everything the common part (main.py) needs from a kind goes through here: collectors, login check,
error hints, load generator and the performance report. A target's kind comes from its "type":
oracle / demo -> "oracle", postgres / postgresql / demo_pg -> "postgres".
"""
from __future__ import annotations

from .ora import awr as ora_awr, collectors as ora, loadgen as ora_load
from .pg import awr as pg_awr, collectors as pg, loadgen as pg_load

KINDS = ("oracle", "postgres")
LABEL = {"oracle": "Oracle", "postgres": "PostgreSQL"}
DEFAULT_PORT = {"oracle": 1521, "postgres": pg.DEFAULT_PORT}
_MOD = {"oracle": ora, "postgres": pg}
_LOAD = {"oracle": ora_load, "postgres": pg_load}
_AWR = {"oracle": ora_awr, "postgres": pg_awr}


def kind_of(cfg_or_type) -> str:
    t = (cfg_or_type.get("type") if isinstance(cfg_or_type, dict) else cfg_or_type) or "oracle"
    t = str(t).lower()
    return "postgres" if t in ("postgres", "postgresql", "pg", "demo_pg") else "oracle"


def norm_kind(value) -> str:
    """Kind from a form/API value; anything unknown is Oracle (the original application)."""
    return kind_of(str(value or "oracle"))


def make(cfg: dict):
    t = str(cfg.get("type", "oracle")).lower()
    if t == "demo":
        return ora.DemoCollector(cfg)
    if t == "demo_pg":
        return pg.DemoCollector(cfg)
    return _MOD[kind_of(t)].make(cfg)


def test_login(kind: str, dsn: str, user: str, password: str, mode: str = "") -> dict:
    return _MOD[kind].test_login(dsn, user, password, mode)


def explain(kind: str, msg: str) -> str:
    return _MOD[kind].explain(msg)


def auth_failure(kind: str, msg: str) -> bool:
    if kind == "oracle":
        return "ORA-01017" in msg or "ORA-28000" in msg
    return pg.auth_failure(msg)


def bad_password(kind: str) -> str:
    """The message for a wrong password checked locally against the running collector."""
    return explain(kind, "ORA-01017" if kind == "oracle" else "password authentication failed")


def load_generator(kind: str, cfg: dict, password: str):
    return _LOAD[kind].LoadGenerator(cfg, password)


def report(kind: str, target: str, begin: int, end: int) -> dict:
    rep = _AWR[kind].report(target, begin, end)
    rep["kind"] = kind
    return rep
