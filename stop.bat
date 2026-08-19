@echo off
echo Stopping Data Intelligence Platform servers...
taskkill /FI "WINDOWTITLE eq DIP Backend*"  /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq DIP Frontend*" /F >nul 2>&1
echo Done.
