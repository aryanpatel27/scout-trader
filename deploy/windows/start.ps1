# Keeps the trader running forever (restart on crash). Launched by the AgenticTrader scheduled task.
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root
$env:PYTHONPATH = "$root\src"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
while ($true) {
  & "$root\.venv\Scripts\python.exe" -m trader serve *>> "$root\logs\trader.log"
  Start-Sleep -Seconds 5
}
