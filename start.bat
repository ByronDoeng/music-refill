@echo off
rem One-click start: launch the backend silently, wait for health check,
rem then open the web UI. Use stop.bat to stop the service.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :init_env
goto :launch

:init_env
echo [init] .venv not found; creating virtualenv and installing dependencies...
python -m venv .venv
if errorlevel 1 goto :init_failed
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
if errorlevel 1 goto :init_failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto :init_failed

:launch
".venv\Scripts\python.exe" -m music_refill launch
if errorlevel 1 goto :launch_failed
exit /b 0

:init_failed
echo.
echo [error] Environment setup failed. Install Python 3.11+ and add it to PATH.
pause
exit /b 1

:launch_failed
echo.
echo [error] Launch failed. See data\logs\serve.log and data\logs\music_refill.log.
pause
exit /b 1
