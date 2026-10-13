# DB ASH Viewer — мониторинг производительности Oracle и PostgreSQL

Одно приложение для двух СУБД: объединяет ASH Viewer Web (Oracle) и PG ASH Viewer (PostgreSQL).
Раз в секунду снимает активные сессии и показывает, чем заняты базы: график активных сессий по классам ожиданий,
топ SQL и сессий, блокировки, текст и план запроса, отчёт о производительности с рекомендациями,
нагрузочное тестирование. Базы Oracle и PostgreSQL можно наблюдать одновременно и переключаться между ними.

## Базы данных

На экране входа — список баз:

- **PostgreSQL из `pg.txt`** — первая в списке, открывается без ввода пароля. Формат файла:
  ```
  Host: localhost
  Port: 5432
  Database: postgres
  User: postgres
  Password: ваш_пароль
  ```
  Файл ищется рядом с программой, в папке данных или по пути из `PGASH_CONN_FILE`. Без него — синтетическая демо-база.
- **Базы сервера из `config/targets.json`** (веб-версия): Oracle и PostgreSQL (`"type": "oracle"` / `"postgres"`).
- **Сохранённые подключения** — «＋ Добавить базу», в форме выбирается **тип базы**:
  - Oracle — хост, порт (1521), сервис, режим SYSDBA, источник V$SESSION или ASH (Diagnostics Pack);
  - PostgreSQL — хост, порт (5432), база данных.

Права: Oracle — CREATE SESSION и SELECT_CATALOG_ROLE; PostgreSQL — роль `pg_monitor` или суперпользователь.

## Запуск

| Способ | Как |
|---|---|
| Программа для macOS | `dist/DB-ASH-Viewer-macOS.dmg` → «Программы»; `pg.txt` — рядом с программой или в `~/Library/Application Support/DB ASH Viewer`. Первый запуск: правый щелчок → «Открыть» |
| Из исходников | `./start.command` (macOS/Linux) или `start.bat` (Windows) |
| Веб-сервер | `uvicorn app.main:app --port 8080` |
| Docker | `docker build --build-arg INSTANT_CLIENT=1 -t dbash .` и `docker run -p 8080:8080 -e ORACLE_THICK=1 -v $PWD/pg.txt:/config/pg.txt:ro -v dbash:/data dbash` |
| Docker на Mac | `cd docker-mac && docker compose up -d --build` → http://127.0.0.1:8080/ (см. `docker-mac/compose.yaml`) |

Oracle 11g требует Oracle Instant Client («толстый» режим драйвера): он вложен в программу для macOS и в образ
Docker (`INSTANT_CLIENT=1`). При запуске из исходников без клиента доступны Oracle 12.1 и новее.
PostgreSQL работает через psycopg 3 со встроенной libpq — клиент не нужен.

Данные настольной версии: `~/Library/Application Support/DB ASH Viewer` (Windows: `%LOCALAPPDATA%\DB ASH Viewer`):
`dbash.db` — история и список баз (SQLite), `config.json`, `dbash.log`. В `config.json`: `"db_path"` — другое место
истории, `"connections_db"` — отдельная база SQLite для списка подключений.

Сборка для macOS: `desktop/build_macos.sh` → `dist/DB ASH Viewer.app`, `dist/DB-ASH-Viewer-macOS.dmg`.

## Возможности по СУБД

| | Oracle | PostgreSQL |
|---|---|---|
| Источник сессий | V$SESSION (или V$ACTIVE_SESSION_HISTORY) | pg_stat_activity |
| Классы ожиданий | CPU, User I/O, Application, Commit, Concurrency… | CPU, IO, Lock, LWLock, IPC, Client… |
| Запрос | SQL ID, текст из V$SQLAREA, план DBMS_XPLAN | query_id или отпечаток нормализованного текста; EXPLAIN, общий план для запросов с $1… |
| Отчёт | снимки V$-статистики (как Statspack): профиль, события, модель времени, SQL, сегменты, I/O, память и советники; при Diagnostics Pack — AWR Oracle | снимки pg_stat_*: профиль, эффективность, ожидания по выборкам, SQL (pg_stat_statements), таблицы и индексы, базы, сервер |
| Рекомендации | память, redo, блокировки, разбор, CPU, I/O, undo, статистика, параметры | shared_buffers, work_mem, контрольные точки, автоочистка, XID, блокировки, индексы, pg_stat_statements… |
| Нагрузка | схема пользователя: LG_DATA, LG_LOG, LG_HOT | схема pgash_load |

Подписи интерфейса (SQL ID / Запрос, Модуль / База данных…), цвета ожиданий и отчёт меняются по типу выбранной базы.

## Устройство

| Каталог | Назначение |
|---|---|
| `app/main.py` | общее веб-API: сессии, список баз, вход (тип базы), нагрузка, отчёты, `pg.txt` |
| `app/dbtypes.py` | выбор модуля по типу базы |
| `app/ora/` | Oracle: сборщик, отчёт (`awr.py`), советник, генератор нагрузки |
| `app/pg/` | PostgreSQL: сборщик, отчёт, советник, генератор нагрузки |
| `app/store.py` | история в SQLite |
| `app/static/` | интерфейс (ECharts), отчёты обеих СУБД (`awr.js`), заставка |
| `desktop/` | настольная оболочка (pywebview) и сборка |

## Автор проекта

Песковский Геннадий Владимирович
Почта: pgv0304@gmail.com
Телефон: 8 961 412 95 95
