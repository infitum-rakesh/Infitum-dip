@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 3000
    goto :eof
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python -m uvicorn main:app --host 127.0.0.1 --port 3000
    goto :eof
)

echo Python was not found in PATH.
echo Install Python 3.12+ and run: python -m pip install -r requirements.txt
exit /b 1