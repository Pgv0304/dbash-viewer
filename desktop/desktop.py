"""DB ASH Viewer — настольная версия (macOS, Windows): мониторинг активных сессий Oracle и PostgreSQL.

Поднимает тот же веб-интерфейс у себя на 127.0.0.1 (снаружи недоступен) и показывает его в окне программы
(WKWebView на macOS, Edge WebView2 на Windows). Не открылось окно — открывается браузер по умолчанию.

Данные и настройки — в каталоге пользователя:
    macOS:   ~/Library/Application Support/DB ASH Viewer
    Windows: %LOCALAPPDATA%\\DB ASH Viewer
Там же config.json (создаётся при первом запуске) и база SQLite dbash.db: история и сохранённые подключения
(таблица connections). Другое место истории — параметр "db_path" в config.json, отдельная база для списка
подключений — "connections_db" (файл или папка; относительный путь считается от папки, где лежит программа).
Первая база в списке — PostgreSQL из файла pg.txt (строки "Host: …", "Port: …", "Database: …", "User: …",
"Password: …") рядом с программой или в папке данных; без него — встроенная синтетическая демо-база.
Остальные базы Oracle и PostgreSQL добавляются в списке баз (тип выбирается в форме подключения).

Oracle Instant Client вложен в пакет (толстый режим драйвера нужен для Oracle 11g); без него драйвер работает
в простом режиме — с Oracle 12.1 и новее. Вложенный клиент 21 требует Windows 8 и новее; на Windows 7 используется
установленный Oracle Client (папка из "oracle_client_lib" в config.json или найденный по PATH), иначе простой режим.

Автор проекта: Песковский Геннадий Владимирович · pgv0304@gmail.com · 8 961 412 95 95
"""
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "DB ASH Viewer"
LOG = logging.getLogger("dbash.desktop")


def base_dir() -> Path:
    """Folder with the bundled files (PyInstaller unpacks to sys._MEIPASS)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = root / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_CONFIG = {
    "retention_days": 7,
    "session_hours": 720,
    "dynamic_idle_hours": 720,
    "open_server_db": True,
}


def prepare_env(data: Path):
    cfg = data / "config.json"
    if not cfg.exists():
        cfg.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    os.environ.setdefault("ASHV_CONFIG", str(cfg))
    settings = json.loads(cfg.read_text(encoding="utf-8"))
    os.environ.setdefault("ASHV_DB", str(db_file(settings, "db_path", data, "dbash.db")))   # history
    if settings.get("connections_db"):             # saved databases in their own SQLite file
        os.environ.setdefault("ASHV_CONN_DB", str(db_file(settings, "connections_db", data, "connections.db")))
    os.environ.setdefault("ASHV_MODE", "normal")
    os.environ.setdefault("ASHV_LOCAL_STORE", "1")      # saved databases: table connections in dbash.db (SQLite)
    conn_file = next((p for p in (app_location() / "pg.txt", data / "pg.txt") if p.is_file()), None)
    if conn_file:                                   # PostgreSQL connection of pg.txt: first in the list
        os.environ.setdefault("PGASH_CONN_FILE", str(conn_file))
    own = settings.get("oracle_client_lib") or ""
    if own:                                     # Oracle Client, указанный пользователем
        ic = Path(own)
    elif old_windows():                         # вложенный клиент не загрузится — ищем установленный
        ic = installed_oracle_client()
        LOG.info("Windows 7: вложенный Instant Client пропущен, установленный клиент: %s", ic or "не найден")
    else:
        ic = unpack_instant_client(data)
    if ic:
        os.environ.setdefault("ORACLE_THICK", "1")
        os.environ.setdefault("ORACLE_CLIENT_LIB", str(ic))


def db_file(settings: dict, key: str, data: Path, name: str) -> Path:
    """SQLite file from config.json: settings[key] — a file or a folder (then <folder>/<name>), absolute or
    relative to the folder of the program; by default <data folder>/<name>."""
    want = str(settings.get(key) or "").strip()
    if not want:
        return data / name
    p = Path(os.path.expandvars(want)).expanduser()
    if not p.is_absolute():
        p = app_location() / p
    if p.is_dir() or not p.suffix:
        p = p / name
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except OSError as e:
        LOG.warning("%s %s недоступен (%s) — файл в папке данных", key, p, e)
        return data / name


def app_location() -> Path:
    """Folder the program lies in: next to DBASHViewer.exe, or the folder containing «DB ASH Viewer.app»."""
    if not getattr(sys, "frozen", False):     # run from sources: the project folder
        return base_dir()
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent.parent
    return exe.parent


def old_windows() -> bool:
    """Windows 7 / Server 2008 R2 and older (6.1-): no GetOverlappedResultEx & co. in kernel32."""
    if sys.platform != "win32":
        return False
    v = sys.getwindowsversion()
    return (v.major, v.minor) < (6, 2)


def installed_oracle_client():
    """Folder of an installed Oracle Client (oci.dll) from ORACLE_HOME or PATH."""
    dirs = []
    if os.environ.get("ORACLE_HOME"):
        dirs += [Path(os.environ["ORACLE_HOME"]) / "bin", Path(os.environ["ORACLE_HOME"])]
    dirs += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return next((d for d in dirs if (d / "oci.dll").is_file()), None)


def unpack_instant_client(data: Path):
    """Instant Client ships as an archive: code signing of the app would alter loose Oracle libraries
    (they then fail with ORA-01804), so they are unpacked untouched into the user's folder on first start."""
    import tarfile
    import zipfile
    arch = next((p for p in sorted(base_dir().glob("instantclient-*")) if p.suffix in (".gz", ".zip")), None)
    if arch is None:
        return None
    dest = data / ("ic-" + arch.name.split(".")[0] + f"-{arch.stat().st_size}")
    marker = dest / ".ok"
    if not marker.exists():
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        if arch.suffix == ".zip":
            with zipfile.ZipFile(arch) as z:
                z.extractall(dest)
        else:
            with tarfile.open(arch) as t:
                t.extractall(dest, filter="data")
        marker.write_text("ok")
    sub = [p for p in dest.iterdir() if p.is_dir()]
    return sub[0] if len(sub) == 1 and not any(dest.glob("*.dll")) and not any(dest.glob("*.dylib")) else dest


def free_port(data: Path) -> int:
    """The port of the previous start when it is free: the window's storage is tied to the address."""
    saved = data / "port.txt"
    try:
        wanted = int(saved.read_text().strip())
    except (OSError, ValueError):
        wanted = 0
    for p in ([wanted] if wanted else []) + [0]:
        with socket.socket() as s:
            if sys.platform != "win32":     # as uvicorn does: the port of a just closed app may be in TIME_WAIT
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
            port = s.getsockname()[1]
        try:
            saved.write_text(str(port))
        except OSError:
            pass
        return port
    raise RuntimeError("no free port")


def run_server(port: int):
    try:
        import uvicorn
        from app.main import app
        # log_config=None: no console in a windowed build, logging goes to dbash.log via the root logger
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, log_config=None)
    except Exception:  # noqa: BLE001 - make the reason visible in dbash.log
        LOG.exception("server failed")


def wait_ready(url: str, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url + "/api/health", timeout=2).read()
            return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.3)
    return False


def thick_mode_check():
    """Try the bundled Instant Client once; fall back to thin mode (Oracle 12.1+) with a log line."""
    if os.environ.get("ORACLE_THICK") != "1":
        return
    try:
        import oracledb
        oracledb.init_oracle_client(lib_dir=os.environ.get("ORACLE_CLIENT_LIB"))
        from app.ora import collectors
        collectors._ORACLE_INIT_DONE = True
    except Exception as e:  # noqa: BLE001
        LOG.warning("Oracle Instant Client не загружен (%s) — простой режим, Oracle 12.1+", e)
        os.environ["ORACLE_THICK"] = "0"


def webview2_available() -> bool:
    """Edge WebView2 runtime is what pywebview needs on Windows; Windows 7/8.1 usually lack it."""
    if sys.platform != "win32":
        return True
    import winreg
    key = r"SOFTWARE\{}Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}"
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, "WOW6432Node\\"), (winreg.HKEY_LOCAL_MACHINE, ""),
                      (winreg.HKEY_CURRENT_USER, "")):
        try:
            with winreg.OpenKey(hive, key.format(sub)) as k:
                if winreg.QueryValueEx(k, "pv")[0] not in ("", "0.0.0.0"):
                    return True
        except OSError:
            pass
    return False


def open_browser(url: str):
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    if sys.platform == "win32":
        # a DLL that cannot be loaded must raise an error in Python, not show a system dialog box
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)   # SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX
    # a windowed build (PyInstaller --windowed) has no console: sys.stdout/stderr are None,
    # and libraries that write to them (uvicorn's logging) would crash
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    data = data_dir()
    logging.basicConfig(filename=str(data / "dbash.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prepare_env(data)
    sys.path.insert(0, str(base_dir()))
    thick_mode_check()
    port = free_port(data)
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=run_server, args=(port,), daemon=True, name="server").start()
    if not wait_ready(url):
        LOG.error("server did not start")
    LOG.info("DB ASH Viewer is serving %s (thick mode: %s); pg.txt: %s", url, os.environ.get("ORACLE_THICK") == "1",
             os.environ.get("PGASH_CONN_FILE") or "нет")
    LOG.info("database: %s; saved connections: %s", os.environ.get("ASHV_DB"),
             os.environ.get("ASHV_CONN_DB") or os.environ.get("ASHV_DB"))
    if os.environ.get("ASHV_HEADLESS") == "1":     # для проверки сборки: только сервер, без окна
        while True:
            time.sleep(3600)
    if not webview2_available():
        LOG.warning("Edge WebView2 не установлен — открываю браузер")
        open_browser(url)
        return
    try:
        import webview
        webview.create_window(APP_NAME, url, width=1400, height=900, min_size=(900, 600))
        webview.start(private_mode=False, storage_path=str(data / "webview"))
    except Exception as e:  # noqa: BLE001 - no WebView: use the default browser and keep serving
        LOG.warning("WebView недоступен (%s) — открываю браузер", e)
        open_browser(url)


if __name__ == "__main__":
    main()
