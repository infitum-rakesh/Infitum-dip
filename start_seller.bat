@echo off
title Data Intelligence Platform — Seller Portal Launcher

echo.
echo  =============================================
echo   Data Intelligence Platform — Seller Portal
echo   Starting backend and frontend, then opening
echo   the Seller screen in your browser...
echo  =============================================
echo.

REM ── Backend (FastAPI on port 8080 — matches frontend/vite.config.js's proxy target) ──
start "DIP Backend (port 8080)" cmd /k "cd /d %~dp0backend && echo [Backend] Starting FastAPI... && %~dp0.venv\Scripts\uvicorn.exe app.main:app --reload --port 8080"

REM Give the backend a moment to come up before the frontend starts
timeout /t 2 /nobreak >nul

REM ── Frontend (Vite on port 5173) ──────────────────────────────
start "DIP Frontend (port 5173)" cmd /k "cd /d %~dp0frontend && echo [Frontend] Starting Vite dev server... && npm run dev"

REM Give Vite a few seconds to boot before opening the browser
timeout /t 5 /nobreak >nul

REM ── Open the Seller screen directly ───────────────────────────
start "" "http://localhost:5173/app/seller"

echo.
echo  Backend:  http://localhost:8080
echo  API Docs: http://localhost:8080/api/docs
echo  Frontend: http://localhost:5173
echo  Seller Portal: http://localhost:5173/app/seller
echo.
echo  Log in with a Seller-role account, e.g.:
echo    seller@platform-admin.com / Seller@1234!
echo.
echo  Both servers launched in separate windows.
echo  Close those windows (or press Ctrl+C in each) to stop,
echo  or run stop.bat.
echo.
pause
