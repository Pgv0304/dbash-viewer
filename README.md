# DB ASH Viewer — мониторинг производительности Oracle и PostgreSQL

Одно приложение для двух СУБД (объединяет ASH Viewer Web для Oracle и PG ASH Viewer для PostgreSQL). Раз в секунду
снимает активные сессии и показывает, чем заняты базы: график активных сессий по классам ожиданий, топ SQL и сессий,
блокировки, текст и план запроса, отчёт о производительности с рекомендациями, нагрузочное тестирование.
Несколько баз Oracle и PostgreSQL наблюдаются одновременно, между ними можно переключаться.

| Где работает | Адрес |
|---|---|
| Сервер 84.54.28.45 | https://84-54-28-45.sslip.io:96/ |
| Демо-витрина (PythonAnywhere, синтетические базы) | https://pgv0304.pythonanywhere.com/ |
| Docker на Mac | http://127.0.0.1:8096/ |

## Возможности

- **График активных сессий** за 5 минут — 24 часа или произвольный период; группировка по классу ожидания, событию,
  SQL, сессии, пользователю, модулю, программе, машине, операции; детализация щелчком по графику.
- **Топ SQL и сессий**, вкладки «События», «Пользователи», «Модули», «Программы», «Машины», «Операции», «Планы»,
  **«Блокировки»** — кто держит и кто ждёт.
- **Детали запроса:** текст, план, статистика выполнений, активность запроса по времени.
- **Отчёт о производительности** по двум снимкам статистики (как Statspack/AWR) с рекомендациями по настройке;
  для Oracle с Diagnostics Pack — родной отчёт AWR. Снимки делаются автоматически и кнопкой.
- **Нагрузочное тестирование** — генератор случайной нагрузки для учебных и тестовых баз.
- **Журнал входов** за 3 месяца с выгрузкой в CSV; **схема оформления** — тёмная, светлая или как в системе.

| | Oracle | PostgreSQL |
|---|---|---|
| Источник сессий | V$SESSION или V$ACTIVE_SESSION_HISTORY (Diagnostics Pack) | pg_stat_activity |
| Классы ожиданий | CPU, User I/O, Application, Commit, Concurrency… | CPU, IO, Lock, LWLock, IPC, Client… |
| Запрос | SQL ID, текст из V$SQLAREA, план DBMS_XPLAN | query_id или отпечаток нормализованного текста; EXPLAIN |
| Отчёт | профиль, события, модель времени, SQL, сегменты, I/O, память и советники | профиль, эффективность, ожидания, SQL (pg_stat_statements), таблицы и индексы, базы |
| Рекомендации | память, redo, блокировки, разбор, CPU, I/O, undo, статистика, параметры | shared_buffers, work_mem, контрольные точки, автоочистка, XID, индексы… |
| Нагрузка | таблицы LG_DATA, LG_LOG, LG_HOT в схеме пользователя (≈ 20 МБ) | схема pgash_load (60–80 МБ) |

Права пользователя мониторинга: Oracle — CREATE SESSION и SELECT_CATALOG_ROLE; PostgreSQL — роль `pg_monitor`
или суперпользователь. Нагрузке нужны права на создание таблиц (Oracle) или CREATE в базе (PostgreSQL).
Не включайте нагрузку на рабочей базе.

## Базы в списке

- **PostgreSQL из `pg.txt`** — первая в списке, открывается без пароля. Файл ищется по пути из `PGASH_CONN_FILE`,
  рядом с программой или в папке данных:
  ```
  Host: localhost
  Port: 5432
  Database: postgres
  User: postgres
  Password: ваш_пароль
  ```
- **Базы сервера** из `config/targets.json` (веб-версия), открываются без пароля при `"open_server_db": true`.
- **Сохранённые подключения** — «＋ Добавить базу»: Oracle (хост, порт 1521, сервис, режим SYSDBA, источник
  V$SESSION или ASH) или PostgreSQL (хост, порт 5432, база данных). В веб-версии хранятся в браузере,
  в настольной — в базе программы.
- **Синтетические демо-базы** (`"type": "demo"` и `"demo_pg"`) — имитация нагрузки без подключения к СУБД.
  Если нет ни `pg.txt`, ни `targets.json`, приложение стартует с демо-базой Oracle. У демо-баз работают снимки и отчёт
  (`app/demo_snap.py`) и кнопка «Нагрузка» — фазы нагрузочного теста появляются на графике (`app/demo_load.py`).

## Запуск

| Способ | Как |
|---|---|
| Программа для macOS | `dist/DB-ASH-Viewer-macOS.dmg` → «Программы»; первый запуск — правый щелчок → «Открыть» |
| Программа для Windows | `DBASHViewer.exe` из GitHub Actions (`.github/workflows/windows.yml`); Windows 7 и новее. Руководство — `docs/` |
| Из исходников | `./start.command` (macOS/Linux) или `start.bat` (Windows) |
| Веб-сервер | `pip install -r requirements.txt` и `uvicorn app.main:app --port 8080` |
| Docker | `docker build --build-arg INSTANT_CLIENT=1 -t dbash .` и `docker run -p 8080:8080 -e ORACLE_THICK=1 -v $PWD/pg.txt:/config/pg.txt:ro -v dbash:/data dbash` |
| Docker на Mac | `cd docker-mac && docker compose up -d --build` → http://127.0.0.1:8096/ (порт — `DBASH_PORT`) |
| Демо на PythonAnywhere | архив и шаги — `pythonanywhere/README.md` |

**Oracle 11g** работает только через Oracle Instant Client («толстый» режим драйвера): клиент вложен в программы
для macOS и Windows и в образ Docker (`INSTANT_CLIENT=1`, `ORACLE_THICK=1`). Без клиента доступны Oracle 12.1 и новее.
**PostgreSQL** работает через psycopg 3 со встроенной libpq — клиент не нужен.

### Настольная версия

Окно программы — pywebview (WKWebView на macOS, Edge WebView2 на Windows); без WebView2 открывается браузер.
Сервер слушает только 127.0.0.1. Данные — `~/Library/Application Support/DB ASH Viewer`
(Windows: `%LOCALAPPDATA%\DB ASH Viewer`): `dbash.db` (история и список баз), `config.json`, `dbash.log`.
В `config.json`: `db_path` — другое место истории, `connections_db` — отдельная база для списка подключений,
`oracle_client_lib` — папка установленного Oracle Client (Windows 7). Сборка для macOS — `desktop/build_macos.sh`.

### Docker на Mac

`docker-mac/compose.yaml`: контейнер `dbash-mac` с Oracle Instant Client, `pg.txt` из корня проекта, история в томе
`dbash-data`. Адреса `localhost` и `127.0.0.1` внутри контейнера подменяются на `host.docker.internal` (сам Mac,
`docker-mac/config/targets.json`). Журнал входов открывается ссылкой `http://127.0.0.1:8096/#admin=<ключ>`,
ключ — `ASHV_ADMIN_TOKEN` в `docker-mac/.env` (не попадает в git).

### Сервер 84.54.28.45

`deploy-84/`: `compose.yaml` (контейнер `dbash` на 127.0.0.1:8095), `nginx-dbash-96` (HTTPS на порту 96),
`targets.json` (базы сервера, пароли — в `.env` через `password_env`). Каталог на сервере — `/opt/dbash`.

## Настройки

`config/targets.json` (путь — `ASHV_CONFIG`):

| Ключ | Назначение | По умолчанию |
|---|---|---|
| `targets` | базы: `name`, `type` (`oracle`, `postgres`, `demo`, `demo_pg`), `dsn` (`хост:порт/сервис`), `user`, `password_env`, `source` (`vsession`/`ash`), `interval`, `display`, `snapshot_minutes` | — |
| `server_targets`, `demo_target` | какие базы показать первыми и какая открывается по умолчанию | демо-база |
| `open_server_db` | базы сервера открываются без пароля | `true` |
| `demo_load`, `load_target` | генератор нагрузки базы сервера (кнопка «Нагрузка»): `type`, `dsn`, `user`, `password_env`, `max_sessions`, `autostart` | — |
| `host_aliases` | подмена адресов из формы и `pg.txt` (например, внешний IP → `host.docker.internal`) | — |
| `retention_days` | хранение истории и снимков, дней | 3 |
| `login_log_days` | хранение журнала входов, дней | 92 |
| `session_hours` | срок сессии браузера, часов | 12 |
| `dynamic_idle_hours` | остановка неиспользуемых баз, добавленных из формы, часов | 12 |
| `max_login_failures` | неудачных входов с одного IP за 10 минут до блокировки | 10 |

Переменные окружения:

| Переменная | Назначение |
|---|---|
| `ASHV_CONFIG`, `ASHV_DB` | файл настроек и файл истории (SQLite) |
| `ASHV_MODE` | `normal` — вход по паролю; `demo` — посетитель сразу видит базы сервера |
| `ASHV_ADMIN_TOKEN` | ключ журнала входов на сервере (заголовок `X-Admin-Token` или ссылка `#admin=`) |
| `PGASH_CONN_FILE` | путь к `pg.txt` |
| `ORACLE_THICK`, `ORACLE_CLIENT_LIB` | толстый режим драйвера Oracle и папка клиента |
| `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_SOURCE` | одна база Oracle без `targets.json` |
| `PG_DSN`, `PG_USER`, `PG_PASSWORD` | одна база PostgreSQL без `targets.json` |
| `LOG_LEVEL` | уровень журнала (`INFO`) |

## Безопасность

Вход в базу — пробным подключением; сессия — cookie `HttpOnly`, `SameSite=Lax`, `Secure` по HTTPS. Каждый запрос
проверяет, что база открыта в этой сессии. 10 неудачных входов с одного IP за 10 минут блокируют вход на 10 минут.
Опечатки в пароле учётной записи, которой уже ведётся мониторинг, проверяются локально и не увеличивают счётчик
неудачных входов Oracle. Журнал входов не хранит паролей; на сервере он доступен только по ключу администратора.

## Устройство

| Каталог / файл | Назначение |
|---|---|
| `app/main.py` | веб-API (FastAPI): вход и сессии, список баз, активность, топы, блокировки, SQL, снимки и отчёты, нагрузка, журнал входов |
| `app/dbtypes.py` | выбор модуля по типу базы |
| `app/ora/`, `app/pg/` | сборщик, отчёт (`awr.py`), советник (`awr_advisor.py`), генератор нагрузки (`loadgen.py`) для Oracle и PostgreSQL |
| `app/demo_snap.py`, `app/demo_load.py` | снимки статистики и имитация нагрузки для синтетических баз |
| `app/store.py` | история, снимки, журнал входов и подключения в SQLite |
| `app/static/` | интерфейс (ECharts), отчёт (`awr.js`), заставка (`intro.js`) |
| `desktop/` | настольная оболочка (pywebview) и сборка |
| `docker-mac/`, `deploy-84/`, `pythonanywhere/` | запуск в Docker на Mac, на сервере 84.54.28.45 и на PythonAnywhere |
| `docs/` | руководство пользователя для Windows (DOCX, PDF) |

## Автор проекта

Песковский Геннадий Владимирович
Почта: pgv0304@gmail.com
Телефон: 8 961 412 95 95
