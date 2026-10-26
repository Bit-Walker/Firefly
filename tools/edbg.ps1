$ErrorActionPreference = 'Stop'
$taskPython = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run .\tools\bootstrap.ps1 first.' }
& $taskPython -m edbg_cli @args
exit $LASTEXITCODE
