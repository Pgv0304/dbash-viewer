#!/bin/bash
# DB ASH Viewer — запуск из исходников на macOS/Linux: двойной щелчок или ./start.command
# Первый запуск создаёт окружение .venv; подключение — из pg.txt рядом с этим файлом.
cd "$(dirname "$0")"
PY=$(command -v python3.13 || command -v python3)
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt pywebview==5.4
exec .venv/bin/python desktop/desktop.py
