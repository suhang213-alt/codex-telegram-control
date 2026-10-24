$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
py -3 -c "import sys; raise SystemExit(sys.version_info < (3,11))"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ from python.org first.' }
py -3 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
& '.\.venv\Scripts\python.exe' -m pip install .
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
& '.\.venv\Scripts\codex-telegram.exe' setup
if ($LASTEXITCODE -ne 0) { throw 'Setup is incomplete; follow the displayed diagnostics.' }
Write-Host 'Next: .\.venv\Scripts\codex-telegram.exe login'
Write-Host 'Then: .\.venv\Scripts\codex-telegram.exe doctor --protocol'
Write-Host 'Run: .\.venv\Scripts\codex-telegram.exe run'
