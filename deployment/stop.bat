@echo off
REM ============================================================
REM  Data Intelligence Platform — stop.bat
REM  Targets the exact PID start.bat recorded — never a generic
REM  window-title or process-name match that could kill an
REM  unrelated process.
REM
REM  Graceful shutdown order (spec section 68): this process embeds
REM  the scheduler and worker pools (see README_DEPLOYMENT.md), so
REM  a single graceful SIGTERM-equivalent (uvicorn's own shutdown
REM  handler, invoked by closing main window / Stop-Process without
REM  -Force first) already runs FastAPI's lifespan shutdown, which
REM  calls scheduler.shutdown(wait=False) — stopping new scheduler
REM  ticks — before the process exits. We try a graceful stop first
REM  and only escalate to a forceful kill if it doesn't exit in time.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PIDFILE=%~dp0run\backend.pid"

if not exist "%PIDFILE%" (
    echo No run\backend.pid found — nothing appears to be running ^(or it was started outside start.bat^).
    goto :end
)

set /p "PID=" < "%PIDFILE%"
tasklist /FI "PID eq %PID%" 2>nul | findstr /i "%PID%" >nul
if !ERRORLEVEL! NEQ 0 (
    echo Recorded PID %PID% is not running. Removing stale run\backend.pid.
    del /q "%PIDFILE%" >nul 2>nul
    goto :end
)

echo Stopping backend (PID %PID%)...
powershell -Command "Stop-Process -Id %PID% -ErrorAction SilentlyContinue"

set "STOPPED=0"
for /l %%i in (1,1,10) do (
    tasklist /FI "PID eq %PID%" 2>nul | findstr /i "%PID%" >nul
    if !ERRORLEVEL! NEQ 0 (
        set "STOPPED=1"
        goto :stopped
    )
    timeout /t 1 /nobreak >nul
)
:stopped

if "!STOPPED!"=="0" (
    echo Process did not exit gracefully within 10 seconds — forcing termination.
    taskkill /PID %PID% /F >nul 2>nul
)

del /q "%PIDFILE%" >nul 2>nul
echo Backend stopped.

:end
pause
exit /b 0
