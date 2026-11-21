@echo off
rem Starts the server on http://localhost:9002
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    ".venv\Scripts\python.exe" -m playwright install chromium
)

".venv\Scripts\python.exe" server.py
