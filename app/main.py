"""DB ASH Viewer - FastAPI application: Active Session History for Oracle and PostgreSQL."""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
from typing import Optional
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import dbtypes as dbt, store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dbash")

CONFIG_PATH = os.environ.get("ASHV_CONFIG", "/config/targets.json")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="DB ASH Viewer")
COLLECTORS: dict = {}                                 # target name -> collector (app.ora / app.pg)
CONFIG: dict = {}
_lock = threading.Lock()

# Browser sessions: token -> {"targets": set(names), "exp": epoch}. Kept in memory only:
# after a restart everybody simply logs in again.
SESSIONS: dict[str, dict] = {}
COOKIE = "dbash_session"
_fails: dict[str, list[float]] = {}   # client ip -> timestamps of failed logins
LOADGEN = None                                         # load of the server's own (configured) database
LOADGENS: dict = {}                                    # target name -> running load generator
MAX_USER_LOAD_SESSIONS = 3


def demo_mode() -> bool:
    """ASHV_MODE: normal - connection parameters and password are asked (default);
    demo - visitors are connected to the default database at once, and a random load runs on it."""
    return os.environ.get("ASHV_MODE", "normal").strip().lower() == "demo"

# Dimensions that may be used for grouping / filtering (whitelist -> SQL expression)
DIMS = {
    "wait_class": "wait_class",
    "event": "event",
    "sql_id": "sql_id",
    "username": "username",
    "program": "program",
    "module": "module",
    "machine": "machine",
    "sql_opname": "sql_opname",
    "plan_hash": "plan_hash",
    "session": "inst_id || ':' || sid || ',' || serial",
    "blocking_session": "blocking_inst || ':' || blocking_sid",
    "session_type": "session_type",
}


def read_conn_file(path: str) -> dict:
    """pg.txt: lines "Host: …", "Port: …", "Database: …", "User: …", "Password: …" (keys in any case)."""
    vals = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            m = re.match(r"\s*([A-Za-z_ ]+?)\s*[:=]\s*(.*?)\s*$", line)
            if m:
                vals[m.group(1).strip().lower()] = m.group(2)
    host = vals.get("host") or vals.get("hostname") or "localhost"
    port = int(vals.get("port") or dbt.DEFAULT_PORT["postgres"])
    db = vals.get("database") or vals.get("dbname") or vals.get("db") or "postgres"
    user = vals.get("user") or vals.get("username") or "postgres"
    return {"host": host, "port": port, "database": db, "user": user, "password": vals.get("password", "")}


def _conn_file_target():
    """The connection of pg.txt (PGASH_CONN_FILE, or pg.txt next to the application) becomes the default
    database: first in the list and opened without asking for the password (it is in the file)."""
    candidates = [os.environ.get("PGASH_CONN_FILE", ""), str(Path(__file__).parent.parent / "pg.txt"), "pg.txt"]
    path = next((p for p in candidates if p and os.path.isfile(p)), None)
    if not path:
        return None
    try:
        c = read_conn_file(path)
    except (OSError, ValueError) as e:
        log.warning("connection file %s unreadable: %s", path, e)
        return None
    # в Docker «localhost» из pg.txt — это сам контейнер: адрес подменяется по host_aliases (host.docker.internal)
    dsn = f'{_resolve_host(c["host"])}:{c["port"]}/{c["database"]}'
    name = f'{c["database"]}@{c["host"]}' + ("" if c["port"] == dbt.DEFAULT_PORT["postgres"] else f':{c["port"]}')
    targets = CONFIG.setdefault("targets", [])
    targets[:] = [t for t in targets if t.get("name") != name]
    targets.insert(0, {"name": name, "type": "postgres", "dsn": dsn,
                       "user": c["user"], "password": c["password"], "interval": 1, "source_file": path})
    old = CONFIG.get("demo_target")
    if old and old != name:
        # the configured server database stays the default one; pg.txt joins the list of server databases
        CONFIG.setdefault("server_targets", [old, name])
        if CONFIG.get("demo_load"):
            CONFIG.setdefault("load_target", old)
    else:
        CONFIG["demo_target"] = name
        # «Нагрузка» button for this database: same account, switched on by hand only
        CONFIG.setdefault("demo_load", {"type": "postgres", "dsn": dsn,
                                        "user": c["user"], "password": c["password"], "max_sessions": 3,
                                        "autostart": False})
    CONFIG.setdefault("open_server_db", True)
    CONFIG.setdefault("login_default", {"type": "postgres", "host": c["host"], "port": c["port"],
                                        "service": c["database"], "user": c["user"]})
    log.info("connection from %s: %s@%s:%s/%s", path, c["user"], c["host"], c["port"], c["database"])
    return name


def load_config():
    global CONFIG
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            CONFIG = json.load(f)
    else:
        log.info("config %s not found: the database of pg.txt (or the synthetic demo database)", CONFIG_PATH)
        CONFIG = {"open_server_db": True}
    pg_target = _conn_file_target()
    # A single target may also be configured purely through environment variables
    if os.environ.get("ORACLE_DSN"):
        CONFIG.setdefault("targets", []).append({
            "name": os.environ.get("ORACLE_NAME", "ORACLE"), "type": "oracle",
            "dsn": os.environ["ORACLE_DSN"], "user": os.environ.get("ORACLE_USER", "ashv"),
            "password_env": "ORACLE_PASSWORD", "source": os.environ.get("ORACLE_SOURCE", "vsession"),
            "rac": os.environ.get("ORACLE_RAC", "0") == "1",
            "interval": float(os.environ.get("ORACLE_INTERVAL", "1")),
        })
    if os.environ.get("PG_DSN"):
        CONFIG.setdefault("targets", []).append({
            "name": os.environ.get("PG_NAME", "POSTGRES"), "type": "postgres",
            "dsn": os.environ["PG_DSN"], "user": os.environ.get("PG_USER", "postgres"),
            "password_env": "PG_PASSWORD", "interval": float(os.environ.get("PG_INTERVAL", "1")),
        })
    if not pg_target and not CONFIG.get("targets"):
        CONFIG.update({"targets": [{"name": "Демо (синтетическая)", "type": "demo", "backfill_hours": 1}],
                       "demo_target": "Демо (синтетическая)"})


def _housekeeping():
    while True:
        try:
            store.purge(float(CONFIG.get("retention_days", 3)))
            store.purge_login_log(float(CONFIG.get("login_log_days", LOGIN_LOG_DAYS)))
        except Exception as e:  # noqa: BLE001
            log.warning("purge failed: %s", e)
        time.sleep(3600)


def _reaper():
    """Stop collectors created from the login form once nobody has looked at them for a while."""
    while True:
        time.sleep(300)
        idle = float(CONFIG.get("dynamic_idle_hours", 12)) * 3600
        now = time.time()
        with _lock:
            for tok in [t for t, v in SESSIONS.items() if v["exp"] < now]:
                SESSIONS.pop(tok, None)
            for name, c in list(COLLECTORS.items()):
                if c.cfg.get("dynamic") and now - c.cfg.get("last_access", now) > idle:
                    log.info("stopping idle collector %s", name)
                    _stop_target(name)


@app.on_event("startup")
def startup():
    store.init()
    _import_conn_file()
    load_config()
    for t in CONFIG.get("targets", []):
        if not t.get("enabled", True):
            continue
        c = dbt.make(t)
        COLLECTORS[t["name"]] = c
        c.start()
    threading.Thread(target=_housekeeping, daemon=True, name="housekeeping").start()
    threading.Thread(target=_reaper, daemon=True, name="reaper").start()
    load_cfg = CONFIG.get("demo_load") or {}
    # on at start: always in demo mode, otherwise only when the config asks for it (autostart: true)
    if load_cfg and load_cfg.get("enabled", True) and (demo_mode() or load_cfg.get("autostart")):
        _start_server_load()


# ----------------------------------------------------------------------------- auth

def _resolve_host(host: str) -> str:
    """Address used from inside the container (e.g. the public IP of this very host -> host gateway)."""
    aliases = {k.lower(): v for k, v in CONFIG.get("host_aliases", {}).items()}
    return aliases.get(host.lower(), host)


def _key(dsn: str, kind: str = "oracle") -> str:
    """Normalised kind:host:port/service used to recognise the same database (PostgreSQL names keep their case)."""
    m = re.match(r"^\s*([^:/\s]+)(?::(\d+))?/([^\s]+)\s*$", dsn or "")
    if not m:
        return f"{kind}:" + (dsn or "").strip().lower()
    host, port, svc = m.groups()
    return f"{kind}:{_resolve_host(host).lower()}:{port or dbt.DEFAULT_PORT[kind]}/{svc if kind == 'postgres' else svc.lower()}"


def _session(request: Request):
    tok = getattr(request.state, "demo_token", None) or request.cookies.get(COOKIE)
    s = SESSIONS.get(tok) if tok else None
    if s and s.get("demo") and not demo_mode():
        SESSIONS.pop(tok, None)  # demo mode switched off: sessions opened without a password are void
        return None, None
    if s and s["exp"] > time.time():
        return tok, s
    return None, None


def _demo_target():
    name = CONFIG.get("demo_target")
    if name in COLLECTORS:
        return name
    return next((n for n, c in COLLECTORS.items() if not c.cfg.get("dynamic")), None)


def _server_targets():
    """The server's own databases the list shows first and opens without a password, in config order
    ("server_targets"; by default just the demo database)."""
    names = CONFIG.get("server_targets") or [_demo_target()]
    return [n for n in names if n in COLLECTORS]


def _load_target():
    """The server database the demo_load generator loads («Нагрузка» button); by default the demo database."""
    name = CONFIG.get("load_target") or _demo_target()
    return name if name in COLLECTORS and CONFIG.get("demo_load") else None


def _server_db_info(name: str) -> dict:
    cfg = COLLECTORS[name].cfg
    synthetic = cfg.get("type") == "demo"
    desc = cfg.get("display") or ("синтетическая: имитация нагрузки без подключения к Oracle" if synthetic
                                  else f'{cfg.get("user", "")}@{cfg.get("dsn", "")}')
    return {"name": name, "desc": desc, "synthetic": synthetic, "kind": dbt.kind_of(cfg),
            "load_available": name == _load_target(), "load": bool(LOADGENS.get(name))}


# ----------------------------------------------------------------------------- login journal

LOGIN_LOG_DAYS = 92          # three months


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def _audit(request: Request, event: str, ok: bool, **kw):
    """One record of the login journal; never the password. A failure here must not break the login."""
    try:
        store.log_login(event=event, ok=ok, ip=_client_ip(request),
                        user_agent=request.headers.get("user-agent"), **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("login journal: %s", e)


def _journal_access(request: Request) -> bool:
    """The journal holds visitors' addresses: it is open on the user's own computer (desktop version)
    and on a server only with the administrator key (ASHV_ADMIN_TOKEN, header X-Admin-Token or ?token=)."""
    token = os.environ.get("ASHV_ADMIN_TOKEN", "")
    given = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    if token and given and hmac.compare_digest(token.encode(), given.encode()):
        return True
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    proxied = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for")
    return (not proxied and (request.client.host if request.client else "") in ("127.0.0.1", "::1")
            and host in ("127.0.0.1", "localhost", "::1"))


def _ensure_demo_session(request: Request):
    """Demo mode: a visitor without a session is connected to the default database at once."""
    if not demo_mode() or _session(request)[1]:
        return None
    targets = _server_targets()
    if not targets:
        return None
    target = targets[0]
    tok = secrets.token_urlsafe(32)
    with _lock:
        SESSIONS[tok] = {"targets": set(targets), "demo": True,
                         "exp": time.time() + float(CONFIG.get("session_hours", 12)) * 3600}
    request.state.demo_token = tok
    _audit(request, "demo", True, target=target, kind=dbt.kind_of(COLLECTORS[target].cfg) if target in COLLECTORS else None,
           message="демо-режим: вход без пароля")
    log.info("demo session for %s opened from %s", target,
             request.headers.get("x-real-ip") or (request.client.host if request.client else "?"))
    return tok


def _set_cookie(request: Request, response, tok: str):
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, tok, httponly=True, secure=secure, samesite="lax",
                        max_age=int(float(CONFIG.get("session_hours", 12)) * 3600))


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    new_demo = (_ensure_demo_session(request)
                if path.startswith("/api/") and path not in ("/api/health", "/api/defaults", "/api/logout",
                                                             "/api/local-connections", "/api/login-log",
                                                             "/api/login-log.csv") else None)
    if new_demo:
        response = await require_login_inner(request, call_next)
        _set_cookie(request, response, new_demo)
        return response
    return await require_login_inner(request, call_next)


async def require_login_inner(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/login", "/api/logout", "/api/defaults", "/api/health", "/api/me",
                                                 "/api/connect-server", "/api/server-load", "/api/local-connections",
                                                 "/api/login-log", "/api/login-log.csv"):
        _, s = _session(request)
        if not s:
            return JSONResponse({"detail": "login required"}, status_code=401)
        t = request.query_params.get("target")
        if t is not None:
            if t not in s["targets"]:
                return JSONResponse({"detail": "no access to this database"}, status_code=403)
            c = COLLECTORS.get(t)
            if c:
                c.cfg["last_access"] = time.time()
    return await call_next(request)


@app.get("/api/defaults")
def defaults(request: Request):
    """Connection form defaults and the server's own (demo) database, which the list shows first."""
    d = CONFIG.get("login_default", {})
    kind = dbt.norm_kind(d.get("type"))
    return {"type": kind, "kinds": [{"value": k, "label": dbt.LABEL[k], "port": dbt.DEFAULT_PORT[k]} for k in dbt.KINDS],
            "host": d.get("host", ""), "port": d.get("port", dbt.DEFAULT_PORT[kind]), "service": d.get("service", ""),
            "user": d.get("user", ""), "source": d.get("source", "vsession"),
            "demo": demo_mode(), "demo_target": _demo_target(),
            "server_dbs": [_server_db_info(n) for n in _server_targets()],
            "server_open": _server_open(), "server_load": bool(_load_target() and LOADGENS.get(_load_target())),
            "server_load_available": bool(_load_target()),
            "max_load_sessions": MAX_USER_LOAD_SESSIONS,
            "local_store": LOCAL_STORE, "login_log_local": _journal_access(request)}


# Desktop version: the saved connections live in SQLite — table connections in ASHV_CONN_DB, or in ASHV_DB if unset.
# The window's own storage is tied to http://127.0.0.1:<port> and is lost when the port or the WebView changes.
# ASHV_CONN_FILE is the JSON file of the previous desktop release: imported once, then renamed *.imported.
LOCAL_STORE = os.environ.get("ASHV_LOCAL_STORE") == "1" or bool(os.environ.get("ASHV_CONN_FILE"))
CONN_FILE = os.environ.get("ASHV_CONN_FILE", "")


def _import_conn_file():
    if LOCAL_STORE:
        n = store.adopt_connections_from_history()
        if n:
            log.info("saved connections copied from the history database: %d", n)
    if not (LOCAL_STORE and CONN_FILE and os.path.isfile(CONN_FILE)):
        return
    try:
        with open(CONN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data and store.count_connections() == 0:
            log.info("saved connections imported from %s: %d", CONN_FILE, store.save_connections(data))
        os.replace(CONN_FILE, CONN_FILE + ".imported")
    except (OSError, ValueError) as e:
        log.warning("saved connections file not imported: %s", e)


def _local_only(request: Request):
    if not LOCAL_STORE:
        raise HTTPException(404, "not available")
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1") or \
            host not in ("127.0.0.1", "localhost", "::1"):     # also refuses DNS-rebinding pages
        raise HTTPException(403, "local access only")


@app.get("/api/local-connections")
def local_connections(request: Request):
    _local_only(request)
    return store.load_connections()


@app.put("/api/local-connections")
def save_local_connections(request: Request, items: list = Body(...)):
    _local_only(request)
    return {"ok": True, "count": store.save_connections(items)}


def _server_open() -> bool:
    """The server's own (demo) database opens without a password: its credentials live in the config."""
    return demo_mode() or bool(CONFIG.get("open_server_db", True))


def _start_server_load():
    global LOADGEN
    load_cfg = CONFIG.get("demo_load")
    if not load_cfg or not _load_target():
        return False
    if LOADGEN is None or LOADGEN.status == "stopped":
        kind = dbt.norm_kind(load_cfg.get("type") or COLLECTORS[_load_target()].cfg.get("type"))
        LOADGEN = dbt.load_generator(kind, load_cfg, load_cfg.get("password")
                                     or os.environ.get(load_cfg.get("password_env", ""), ""))
        LOADGEN.start()
        log.info("load generator of the server database started")
    LOADGENS[_load_target()] = LOADGEN
    return True


@app.post("/api/server-load")
def server_load(request: Request, enabled: bool = Body(..., embed=True)):
    """Switch the load generator of the server's own database on or off (the list's «Нагрузка» button).
    Like the database itself, it needs no password when the server database is open."""
    global LOADGEN
    if not _server_open() and not _session(request)[1]:
        raise HTTPException(401, "login required")
    if not CONFIG.get("demo_load"):
        raise HTTPException(409, "Для базы сервера не настроен пользователь нагрузки (блок demo_load в конфигурации)")
    with _lock:
        if enabled:
            _start_server_load()
        elif LOADGEN:
            LOADGEN.stop()
            LOADGENS.pop(_load_target(), None)
            LOADGEN = None
            log.info("load generator of the server database stopped")
    return {"server_load": bool(LOADGEN)}


@app.post("/api/connect-server")
def connect_server(request: Request, response: Response, body: Optional[dict] = Body(None)):
    """Connect this browser session to one of the server's own databases without asking for a password."""
    targets = _server_targets()
    target = (body or {}).get("target") or (targets[0] if targets else None)
    if target not in targets:
        raise HTTPException(404, "Нет такой базы сервера")
    if not _server_open():
        raise HTTPException(403, "Вход в базу сервера без пароля отключён")
    with _lock:
        tok, s = _session(request)
        if not s:
            tok = secrets.token_urlsafe(32)
            s = SESSIONS[tok] = {"targets": set()}
        s["targets"].add(target)
        s["exp"] = time.time() + float(CONFIG.get("session_hours", 12)) * 3600
    c = COLLECTORS.get(target)
    _audit(request, "server", True, target=target, kind=dbt.kind_of(c.cfg) if c else None,
           db_user=c.cfg.get("user") if c else None, dsn=c.cfg.get("dsn") if c else None,
           message="база сервера без пароля")
    _set_cookie(request, response, tok)
    return {"target": target}


@app.get("/api/me")
def me(request: Request):
    _, s = _session(request)
    return {"authenticated": bool(s), "targets": sorted(s["targets"]) if s else [], "demo": demo_mode(),
            "load": LOADGEN.describe() if LOADGEN else None}


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    return len(_fails[ip]) >= int(CONFIG.get("max_login_failures", 10))


@app.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    if _too_many_fails(ip):
        _audit(request, "blocked", False, message="слишком много неудачных попыток за 10 минут")
        raise HTTPException(429, "Слишком много неудачных попыток входа, повторите через 10 минут")
    host = str(body.get("host", "")).strip()
    service = str(body.get("service", "")).strip()
    user = str(body.get("user", "")).strip()
    password = str(body.get("password", ""))
    kind = dbt.norm_kind(body.get("type"))
    mode = "SYSDBA" if kind == "oracle" and str(body.get("mode", "")).upper() == "SYSDBA" else ""
    source = "ash" if kind == "oracle" and body.get("source") == "ash" else "vsession"
    what = "сервис" if kind == "oracle" else "базу данных"
    try:
        port = int(body.get("port") or dbt.DEFAULT_PORT[kind])
    except ValueError:
        raise HTTPException(400, "Неверный порт")
    if not (host and service and user and password):
        raise HTTPException(400, f"Заполните хост, {what}, пользователя и пароль")
    if not re.fullmatch(r"[\w.\-]+", host) or not re.fullmatch(r"[\w.\-$#]+", service):
        raise HTTPException(400, "Недопустимые символы в хосте или имени " + ("сервиса" if kind == "oracle" else "базы данных"))

    dsn = f"{_resolve_host(host)}:{port}/{service}"
    key = _key(dsn, kind)
    with _lock:
        existing = next((c for c in COLLECTORS.values()
                         if c.cfg.get("dsn") and _key(c.cfg["dsn"], dbt.kind_of(c.cfg)) == key), None)

    # Same account as the running collector: check locally, so that typos in the form
    # never count against the monitoring account's FAILED_LOGIN_ATTEMPTS in Oracle.
    if (existing and not existing.cfg.get("dynamic") and not mode
            and existing.cfg.get("user", "").lower() == user.lower()):
        if not hmac.compare_digest(existing._password().encode(), password.encode()):
            _fails.setdefault(ip, []).append(time.time())
            _audit(request, "login", False, kind=kind, db_user=user, dsn=dsn, target=existing.name_,
                   message=dbt.bad_password(kind))
            raise HTTPException(401, dbt.bad_password(kind))
    else:
        try:
            dbt.test_login(kind, dsn, user, password, mode)
        except Exception as e:  # noqa: BLE001
            text = str(e).strip()
            msg = dbt.explain(kind, text.splitlines()[0][:300] if text else repr(e))
            if dbt.auth_failure(kind, msg):
                _fails.setdefault(ip, []).append(time.time())
            log.info("login failed for %s@%s from %s: %s", user, key, ip, msg)
            _audit(request, "login", False, kind=kind, db_user=user, dsn=dsn, message=msg)
            raise HTTPException(401, msg)

    with _lock:
        if existing is None or (existing.cfg.get("dynamic") and existing.status == "error"):
            if existing is not None:
                existing.stop()
                COLLECTORS.pop(existing.name_, None)
            name = (f"{service.upper()}@{host}" if kind == "oracle" else f"{service}@{host}") \
                + ("" if port == dbt.DEFAULT_PORT[kind] else f":{port}")
            if name in COLLECTORS:                     # same name, other kind of database
                name += f" ({dbt.LABEL[kind]})"
            cfg = {"name": name, "type": kind, "dsn": dsn, "user": user, "password": password,
                   "mode": mode, "source": source, "interval": 1, "rac": False,
                   "include_background": True, "dynamic": True, "last_access": time.time()}
            existing = dbt.make(cfg)
            COLLECTORS[name] = existing
            existing.start()
            log.info("started collector %s for %s", name, user)
        tok, s = _session(request)
        if not s:
            tok = secrets.token_urlsafe(32)
            s = SESSIONS[tok] = {"targets": set()}
        s["targets"].add(existing.name_)
        s["exp"] = time.time() + float(CONFIG.get("session_hours", 12)) * 3600
        note = _apply_load(existing, dsn, user, password, body.get("load") or {})
    _set_cookie(request, response, tok)
    _audit(request, "login", True, kind=kind, db_user=user, dsn=dsn, target=existing.name_,
           message=("SYSDBA; " if mode else "") + ("нагрузочное тестирование" if (body.get("load") or {}).get("enabled") else ""))
    return {"target": existing.name_, "load_note": note}


def _apply_load(c, dsn: str, user: str, password: str, load: dict):
    """Start, restart or stop the load generator of a database added from the form (off by default).
    The configured (demo) databases keep the load the server configuration gives them."""
    global LOADGEN
    enabled = bool(load.get("enabled"))
    if not c.cfg.get("dynamic"):
        # The form's switch is the database's switch: for the server's own database it drives the same
        # generator as the list's «Нагрузка» button (the last choice wins).
        if c.name_ != _load_target():
            return "Для этой базы нагрузочное тестирование не настроено на сервере" if enabled else None
        if enabled:
            _start_server_load()
        elif LOADGEN:
            LOADGEN.stop()
            LOADGENS.pop(c.name_, None)
            LOADGEN = None
            log.info("load generator of the server database stopped from the connection form")
        return None
    name = c.name_
    cur = LOADGENS.get(name)
    if not enabled:
        if cur:
            cur.stop()
            LOADGENS.pop(name, None)
            log.info("load generator for %s stopped", name)
        return None
    # the load runs under the connection's own account: the form only switches it on or off
    luser, lpass, sessions = user, password, 2
    if (cur and cur.cfg.get("user", "").lower() == luser.lower() and cur.password == lpass
            and cur.max_sessions == sessions and cur.status != "error"):
        return None
    if cur:
        cur.stop()
    g = dbt.load_generator(dbt.kind_of(c.cfg), {"dsn": dsn, "user": luser, "max_sessions": sessions}, lpass)
    g.start()
    LOADGENS[name] = g
    log.info("load generator for %s started as %s, %d sessions", name, luser, sessions)
    return None


def _stop_target(name: str):
    """Stop a database added from the form: its load generator and its collector (history stays)."""
    g = LOADGENS.pop(name, None)
    if g and g is not LOADGEN:
        g.stop()
    c = COLLECTORS.get(name)
    if c and c.cfg.get("dynamic"):
        c.stop()
        COLLECTORS.pop(name, None)


@app.post("/api/disconnect")
def disconnect(request: Request, target: str):
    """Remove a database from this browser session (when its saved connection is deleted).
    Its collector and load stop once no other session uses it."""
    tok, s = _session(request)
    with _lock:
        if s:
            s["targets"].discard(target)
        in_use = any(target in v["targets"] for v in SESSIONS.values())
        if not in_use:
            _stop_target(target)
    return {"ok": True, "stopped": not in_use}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    tok, s = _session(request)
    if s:
        _audit(request, "logout", True, target=", ".join(sorted(s["targets"])) or None)
    if tok:
        SESSIONS.pop(tok, None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ----------------------------------------------------------------------------- helpers

def _target(name: str):
    if name not in COLLECTORS:
        raise HTTPException(404, f"unknown target {name}")
    return COLLECTORS[name]


def _where(request: Request, target: str, t_from: int, t_to: int):
    """Build WHERE clause from time range + any filter query params (f_<dim>=value)."""
    clauses = ["target = ?", "ts >= ?", "ts < ?"]
    args: list = [target, t_from, t_to]
    for key, val in request.query_params.multi_items():
        if not key.startswith("f_"):
            continue
        dim = key[2:]
        if dim not in DIMS:
            raise HTTPException(400, f"bad filter {dim}")
        if val == "":
            clauses.append(f"({DIMS[dim]}) IS NULL")
        else:
            clauses.append(f"CAST(({DIMS[dim]}) AS TEXT) = ?")
            args.append(val)
    return " AND ".join(clauses), args


def _range(t_from, t_to):
    now = int(time.time())
    t_to = int(t_to) if t_to else now + 1
    t_from = int(t_from) if t_from else t_to - 900
    if t_to <= t_from:
        raise HTTPException(400, "empty range")
    return t_from, t_to


def _ticks(target, t_from, t_to):
    r = store.conn().execute("SELECT COUNT(*) FROM ticks WHERE target=? AND ts>=? AND ts<?",
                             (target, t_from, t_to)).fetchone()
    return r[0] or 0


# ----------------------------------------------------------------------------- API

@app.get("/api/targets")
def targets(request: Request):
    _, s = _session(request)
    out = []
    for n, c in COLLECTORS.items():
        if n in s["targets"]:
            d = c.describe()
            d["kind"] = dbt.kind_of(c.cfg)
            g = LOADGENS.get(n)
            d["load"] = {**g.describe(), "user": g.cfg.get("user"), "sessions": g.max_sessions,
                         "configured": not c.cfg.get("dynamic")} if g else None
            out.append(d)
    return out


@app.get("/api/activity")
def activity(request: Request, target: str, t_from: Optional[int] = Query(None, alias="from"),
             t_to: Optional[int] = Query(None, alias="to"), group: str = "wait_class",
             points: int = 300, top: int = 12):
    """Average active sessions over time, stacked by `group`."""
    _target(target)
    if group not in DIMS:
        raise HTTPException(400, "bad group")
    t_from, t_to = _range(t_from, t_to)
    span = t_to - t_from
    nice = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400]
    bucket = next((b for b in nice if span / b <= points), nice[-1])
    t_from = t_from // bucket * bucket
    where, args = _where(request, target, t_from, t_to)
    db = store.conn()
    expr = DIMS[group]

    # top-N keys over the whole range, the rest is folded into "Other"
    keys = [r[0] for r in db.execute(
        f"SELECT ({expr}) k, COUNT(*) c FROM samples WHERE {where} GROUP BY k ORDER BY c DESC LIMIT ?",
        args + [top]).fetchall()]
    rows = db.execute(
        f"SELECT (ts / ?) * ? b, ({expr}) k, COUNT(*) c FROM samples WHERE {where} GROUP BY b, k",
        [bucket, bucket] + args).fetchall()
    tick_rows = db.execute(
        "SELECT (ts / ?) * ? b, COUNT(*) FROM ticks WHERE target=? AND ts>=? AND ts<? GROUP BY b",
        (bucket, bucket, target, t_from, t_to)).fetchall()
    ticks = {b: n for b, n in tick_rows}

    buckets = list(range(t_from, t_to, bucket))
    series: dict = {}
    keyset = set(keys)
    for b, k, c in rows:
        name = k if k in keyset else "__other__"
        series.setdefault(name, {})
        series[name][b] = series[name].get(b, 0) + c
    out = []
    for k in keys + (["__other__"] if "__other__" in series else []):
        vals = []
        for b in buckets:
            n = ticks.get(b, 0)
            vals.append(round(series.get(k, {}).get(b, 0) / n, 3) if n else None)
        out.append({"name": "Прочее" if k == "__other__" else (k if k is not None else "—"),
                    "key": k, "data": vals})
    c = COLLECTORS[target]
    return {"from": t_from, "to": t_to, "bucket": bucket, "times": buckets, "series": out,
            "cpu_count": c.info.get("cpu_count")}


@app.get("/api/top")
def top(request: Request, target: str, dim: str = "sql_id",
        t_from: Optional[int] = Query(None, alias="from"), t_to: Optional[int] = Query(None, alias="to"),
        limit: int = 30):
    """Top-N of a dimension with wait-class breakdown (like ASH Viewer's Top SQL / Top Sessions)."""
    _target(target)
    if dim not in DIMS:
        raise HTTPException(400, "bad dim")
    t_from, t_to = _range(t_from, t_to)
    where, args = _where(request, target, t_from, t_to)
    db = store.conn()
    expr = DIMS[dim]
    total = db.execute(f"SELECT COUNT(*) FROM samples WHERE {where}", args).fetchone()[0] or 0
    ticks = _ticks(target, t_from, t_to) or 1

    extra = ""
    if dim == "sql_id":
        extra = ", MAX(sql_opname) op, COUNT(DISTINCT plan_hash) plans"
    elif dim == "session":
        extra = ", MAX(username) username, MAX(program) program, MAX(module) module, MAX(machine) machine, MAX(session_type) stype"
    elif dim == "event":
        extra = ", MAX(wait_class) wclass"
    tops = db.execute(
        f"SELECT ({expr}) k, COUNT(*) c {extra} FROM samples WHERE {where} AND ({expr}) IS NOT NULL "
        f"GROUP BY k ORDER BY c DESC LIMIT ?", args + [limit]).fetchall()
    keys = [r["k"] for r in tops]
    breakdown: dict = {}
    if keys:
        ph = ",".join("?" * len(keys))
        for k, wc, c in db.execute(
                f"SELECT ({expr}) k, wait_class, COUNT(*) FROM samples WHERE {where} AND ({expr}) IN ({ph}) "
                f"GROUP BY k, wait_class", args + keys):
            breakdown.setdefault(k, {})[wc or "Other"] = c
    texts = {}
    if dim == "sql_id" and keys:
        ph = ",".join("?" * len(keys))
        texts = {r[0]: r[1] for r in db.execute(
            f"SELECT sql_id, text FROM sqltext WHERE target=? AND sql_id IN ({ph})", [target] + keys)}
    rows = []
    for r in tops:
        d = dict(r)
        k = d.pop("k")
        c = d.pop("c")
        item = {"key": k, "samples": c, "pct": round(100.0 * c / total, 2) if total else 0,
                "aas": round(c / ticks, 3), "breakdown": breakdown.get(k, {}), **d}
        if dim == "sql_id":
            item["text"] = (texts.get(k) or "")[:300]
        rows.append(item)
    return {"total": total, "ticks": ticks, "rows": rows}


@app.get("/api/blocking")
def blocking(request: Request, target: str, t_from: Optional[int] = Query(None, alias="from"),
             t_to: Optional[int] = Query(None, alias="to")):
    """Blockers in the range and who/what they were blocking."""
    _target(target)
    t_from, t_to = _range(t_from, t_to)
    where, args = _where(request, target, t_from, t_to)
    db = store.conn()
    ticks = _ticks(target, t_from, t_to) or 1
    rows = db.execute(
        f"""SELECT blocking_inst || ':' || blocking_sid blocker, COUNT(*) c,
                   COUNT(DISTINCT inst_id || ':' || sid) waiters, MIN(ts) first_ts, MAX(ts) last_ts
              FROM samples WHERE {where} AND blocking_sid IS NOT NULL
             GROUP BY blocker ORDER BY c DESC LIMIT 50""", args).fetchall()
    out = []
    for r in rows:
        inst, sid = r["blocker"].split(":")
        ev = db.execute(
            f"""SELECT event, COUNT(*) c FROM samples WHERE {where} AND blocking_inst=? AND blocking_sid=?
                GROUP BY event ORDER BY c DESC LIMIT 5""", args + [int(inst), int(sid)]).fetchall()
        sq = db.execute(
            f"""SELECT sql_id, COUNT(*) c FROM samples WHERE {where} AND blocking_inst=? AND blocking_sid=?
                AND sql_id IS NOT NULL GROUP BY sql_id ORDER BY c DESC LIMIT 5""",
            args + [int(inst), int(sid)]).fetchall()
        # what the blocker itself was doing (if it was active and sampled)
        me = db.execute(
            """SELECT username, program, module, machine, MAX(ts) FROM samples
               WHERE target=? AND inst_id=? AND sid=? AND ts>=? AND ts<?""",
            (target, int(inst), int(sid), t_from, t_to)).fetchone()
        out.append({"blocker": r["blocker"], "samples": r["c"], "aas": round(r["c"] / ticks, 3),
                    "waiters": r["waiters"], "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                    "events": [dict(e) for e in ev], "sql_ids": [dict(s) for s in sq],
                    "blocker_info": {"username": me[0], "program": me[1], "module": me[2],
                                     "machine": me[3]} if me and me[4] else None})
    return {"rows": out}


@app.get("/api/sql/{sql_id}")
def sql_detail(sql_id: str, target: str):
    c = _target(target)
    try:
        d = c.sql_details(sql_id)
    except Exception as e:  # noqa: BLE001
        d = {"error": str(e).splitlines()[0]}
    if not d.get("text"):
        r = store.conn().execute("SELECT text FROM sqltext WHERE target=? AND sql_id=?",
                                 (target, sql_id)).fetchone()
        if r and r[0]:
            d["text"] = r[0]
    d["sql_id"] = sql_id
    return d


# ----------------------------------------------------------------------------- AWR-like reports

@app.get("/api/awr/snapshots")
def awr_snapshots(target: str):
    c = _target(target)
    return {"snapshots": store.list_snapshots(target),
            "interval_min": getattr(c, "snapshot_minutes", None)}


@app.post("/api/awr/snapshot")
def awr_take_snapshot(target: str):
    c = _target(target)
    try:
        sid = c.take_snapshot(manual=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, str(e).splitlines()[0][:300])
    return {"snap_id": sid}


@app.get("/api/awr/report")
def awr_report(target: str, begin: int, end: int):
    _target(target)
    try:
        return dbt.report(dbt.kind_of(COLLECTORS[target].cfg), target, begin, end)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/awr/native")
def awr_native(target: str):
    c = _target(target)
    try:
        return c.native_awr()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e).splitlines()[0][:300]}


@app.get("/api/awr/native/report")
def awr_native_report(target: str, begin: int, end: int):
    c = _target(target)
    try:
        html = c.native_awr_report(begin, end)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, str(e).splitlines()[0][:300])
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="awr_{target}_{begin}_{end}.html"'})


@app.get("/api/login-log")
def login_log_api(request: Request, days: float = 92, limit: int = 2000, event: str = "", ok: Optional[int] = None):
    if not _journal_access(request):
        raise HTTPException(403, "Журнал входов доступен на этом компьютере или по ключу администратора")
    days = max(0.01, min(days, float(CONFIG.get("login_log_days", LOGIN_LOG_DAYS))))
    since = int(time.time() - days * 86400)
    return {"days": days, "keep_days": CONFIG.get("login_log_days", LOGIN_LOG_DAYS),
            "stats": store.login_stats(since),
            "rows": store.login_log(since, max(1, min(limit, 20000)), event, None if ok is None else bool(ok))}


@app.get("/api/login-log.csv")
def login_log_csv(request: Request, days: float = 92):
    if not _journal_access(request):
        raise HTTPException(403, "Журнал входов доступен на этом компьютере или по ключу администратора")
    import csv
    import io
    from datetime import datetime
    since = int(time.time() - min(days, float(CONFIG.get("login_log_days", LOGIN_LOG_DAYS))) * 86400)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Время", "Событие", "Успешно", "IP", "База", "Тип", "Пользователь БД", "Подключение", "Сообщение", "Браузер"])
    for r in store.login_log(since, 1_000_000):
        w.writerow([datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"), r["event"], "да" if r["ok"] else "нет",
                    r["ip"], r["target"], r["kind"], r["db_user"], r["dsn"], r["message"], r["user_agent"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="login_log.csv"'})


@app.get("/api/health")
def health():
    return {"ok": True, "targets": {n: c.status for n, c in COLLECTORS.items()}, "demo": demo_mode(),
            "load": LOADGEN.describe() if LOADGEN else None}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _asset_version() -> str:
    h = hashlib.sha1()
    for f in sorted(STATIC.glob("*.*")):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()


@app.middleware("http")
async def no_stale_assets(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        # versioned URLs (?v=...) may be cached for long; anything else must be revalidated
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                         if request.query_params.get("v") else "no-cache")
    elif request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("intro.js", "app.js", "awr.js", "style.css", "vendor/echarts.min.js"):
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={ASSET_VERSION}\"")
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
