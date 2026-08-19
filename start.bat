@echo off
title Data Intelligence Platform — Launcher

echo.
echo  =============================================
echo   Data Intelligence Platform
echo   Starting backend and frontend servers...
echo  =============================================
echo.

REM ── Backend (FastAPI on port 8000) ──────────────────────────
start "DIP Backend (port 8000)" cmd /k "cd /d %~dp0backend && echo [Backend] Starting FastAPI... && %~dp0.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000"

REM Small delay so backend window opens first
timeout /t 2 /nobreak >nul

REM ── Frontend (Vite on port 5173) ────────────────────────────
start "DIP Frontend (port 5173)" cmd /k "cd /d %~dp0frontend && echo [Frontend] Starting Vite dev server... && npm run dev"

echo.
echo  Backend:  http://localhost:8000
echo  API Docs: http://localhost:8000/api/docs
echo  Frontend: http://localhost:5173
echo.
echo  Both servers launched in separate windows.
echo  Close those windows (or press Ctrl+C in each) to stop.
echo.
pause
