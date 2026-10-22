# install_service.ps1 — installs tally_agent.exe as a Windows Service
# and performs enrollment.
#
# Real script, NOT run here — see build.ps1's own disclosure.
#
# Usage (elevated PowerShell):
#   .\install_service.ps1 -ServerUrl "https://dip.example.com" -Token "<enrollment-token>" `
#       [-TallyHost "127.0.0.1"] [-TallyPort 9000] [-InstallDir "C:\Program Files\TallyAgent"]

param(
    [Parameter(Mandatory = $true)] [string] $ServerUrl,
    [Parameter(Mandatory = $true)] [string] $Token,
    [string] $TallyHost = "127.0.0.1",
    [int] $TallyPort = 9000,
    [string] $InstallDir = "$env:ProgramFiles\TallyAgent"
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsIdentity]::GetCurrent().Groups -contains "S-1-5-32-544")) {
    Write-Error "This script must be run from an elevated (Administrator) PowerShell session."
    exit 1
}

Write-Host "Installing to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path "..\dist\tally_agent.exe" -Destination "$InstallDir\tally_agent.exe" -Force

Write-Host "Enrolling agent with $ServerUrl ..."
& "$InstallDir\tally_agent.exe" enroll --server-url $ServerUrl --token $Token --tally-host $TallyHost --tally-port $TallyPort

Write-Host "Installing Windows Service..."
& "$InstallDir\tally_agent.exe" service install

Write-Host "Starting service..."
& "$InstallDir\tally_agent.exe" service start

Write-Host "Done. Check status with: & '$InstallDir\tally_agent.exe' service status"
