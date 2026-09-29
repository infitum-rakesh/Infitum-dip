@echo off
REM ============================================================
REM  Data Intelligence Platform — upgrade.bat
REM  Never overwrites backend\.env or any production data.
REM  Expects the new package's backend\app, backend\scripts, and
REM  frontend\dist to already be copied over this installation's
REM  equivalents (NEW_APP_DIR / NEW_SCRIPTS_DIR / NEW_DIST_DIR
REM  below), leaving .env/dip.db/uploads/logs untouched — this
REM  matches how the deployment package is redistributed (see
REM  README_DEPLOYMENT.md "Upgrade").
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ========================================
echo  Data Intelligence Platform — Upgrade
echo ========================================
echo.

if "%~1"=="" (
    echo Usage: upgrade.bat ^<path-to-extracted-new-package^>
    echo   e.g. upgrade.bat C:\Downloads\DataIntelligencePlatform-2.1
    goto :fail
)
set "NEWPKG=%~1"
if not exist "%NEWPKG%\backend\app" (
    echo ERROR: %NEWPKG%\backend\app not found — is this a valid package folder?
    goto :fail
)

REM ── Step 1: backup configuration ─────────────────────────────
echo [1/8] Backing up configuration...
if not exist "%~dp0backup" mkdir "%~dp0backup"
for /f "delims=" %%t in ('powershell -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"
copy /y "%~dp0backend\.env" "%~dp0backup\.env.%TS%.bak" >nul
echo   Saved backup\.env.%TS%.bak

REM ── Step 2: backup/verify database ───────────────────────────
echo [2/8] Backing up database ^(if SQLite^)...
if exist "%~dp0backend\dip.db" (
    copy /y "%~dp0backend\dip.db" "%~dp0backup\dip.db.%TS%.bak" >nul
    echo   Saved backup\dip.db.%TS%.bak
) else (
    echo   No local dip.db ^(likely using an external Postgres/MySQL database — back that up separately^).
)

REM ── Step 3: stop application ──────────────────────────────────
echo [3/8] Stopping application...
if exist "%~dp0run\backend.pid" (
    set /p "PID=" < "%~dp0run\backend.pid"
    powershell -Command "Stop-Process -Id !PID! -ErrorAction SilentlyContinue" >nul 2>nul
    timeout /t 3 /nobreak >nul
    taskkill /PID !PID! /F >nul 2>nul
    del /q "%~dp0run\backend.pid" >nul 2>nul
)
echo   Stopped.

REM ── Step 4: update application files ─────────────────────────
echo [4/8] Updating application files...
robocopy "%NEWPKG%\backend\app" "%~dp0backend\app" /MIR /NFL /NDL /NJH /NJS >nul
robocopy "%NEWPKG%\backend\scripts" "%~dp0backend\scripts" /MIR /NFL /NDL /NJH /NJS >nul
if exist "%NEWPKG%\frontend\dist" (
    robocopy "%NEWPKG%\frontend\dist" "%~dp0frontend\dist" /MIR /NFL /NDL /NJH /NJS >nul
)
if exist "%NEWPKG%\backend\requirements.txt" (
    copy /y "%NEWPKG%\backend\requirements.txt" "%~dp0backend\requirements.txt" >nul
)
echo   Updated backend\app, backend\scripts, frontend\dist, requirements.txt.
echo   backend\.env, dip.db, uploads\, and logs\ were left untouched.

REM ── Step 5: install dependency changes ───────────────────────
echo [5/8] Installing dependency changes...
"%~dp0.venv\Scripts\pip.exe" install -r "%~dp0backend\requirements.txt"
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: pip install failed. See the output above.
    goto :fail
)

REM ── Step 6: run migrations ────────────────────────────────────
echo [6/8] Running database migrations...
"%~dp0.venv\Scripts\python.exe" "%~dp0backend\scripts\run_all_migrations.py"
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: migration run failed. Your pre-upgrade backups are in backup\%TS%*.
    echo   See the output above, fix the issue, and rerun upgrade.bat.
    goto :fail
)

REM ── Step 7: start application ──────────────────────────────────
echo [7/8] Starting application...
call "%~dp0start.bat" nowait

REM ── Step 8: health check ──────────────────────────────────────
echo [8/8] Health check...
set "HEALTH_OK=0"
for /l %%i in (1,1,15) do (
    curl -s -o nul -w "%%{http_code}" http://localhost:8080/api/health > "%TEMP%\dip_health.tmp" 2>nul
    set /p "HCODE=" < "%TEMP%\dip_health.tmp"
    if "!HCODE!"=="200" (
        set "HEALTH_OK=1"
        goto :health_done
    )
    timeout /t 2 /nobreak >nul
)
:health_done
del /q "%TEMP%\dip_health.tmp" >nul 2>nul

if "!HEALTH_OK!"=="0" (
    echo   ERROR: backend did not report healthy after upgrade. Check logs\backend.log.
    echo   Backups are available in backup\%TS%* if you need to roll back.
    goto :fail
)

echo.
echo ========================================
echo  Upgrade completed successfully.
echo ========================================
pause
exit /b 0

:fail
pause
exit /b 1
