$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use this installer on the Windows computer with the existing Desktop app.' }
$repositoryPath = Split-Path -Parent $PSScriptRoot
& py -3.12 -m venv (Join-Path $repositoryPath '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 with venv is required.' }
$taskPython = Join-Path $repositoryPath '.venv\Scripts\python.exe'
& $taskPython -m pip install $repositoryPath
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
& $taskPython (Join-Path $PSScriptRoot 'docker.py') desktop-setup
if ($LASTEXITCODE -ne 0) { throw 'Native adapter setup failed.' }
& $taskPython (Join-Path $PSScriptRoot 'docker.py') setup
if ($LASTEXITCODE -ne 0) { throw 'Docker private setup failed.' }
Write-Output 'Setup complete. Follow docs/DOCKER.md; Windows native compatibility and gateway preparation are still required.'
