# uninstall_service.ps1 — stops/removes the Windows Service and deletes
# local config/credentials/binaries.
#
# Real script, NOT run here — see build.ps1's own disclosure.
#
# Usage (elevated PowerShell):
#   .\uninstall_service.ps1 [-InstallDir "C:\Program Files\TallyAgent"]

param(
    [string] $InstallDir = "$env:ProgramFiles\TallyAgent"
)

$ErrorActionPreference = "Continue"

if (Test-Path "$InstallDir\tally_agent.exe") {
    Write-Host "Removing service and local config/credentials..."
    & "$InstallDir\tally_agent.exe" uninstall --yes
}

Write-Host "Deleting $InstallDir ..."
Remove-Item -Recurse -Force -Path $InstallDir -ErrorAction SilentlyContinue

Write-Host "Deleting %PROGRAMDATA%\TallyAgent (config/logs)..."
Remove-Item -Recurse -Force -Path "$env:ProgramData\TallyAgent" -ErrorAction SilentlyContinue

Write-Host "Uninstall complete."
