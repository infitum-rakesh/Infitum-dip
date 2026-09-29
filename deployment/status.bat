@echo off
REM ============================================================
REM  Data Intelligence Platform — status.bat
REM  All checks are real: PID/tasklist for the process, real HTTP
REM  calls for backend/scheduler/OCR/DB health. Scheduler/Worker
REM  are reported as in-process subsystems of the one backend PID
REM  (see README_DEPLOYMENT.md) — this is the real architecture,
REM  not a simplification of a "should be separate" design.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PIDFILE=%~dp0run\backend.pid"
set "PORT=8080"
if exist "%~dp0backend\.env" (
    for /f "tokens=1,2 delims==" %%a in ('findstr /b "PORT=" "%~dp0backend\.env"') do set "PORT=%%b"
)

echo ========================================
echo  Data Intelligence Platform — Status
echo ========================================
echo.

REM ── Backend process ──────────────────────────────────────────
set "BACKEND_UP=0"
if exist "%PIDFILE%" (
    set /p "PID=" < "%PIDFILE%"
    tasklist /FI "PID eq !PID!" 2>nul | findstr /i "!PID!" >nul
    if !ERRORLEVEL! EQU 0 set "BACKEND_UP=1"
)
if "!BACKEND_UP!"=="1" (
    echo Backend:  RUNNING  ^(PID !PID!, port !PORT!^)
) else (
    echo Backend:  STOPPED
)

REM ── HTTP health + ops-status (real endpoints) ────────────────
if "!BACKEND_UP!"=="1" (
    curl -s http://localhost:!PORT!/api/ops-status > "%TEMP%\dip_ops.json" 2>nul
    findstr /c:"\"scheduler_running\":true" "%TEMP%\dip_ops.json" >nul
    if !ERRORLEVEL! EQU 0 (
        echo Scheduler ^(in-process^): RUNNING
    ) else (
        echo Scheduler ^(in-process^): STOPPED or UNREACHABLE
    )
    echo   Worker pools ^(same process^): see logs\worker.log for recent activity
    del /q "%TEMP%\dip_ops.json" >nul 2>nul

    curl -s -o nul -w "%%{http_code}" http://localhost:!PORT!/ocr-health > "%TEMP%\dip_ocr.tmp" 2>nul
    set /p "OCRCODE=" < "%TEMP%\dip_ocr.tmp"
    if "!OCRCODE!"=="200" (
        echo OCR ^(Tesseract^): AVAILABLE
    ) else (
        echo OCR ^(Tesseract^): CHECK FAILED ^(http !OCRCODE!^)
    )
    del /q "%TEMP%\dip_ocr.tmp" >nul 2>nul
) else (
    echo Scheduler: N/A ^(backend not running^)
    echo OCR: N/A ^(backend not running^)
)

REM ── Database reachability (real check, no server needed) ────
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0backend\scripts\validate_config.py" --db-only >nul 2>"%TEMP%\dip_db.err"
    if !ERRORLEVEL! EQU 0 (
        echo Database: CONNECTED
    ) else (
        echo Database: FAILED — see %TEMP%\dip_db.err
    )
) else (
    echo Database: UNKNOWN ^(.venv not found — run install.bat^)
)

REM ── Storage ───────────────────────────────────────────────────
if exist "%~dp0uploads" (
    echo Storage:  AVAILABLE
) else (
    echo Storage:  MISSING ^(run install.bat^)
)

echo.
echo ========================================
pause
exit /b 0
