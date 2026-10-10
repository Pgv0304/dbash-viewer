#!/bin/bash
# Однократная настройка PostgreSQL в oracle11 (выполняется на хосте 84.54.28.45 от root).
# Пароли берутся из /opt/dbash/.env (PG_SUPERUSER_PASSWORD, PG_PASSWORD, PGLOAD_PASSWORD) и никуда не выводятся.
set -euo pipefail
set -a; . /opt/dbash/.env; set +a
PGDATA=/u01/app/oracle/pgdata
docker exec -u oracle -e PGSU="$PG_SUPERUSER_PASSWORD" oracle11 bash -c '
  set -e
  PGDATA='"$PGDATA"'
  if [ ! -f "$PGDATA/PG_VERSION" ]; then
    printf "%s" "$PGSU" > /tmp/.pgsu && chmod 600 /tmp/.pgsu
    /usr/bin/initdb -D "$PGDATA" -U postgres --pwfile=/tmp/.pgsu --auth-local=trust --auth-host=scram-sha-256 \
      -E UTF8 --locale=C >/dev/null
    rm -f /tmp/.pgsu
    cat >> "$PGDATA/postgresql.conf" <<CONF

# --- PostgreSQL в контейнере oracle11 (DB ASH Viewer, 2026-10-10): слабый сервер, мало диска
listen_addresses = '"'"'*'"'"'
port = 5432
unix_socket_directories = '"'"'/tmp'"'"'
max_connections = 40
shared_buffers = 64MB
work_mem = 4MB
max_wal_size = 64MB
min_wal_size = 32MB
shared_preload_libraries = '"'"'pg_stat_statements'"'"'
pg_stat_statements.track = all
track_io_timing = on
logging_collector = on
log_directory = '"'"'log'"'"'
log_rotation_size = 10MB
log_truncate_on_rotation = on
log_filename = '"'"'postgresql-%a.log'"'"'
CONF
    printf "\n# Приложения из сетей Docker (DB ASH Viewer); порт 5432 на хост не опубликован\nhost all all 172.16.0.0/12 scram-sha-256\n" >> "$PGDATA/pg_hba.conf"
  fi
  /usr/bin/pg_ctl -D "$PGDATA" status >/dev/null 2>&1 || /usr/bin/pg_ctl -D "$PGDATA" -l "$PGDATA/startup.log" -w -t 60 start >/dev/null
'
docker cp /opt/dbash/oracle11-pg/50-postgres.sh oracle11:/container-entrypoint-startdb.d/50-postgres.sh
docker exec -u 0 oracle11 chmod 755 /container-entrypoint-startdb.d/50-postgres.sh
# база demo, пользователи мониторинга и нагрузки (как в отдельном контейнере dbash-pg)
docker exec -i -u oracle oracle11 psql -h /tmp -U postgres -d postgres -v ON_ERROR_STOP=1 -q \
  -v pg_password="$PG_PASSWORD" -v pgload_password="$PGLOAD_PASSWORD" <<'SQL'
SELECT 'CREATE DATABASE demo' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'demo')\gexec
\c demo
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dbash') THEN CREATE ROLE dbash LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgload') THEN CREATE ROLE pgload LOGIN; END IF;
END $$;
ALTER ROLE dbash PASSWORD :'pg_password';
ALTER ROLE pgload PASSWORD :'pgload_password';
GRANT pg_monitor TO dbash;
GRANT CREATE ON DATABASE demo TO pgload;
ALTER DEFAULT PRIVILEGES FOR ROLE pgload GRANT USAGE ON SCHEMAS TO dbash;
ALTER DEFAULT PRIVILEGES FOR ROLE pgload GRANT SELECT ON TABLES TO dbash;
SQL
echo "PostgreSQL в oracle11 готов"
