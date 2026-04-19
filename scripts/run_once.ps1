# Run all enabled bots once. Intended for Windows Task Scheduler.
#
# Pre-conditions expected on the host:
#   * IB Gateway is already running and logged into the PAPER account
#     (auto-login or stay-logged-in for 24h — see TASK_SCHEDULER.md)
#   * .venv exists at repo root
#   * .env has BROKER_BACKEND=ibkr when you've flipped over from mock
#
# Exit code:
#   0  success (all enabled bots ran; individual bot errors are caught inside
#      core.runner and do NOT fail the task)
#   1  unrecoverable error (Python crash, venv missing, etc.) — Task Scheduler
#      will retry next window

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Error "Virtualenv python not found at $Python. Create .venv first."
    exit 1
}

$LogDir = Join-Path $RepoRoot 'data\logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Stamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile = Join-Path $LogDir "run_$Stamp.log"

Write-Host "[$Stamp] Running bots. Log: $LogFile"
& $Python -u main.py --once 2>&1 | Tee-Object -FilePath $LogFile
$code = $LASTEXITCODE
Write-Host "[$(Get-Date -Format 'yyyyMMdd_HHmmss')] Exit code: $code"
exit $code
