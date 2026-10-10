#!/bin/bash
# PostgreSQL 16 внутри контейнера oracle11 (gvenzl/oracle-xe:11): запуск при каждом старте контейнера.
# Лежит в /container-entrypoint-startdb.d/; данные — /u01/app/oracle/pgdata (диск /srv/oracle11 на хосте).
PGDATA=/u01/app/oracle/pgdata
if [ -f "$PGDATA/PG_VERSION" ] && ! /usr/bin/pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  /usr/bin/pg_ctl -D "$PGDATA" -l "$PGDATA/startup.log" -w -t 60 start
fi
