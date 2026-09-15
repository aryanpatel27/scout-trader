# agentic-trader — Windows one-time setup. Run in PowerShell from the project folder:
#   Set-ExecutionPolicy -Scope Process Bypass; .\deploy\windows\setup.ps1
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root
Write-Host "== project: $root"

function Find-Py($versions) { foreach ($v in $versions) { try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return $v } } catch {} } ; return $null }

$engPy = Find-Py @("3.13","3.12","3.11")
if (-not $engPy) { throw "Need Python 3.11+ (install from python.org, tick 'Add to PATH' + 'py launcher')" }
Write-Host "== engine python: $engPy"
& py "-$engPy" -m venv .venv
.\.venv\Scripts\python -m pip install --quiet --upgrade pip
.\.venv\Scripts\python -m pip install --quiet yfinance tzdata
Write-Host "== engine venv ready"

$sdkPy = Find-Py @("3.11","3.10","3.9","3.12")
if (-not $sdkPy) { throw "Need a Python 3.9-3.12 for the Webull SDK (install 3.11 from python.org)" }
Write-Host "== webull sdk python: $sdkPy"
& py "-$sdkPy" -m venv .venv-webull
.\.venv-webull\Scripts\python -m pip install --quiet --upgrade pip
try {
  .\.venv-webull\Scripts\python -m pip install --quiet webull-python-sdk-core webull-python-sdk-trade
} catch {
  Write-Host "   full install failed (grpcio) — using the no-deps recipe"
  .\.venv-webull\Scripts\python -m pip install --quiet --no-deps webull-python-sdk-core webull-python-sdk-trade webull-python-sdk-mdata webull-python-sdk-trade-events-core
  .\.venv-webull\Scripts\python -m pip install --quiet cryptography "jmespath<1.0.0" "cachetools==5.2.0" protobuf python-dateutil six requests
}
Write-Host "== webull venv ready"

$env:PYTHONPATH = "$root\src"
.\.venv\Scripts\python -m trader selftest
if ($LASTEXITCODE -ne 0) { throw "selftest failed" }

if (Test-Path ".env") {
  .\.venv-webull\Scripts\python src\trader\live_exec.py check
} else { Write-Host "!! no .env yet — copy .env.example to .env and fill in your Webull keys (never share them)" }

# Auto-start at logon, hidden window, restarts forever via start.ps1's loop
$task = "AgenticTrader"
$cmd  = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\deploy\windows\start.ps1`""
schtasks /Create /F /TN $task /TR $cmd /SC ONLOGON /RL LIMITED | Out-Null
Write-Host "== scheduled task '$task' created (runs at logon)"
Write-Host ""
Write-Host "NEXT: Settings > System > Power: set Sleep = Never (plugged in). Then start now with:"
Write-Host "      schtasks /Run /TN $task      and open http://localhost:8787"
