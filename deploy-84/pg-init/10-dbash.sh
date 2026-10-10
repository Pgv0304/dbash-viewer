#!/bin/sh
# Первый запуск PostgreSQL: пользователь мониторинга dbash (pg_monitor) и пользователь нагрузки pgload.
set -e
psql -v ON_ERROR_STOP=1 -U postgres -d demo \
     -v pg_password="$PG_PASSWORD" -v pgload_password="$PGLOAD_PASSWORD" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE ROLE dbash LOGIN PASSWORD :'pg_password';
GRANT pg_monitor TO dbash;
CREATE ROLE pgload LOGIN PASSWORD :'pgload_password';
GRANT CREATE ON DATABASE demo TO pgload;          -- генератор создаёт схему pgash_load
-- мониторинг видит таблицы нагрузки (планы EXPLAIN)
ALTER DEFAULT PRIVILEGES FOR ROLE pgload GRANT USAGE ON SCHEMAS TO dbash;
ALTER DEFAULT PRIVILEGES FOR ROLE pgload GRANT SELECT ON TABLES TO dbash;
SQL
