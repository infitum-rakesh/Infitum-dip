@echo off
title Data Intelligence Platform — Launcher
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo  =============================================
echo   Data Intelligence Platform
echo   Starting backend and frontend servers...
echo  =============================================
echo.

REM ── Locate Python ────────────────────────────────────────────
set "PYTHON_BIN="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_BIN=%~dp0.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if !ERRORLEVEL!==0 (
        set "PYTHON_BIN=py -3"
    ) else (
        where python >nul 2>nul
        if !ERRORLEVEL!==0 (
            set "PYTHON_BIN=python"
        ) else (
            echo [ERROR] Python was not found on PATH. Install Python 3.11 or 3.12 first.
            pause
            exit /b 1
        )
    )
)

REM ── Create the venv on first run ─────────────────────────────
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [INFO] .venv not found. Creating virtual environment...
    %PYTHON_BIN% -m venv "%~dp0.venv"
    set "PYTHON_BIN=%~dp0.venv\Scripts\python.exe"
    echo [INFO] Installing backend dependencies (first run only, this can take a while)...
    "%~dp0.venv\Scripts\pip.exe" install -r "%~dp0backend\requirements.txt"
)

REM ── Backend (FastAPI on port 8080 — matches frontend/vite.config.js's
REM    dev proxy target; do NOT change this port without also updating
REM    vite.config.js, or the UI will silently fail to reach the API) ──
start "DIP Backend (port 8080)" cmd /k "cd /d %~dp0backend && echo [Backend] Starting FastAPI on port 8080... && %~dp0.venv\Scripts\uvicorn.exe app.main:app --reload --port 8080"

REM Small delay so backend window opens first
timeout /t 2 /nobreak >nul

REM ── Frontend (Vite on port 5173) ────────────────────────────
start "DIP Frontend (port 5173)" cmd /k "cd /d %~dp0frontend && if not exist node_modules (echo [Frontend] Installing npm packages, first run only... && npm install) && echo [Frontend] Starting Vite dev server... && npm run dev"

echo.
echo  Backend:  http://localhost:8080
echo  API Docs: http://localhost:8080/api/docs
echo  Frontend: http://localhost:5173
echo.
echo  Both servers launched in separate windows.
echo  Close those windows (or press Ctrl+C in each) to stop.
echo.
pause
