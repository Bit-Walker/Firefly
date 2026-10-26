param([switch]$Offline)
$ErrorActionPreference = 'Stop'
$taskWorkspace = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskWorkspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
  & python -m venv --system-site-packages (Join-Path $taskWorkspace '.venv')
  if ($LASTEXITCODE -ne 0) { throw 'Could not create the workspace Python environment.' }
}
$taskPackages = @('packages/analysis','packages/sdk-python','apps/control-plane','apps/cli')
$taskArguments = @('-m','pip','install','--disable-pip-version-check')
if ($Offline) { $taskArguments += @('--no-deps','--no-build-isolation') }
foreach ($taskPackage in $taskPackages) {
  $taskArguments += @('--editable',(Join-Path $taskWorkspace $taskPackage))
}
& $taskPython @taskArguments
if ($LASTEXITCODE -ne 0) { throw 'Workspace package installation failed.' }
Write-Output 'Installed local workspace packages in .venv. Run .\tools\edbg.ps1 --help.'
