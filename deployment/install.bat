@echo off
REM ============================================================
REM  Data Intelligence Platform — Windows Server Installer
REM ============================================================
REM  Lives at the root of the DataIntelligencePlatform/ package,
REM  as a sibling of backend/, frontend/, logs/, run/, uploads/.
REM  Resolves everything from %~dp0 — never depends on the
REM  caller's current working directory, and never hardcodes a
REM  developer-specific path.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ========================================
echo  Data Intelligence Platform Installation
echo ========================================
echo.

REM ── Step 1: Windows version check ───────────────────────────
echo [1/16] Checking Windows version...
ver | findstr /i "10\.\|11\.\| 6\.3" >nul
if !ERRORLEVEL! NEQ 0 (
    echo   WARNING: could not confirm this is Windows 10/11 or Server 2016+.
    echo            Continuing anyway — this is an advisory check only.
) else (
    echo   OK
)

REM ── Step 2: Python ───────────────────────────────────────────
echo [2/16] Checking Python installation...
set "PYTHON_BIN="
where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
    echo   Found Python !PYVER!
    set "PYTHON_BIN=python"
) else (
    where py >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        set "PYTHON_BIN=py -3"
        echo   Found Python via the py launcher.
    ) else (
        echo   ERROR: Python was not found on PATH.
        echo   Install Python 3.11 or 3.12 from https://www.python.org/downloads/windows/
        echo   and ensure "Add Python to PATH" is checked during setup.
        goto :fail
    )
)

REM ── Step 3: Node.js / npm (needed to build the frontend) ────
echo [3/16] Checking Node.js / npm...
where node >nul 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: Node.js was not found on PATH.
    echo   Install Node.js 20 LTS from https://nodejs.org/ and rerun install.bat.
    goto :fail
)
where npm >nul 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: npm was not found on PATH ^(usually installed alongside Node.js^).
    goto :fail
)
for /f "tokens=*" %%v in ('node --version') do echo   Found Node.js %%v

REM ── Step 4: required system directories exist before we need them
echo [4/16] Checking required system dependencies...
echo   (Tesseract OCR is checked later, after Python deps are installed
echo    and validate_config.py can actually import pytesseract.)

REM ── Step 5: Python virtual environment ──────────────────────
echo [5/16] Creating Python virtual environment...
if exist "%~dp0.venv\Scripts\python.exe" (
    echo   .venv already exists, skipping.
) else (
    %PYTHON_BIN% -m venv "%~dp0.venv"
    if !ERRORLEVEL! NEQ 0 (
        echo   ERROR: failed to create the virtual environment.
        goto :fail
    )
)
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_PIP=%~dp0.venv\Scripts\pip.exe"

REM ── Step 6: backend Python dependencies ──────────────────────
echo [6/16] Installing backend Python dependencies...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PIP%" install -r "%~dp0backend\requirements.txt"
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: pip install failed. See the output above.
    goto :fail
)

REM ── Step 7+8: frontend dependencies + production build ──────
echo [7/16] Installing frontend dependencies...
if exist "%~dp0frontend\package-lock.json" (
    call npm ci --prefix "%~dp0frontend"
) else (
    call npm install --prefix "%~dp0frontend"
)
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: npm install failed. See the output above.
    goto :fail
)

echo [8/16] Building frontend production bundle...
call npm run build --prefix "%~dp0frontend"
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: frontend build failed. See the output above.
    goto :fail
)
if not exist "%~dp0frontend\dist\index.html" (
    echo   ERROR: build finished but frontend\dist\index.html is missing.
    goto :fail
)
echo   OK — frontend\dist is ready ^(the backend serves it directly^).

REM ── Step 9: required directories ─────────────────────────────
echo [9/16] Creating required directories...
if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0run" mkdir "%~dp0run"
if not exist "%~dp0uploads" mkdir "%~dp0uploads"
echo   OK

REM ── Step 10: environment configuration ───────────────────────
echo [10/16] Configuring environment...
if exist "%~dp0backend\.env" (
    echo   backend\.env already exists.
    set /p "RECONFIGURE=  Reconfigure it now? [y/N]: "
    if /i not "!RECONFIGURE!"=="y" goto :skip_env_wizard
)

echo.
echo   ---- Database configuration ----
echo   [1] SQLite   ^(default — good for evaluation / small deployments^)
echo   [2] PostgreSQL ^(recommended for production^)
echo   [3] MySQL
echo   [4] SQL Server ^(NOT supported as the platform database in this
echo                    build — no async driver installed. Offered only
echo                    for Bring-Your-Own-Database tenant targets you
echo                    configure LATER inside the app, not this
echo                    installer.^)
set /p "DBCHOICE=  Select database type [1]: "
if "!DBCHOICE!"=="" set "DBCHOICE=1"

if "!DBCHOICE!"=="4" (
    echo   SQL Server cannot be used as the platform database. Defaulting to SQLite.
    set "DBCHOICE=1"
)

if "!DBCHOICE!"=="1" (
    set "DATABASE_URL=sqlite+aiosqlite:///./dip.db"
    echo.
    set /p "CONFIRM_SQLITE=  No database details given — use SQLite ^(can migrate to a real DB later^)? [Y/n]: "
    if /i "!CONFIRM_SQLITE!"=="n" (
        echo   Rerun install.bat and choose PostgreSQL or MySQL instead.
        goto :fail
    )
) else (
    set /p "DBHOST=  Host: "
    set /p "DBPORT=  Port: "
    set /p "DBNAME=  Database name: "
    set /p "DBUSER=  Username: "
    powershell -Command "$p = Read-Host -Prompt 'Password' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))" > "%TEMP%\dip_dbpass.tmp"
    set /p "DBPASS=" < "%TEMP%\dip_dbpass.tmp"
    del /q "%TEMP%\dip_dbpass.tmp" >nul 2>nul
    set /p "DBSSL=  Use SSL? [y/N]: "

    if "!DBCHOICE!"=="2" set "DBDRIVER=postgresql+asyncpg"
    if "!DBCHOICE!"=="3" set "DBDRIVER=mysql+aiomysql"
    set "DATABASE_URL=!DBDRIVER!://!DBUSER!:!DBPASS!@!DBHOST!:!DBPORT!/!DBNAME!"
    if /i "!DBSSL!"=="y" set "DATABASE_URL=!DATABASE_URL!?ssl=true"
)

echo.
echo   ---- Security configuration ----
for /f "delims=" %%s in ('powershell -Command "[guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()"') do set "SECRET_KEY=%%s"
echo   Generated a random SECRET_KEY.

echo.
echo   ---- Application configuration ----
set /p "PUBLIC_URL=  Public base URL this server will be reached at (e.g. https://dip.yourcompany.com) [http://localhost:8080]: "
if "!PUBLIC_URL!"=="" set "PUBLIC_URL=http://localhost:8080"
set /p "WORKERCOUNT=  uvicorn worker processes [1 — recommended, see README_DEPLOYMENT.md]: "
if "!WORKERCOUNT!"=="" set "WORKERCOUNT=1"
if not "!WORKERCOUNT!"=="1" (
    echo   WARNING: more than 1 worker means SQL Workspace query cancellation
    echo   and other in-memory task state will NOT be shared across workers.
    set /p "WORKERCONFIRM=  Proceed with !WORKERCOUNT! workers anyway? [y/N]: "
    if /i not "!WORKERCONFIRM!"=="y" set "WORKERCOUNT=1"
)

(
    echo APP_ENV=production
    echo HOST=0.0.0.0
    echo PORT=8080
    echo DATABASE_URL=!DATABASE_URL!
    echo SECRET_KEY=!SECRET_KEY!
    echo FRONTEND_BASE_URL=!PUBLIC_URL!
    echo BACKEND_PUBLIC_BASE_URL=!PUBLIC_URL!
    echo ALLOWED_ORIGINS=!PUBLIC_URL!
    echo WORKER_COUNT=!WORKERCOUNT!
    echo UPLOAD_STORAGE_PATH=./uploads
    echo MAX_UPLOAD_SIZE_MB=25
    echo LOG_MAX_BYTES=10000000
    echo LOG_BACKUP_COUNT=5
) > "%~dp0backend\.env"
echo   Wrote backend\.env

:skip_env_wizard

REM ── Step 11: validate configuration ──────────────────────────
echo [11/16] Validating configuration...
"%VENV_PY%" "%~dp0backend\scripts\validate_config.py"
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: configuration is invalid. Fix the FAILED item(s) above and rerun install.bat.
    goto :fail
)

REM ── Step 12+13: run database migrations / create tables ─────
echo [12/16] Running database migrations...
"%VENV_PY%" "%~dp0backend\scripts\run_all_migrations.py"
if !ERRORLEVEL! NEQ 0 (
    echo   ERROR: migration run failed. See the output above.
    echo   Fix the issue and rerun install.bat — it is safe to rerun
    echo   ^(every migration step is idempotent^).
    goto :fail
)

REM ── Step 14: create initial Seller Admin account ─────────────
echo [14/16] Creating initial Seller Admin account...
echo   ^(A default seller login already exists automatically —
echo    seller@platform-admin.com — see README_DEPLOYMENT.md.
echo    This step lets you set your OWN email/password instead.^)
set /p "MAKE_SELLER=  Create/reset a Seller Admin with your own credentials now? [Y/n]: "
if /i not "!MAKE_SELLER!"=="n" (
    "%VENV_PY%" "%~dp0backend\scripts\create_seller_account.py"
    if !ERRORLEVEL! NEQ 0 (
        echo   WARNING: Seller Admin creation did not complete. You can rerun
        echo   this later with: .venv\Scripts\python backend\scripts\create_seller_account.py
    )
) else (
    echo   Skipped — the auto-seeded default Seller Admin account will be used.
)

REM ── Step 15: health checks ────────────────────────────────────
echo [15/16] Performing health checks...
call "%~dp0start.bat" nowait
echo   Waiting for the backend to come up...
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
    echo   ERROR: backend did not report healthy within 30 seconds.
    echo   Check logs\backend.log for details.
    goto :fail
)
echo   OK — backend is healthy.

REM ── Step 16: installation summary ─────────────────────────────
echo [16/16] Installation summary
echo.
echo ========================================
echo  Installation completed successfully.
echo ========================================
echo.
echo  Application: http://localhost:8080
echo  Backend:     http://localhost:8080/api
echo  Health:      http://localhost:8080/api/health
echo  Database:    CONNECTED
echo  Storage:     READY
echo  Configuration: backend\.env
echo.
echo  Next steps:
echo   1. Application is already running ^(started during health check^).
echo   2. Open http://localhost:8080 in a browser.
echo   3. Log in as Seller Admin.
echo   4. Configure connections.
echo   5. Configure pipelines.
echo   6. Upload documents.
echo   7. Start processing.
echo.
echo  Use start.bat / stop.bat / status.bat to manage the service.
pause
exit /b 0

:fail
echo.
echo ========================================
echo  INSTALLATION FAILED
echo ========================================
echo  Fix the error reported above, then rerun install.bat.
echo  It is safe to rerun — completed steps are skipped or are
echo  idempotent, so nothing will be duplicated.
echo ========================================
pause
exit /b 1
