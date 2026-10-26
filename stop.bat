@echo off
rem Stop the backend started by start.bat.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :missing_env
".venv\Scripts\python.exe" -m music_refill stop
if errorlevel 1 goto :stop_failed
exit /b 0

:missing_env
echo [error] .venv not found; cannot determine service state.
pause
exit /b 1

:stop_failed
echo.
echo If the service is still running, end the pythonw.exe process in Task Manager.
pause
exit /b 1
