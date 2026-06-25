@echo off
REM ============================================================
REM  Cloud Agent Monitoring - start script (double-click me)
REM  - installs dependencies once (first run)
REM  - opens the dashboard in your browser
REM  - starts the server (Ctrl+C to stop)
REM ============================================================
setlocal
cd /d "%~dp0"
title Cloud Agent Monitoring

REM --- Check Python is available ---
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python was not found on your PATH.
  echo         Install Python 3 from https://www.python.org/downloads/ and retry.
  pause
  exit /b 1
)

REM --- Install dependencies once (creates a marker so later runs are instant) ---
if not exist ".deps_installed" (
  echo [setup] Installing dependencies ^(first run only^)...
  python -m pip install -r Backend\requirements.txt
  if errorlevel 1 ( echo [ERROR] Dependency install failed. & pause & exit /b 1 )
  echo done > ".deps_installed"
)

REM --- Ensure a .env exists ---
if not exist ".env" (
  echo [setup] No .env found - creating one from .env.example.
  echo         Edit .env to add your AccessKey and set MOCK_MODE=false for live data.
  copy /y ".env.example" ".env" >nul
)

REM --- Open the dashboard a few seconds after the server starts ---
start "" cmd /c "timeout /t 4 >nul & start "" http://127.0.0.1:5000"

echo.
echo [run] Starting Cloud Agent Monitoring at http://127.0.0.1:5000
echo [run] Leave this window open. Press Ctrl+C to stop the server.
echo.
python Backend\app.py

echo.
echo [stopped] Server has stopped.
pause
endlocal
