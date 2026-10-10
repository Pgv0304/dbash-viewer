@echo off
rem DB ASH Viewer - zapusk iz ishodnikov na Windows; podklyuchenie - iz pg.txt ryadom s etim faylom
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python -m pip install -q -r requirements.txt pywebview==5.4
.venv\Scripts\python desktop\desktop.py
