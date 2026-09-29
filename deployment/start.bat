@echo off
REM ============================================================
REM  Data Intelligence Platform — start.bat
REM  Resolves everything from %~dp0 — never depends on the
REM  caller's current working directory, never hardcodes a
REM  developer-specific path.
REM
REM  One process serves the API, the built frontend, the embedded
REM  scheduler, and the embedded async worker pools — see
REM  README_DEPLOYMENT.md's "Architecture" section for why this
REM  app doesn't run separate Backend/Worker/Scheduler processes.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PIDFILE=%~dp0run\backend.pid"
set "PORT=8080"
if exist "%~dp0backend\.env" (
    for /f "tokens=1,2 delims==" %%a in ('findstr /b "PORT=" "%~dp0backend\.env"') do set "PORT=%%b"
)
set "WORKERCOUNT=1"
if exist "%~dp0backend\.env" (
    for /f "tokens=1,2 delims==" %%a in ('findstr /b "WORKER_COUNT=" "%~dp0backend\.env"') do set "WORKERCOUNT=%%b"
)

REM ── Already running? ──────────────────────────────────────────
if exist "%PIDFILE%" (
    set /p "OLDPID=" < "%PIDFILE%"
    tasklist /FI "PID eq !OLDPID!" 2>nul | findstr /i "!OLDPID!" >nul
    if !ERRORLEVEL! EQU 0 (
        echo Backend is already running ^(PID !OLDPID!^). Use stop.bat first if you want to restart it.
        goto :end
    ) else (
        del /q "%PIDFILE%" >nul 2>nul
    )
)

if not exist "%~dp0.venv\Scripts\uvicorn.exe" (
    echo ERROR: .venv not found or incomplete. Run install.bat first.
    goto :fail
)

if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0run" mkdir "%~dp0run"

echo Starting Data Intelligence Platform backend on port %PORT% (workers=%WORKERCOUNT%)...

REM Launch detached, capture its PID via PowerShell (reliable — batch's
REM own `start` doesn't hand back the child PID directly), redirect
REM stdout/stderr as a safety net even though app.main's own
REM RotatingFileHandler already writes logs\backend.log.
for /f "delims=" %%p in ('powershell -Command ^
  "$p = Start-Process -FilePath '%~dp0.venv\Scripts\uvicorn.exe' -ArgumentList 'app.main:app --host 0.0.0.0 --port %PORT% --workers %WORKERCOUNT%' -WorkingDirectory '%~dp0backend' -WindowStyle Hidden -RedirectStandardOutput '%~dp0logs\uvicorn.out.log' -RedirectStandardError '%~dp0logs\uvicorn.err.log' -PassThru; $p.Id"') do set "NEWPID=%%p"

if "!NEWPID!"=="" (
    echo ERROR: failed to start the backend process.
    goto :fail
)
echo !NEWPID! > "%PIDFILE%"
echo Backend started (PID !NEWPID!).
echo   Application: http://localhost:%PORT%
echo   Health:      http://localhost:%PORT%/api/health
echo   Logs:        logs\backend.log ^(+ logs\scheduler.log / logs\worker.log^)

:end
if /i not "%~1"=="nowait" pause
exit /b 0

:fail
if /i not "%~1"=="nowait" pause
exit /b 1
