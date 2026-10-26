$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPidFile = Join-Path $taskRoot '.state\processes.local.json'
if (Test-Path -LiteralPath $taskPidFile) {
  $taskProcesses = Get-Content -LiteralPath $taskPidFile -Raw | ConvertFrom-Json
  foreach ($taskProcessId in @($taskProcesses.console,$taskProcesses.tls)) {
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId"
    if ($taskProcess -and $taskProcess.Name -match '^python' -and $taskProcess.CommandLine -match '-m uvicorn server:app') {
      Stop-Process -Id $taskProcessId
    }
  }
}
Write-Output 'Stopped the local fleet processes recorded by start.ps1.'
