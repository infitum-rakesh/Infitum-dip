# build.ps1 — builds tally_agent.exe via PyInstaller.
#
# Real script, NOT run here — requires a Windows machine with Python +
# pywin32 + PyInstaller installed (see ../tally_agent/__init__.py's own
# disclosure on why this project has no Windows box to verify it on).
#
# Usage (from an elevated PowerShell, in this installer/ directory):
#   .\build.ps1

$ErrorActionPreference = "Stop"

Write-Host "Installing build dependencies..."
python -m pip install --upgrade pip
python -m pip install -r ..\requirements.txt
python -m pip install pyinstaller

Write-Host "Building tally_agent.exe..."
pyinstaller tally_agent.spec --clean --noconfirm

Write-Host "Build complete: dist\tally_agent.exe"
