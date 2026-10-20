#!/bin/bash
# DB ASH Viewer — демо-витрина на PythonAnywhere (ASGI-сайт, бета).
# Запускается командой сайта:  pa website create --domain ЛОГИН.pythonanywhere.com --command '/bin/bash /home/ЛОГИН/dbash-demo/run.sh' (ЛОГИН — как в пути домашней папки, с учётом регистра)
# PythonAnywhere передаёт путь к сокету в DOMAIN_SOCKET.
set -e
cd "$(dirname "$0")"
# окружение — рядом с папкой приложения (~/.virtualenvs/dbash-demo); $HOME у команды сайта может быть пустым
VENV="$(cd .. && pwd)/.virtualenvs/dbash-demo"
mkdir -p data
echo "$(date -u '+%F %T') старт: venv=$VENV socket=${DOMAIN_SOCKET:-не задан}" >> data/run.log
export ASHV_MODE=demo                          # посетитель сразу видит демо-базы, без пароля
export ASHV_CONFIG="$PWD/config/targets.json"  # две синтетические базы: Oracle и PostgreSQL
export ASHV_DB="$PWD/data/dbash.db"            # история; хранится 6 часов (retention_days: 0.25) — на бесплатном тарифе 512 МБ диска
export PGASH_CONN_FILE="$PWD/no-pg.txt"        # pg.txt не используется
export LOG_LEVEL=WARNING
exec "$VENV/bin/python" -m uvicorn --app-dir "$PWD" --uds "$DOMAIN_SOCKET" \
     --proxy-headers --forwarded-allow-ips='*' app.main:app >> data/run.log 2>&1
