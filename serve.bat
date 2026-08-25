@echo off
REM serve.bat - start the Conftech web UI with the project's venv.
REM IMPORTANT: run with .venv\Scripts\python.exe (NOT `py`) so the
REM `ddgs` package actually loads. Using `py` resolves to the Anaconda
REM Python install on this machine, which has no ddgs, and the server
REM silently falls back to Serper-only - which immediately hits the
REM "Query pattern not allowed for free accounts" 400.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [serve] ERROR: .venv\Scripts\python.exe not found.
    echo [serve] Create the venv with:  py -3.11 -m venv .venv
    echo [serve] Then install deps:     .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

echo [serve] Starting Conftech web UI on http://localhost:8000
".venv\Scripts\python.exe" -m uvicorn webapp:build_app --factory --host 0.0.0.0 --port 8000 %*
endlocal
