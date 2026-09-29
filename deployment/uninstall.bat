@echo off
REM ============================================================
REM  Data Intelligence Platform — uninstall.bat
REM  Default is to KEEP data (dip.db / uploads / logs) — deleting
REM  a production database/document store from an uninstaller by
REM  default is exactly the kind of silent destructive action this
REM  project avoids. Deletion only happens if explicitly confirmed.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ========================================
echo  Data Intelligence Platform — Uninstall
echo ========================================
echo.

call "%~dp0stop.bat" nowait_placeholder >nul 2>nul
REM stop.bat always pauses at the end; call it directly instead so this
REM script controls its own flow.
if exist "%~dp0run\backend.pid" (
    set /p "PID=" < "%~dp0run\backend.pid"
    powershell -Command "Stop-Process -Id !PID! -ErrorAction SilentlyContinue" >nul 2>nul
    taskkill /PID !PID! /F >nul 2>nul
    del /q "%~dp0run\backend.pid" >nul 2>nul
    echo Stopped running backend.
)

echo.
set /p "DELDATA=Delete the database, uploaded documents, and logs too? [y/N] (default keeps them): "
if /i "!DELDATA!"=="y" (
    set /p "CONFIRM=Type YES to permanently delete backend\dip.db, uploads\, and logs\: "
    if "!CONFIRM!"=="YES" (
        if exist "%~dp0backend\dip.db" del /q "%~dp0backend\dip.db"
        if exist "%~dp0uploads" rmdir /s /q "%~dp0uploads"
        if exist "%~dp0logs" rmdir /s /q "%~dp0logs"
        echo Data deleted.
    ) else (
        echo Confirmation did not match "YES" — data was NOT deleted.
    )
) else (
    echo Keeping database, uploads, and logs. Back them up separately if you
    echo intend to reinstall on a different machine — see README_DEPLOYMENT.md
    echo "Backup" section.
)

echo.
set /p "DELVENV=Remove the Python virtual environment (.venv)? [Y/n]: "
if /i not "!DELVENV!"=="n" (
    if exist "%~dp0.venv" rmdir /s /q "%~dp0.venv"
    echo Removed .venv.
)

if exist "%~dp0run" rmdir /s /q "%~dp0run"

echo.
echo Uninstall complete. The application folder itself was not deleted —
echo remove it manually once you've confirmed any data you wanted is backed up.
pause
exit /b 0
