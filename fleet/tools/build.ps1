param([string]$Version = '4.0.0',[switch]$RollbackProbe)
$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^[A-Za-z0-9.-]{1,31}$') { throw 'Use a version containing only letters, digits, dots and hyphens (up to 31 characters).' }
$taskRoot = Split-Path $PSScriptRoot -Parent
$env:IDF_TOOLS_PATH = Join-Path $env:USERPROFILE '.espressif'
if (-not $env:FLEET_IDF_PATH) { $env:FLEET_IDF_PATH = Join-Path $env:USERPROFILE 'esp\v5.3.1\esp-idf' }
$env:IDF_PATH = $env:FLEET_IDF_PATH
$env:IDF_PYTHON_ENV_PATH = Join-Path $env:IDF_TOOLS_PATH 'python_env\idf5.3_py3.11_env'
$env:PATH = "$env:IDF_PYTHON_ENV_PATH\Scripts;$env:PATH"
& "$env:IDF_PATH\export.ps1" *> $null
if ($LASTEXITCODE -ne 0) { throw 'ESP-IDF environment activation failed.' }
$env:IDF_TARGET = 'esp32s3'
Push-Location (Join-Path $taskRoot 'firmware')
try {
  $taskProbe = if ($RollbackProbe) { 'ON' } else { 'OFF' }
  & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" -D "PROJECT_VER=$Version" -D "FLEET_TEST_CRASH_BEFORE_CONFIRM=$taskProbe" build
  if ($LASTEXITCODE -ne 0) { throw 'Firmware compilation failed.' }
  $taskOutput = Join-Path $taskRoot "artifacts\$Version"
  New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
  foreach ($taskFile in @('fleet_agent.bin','fleet_agent.elf','fleet_agent.map','flasher_args.json')) {
    Copy-Item -LiteralPath (Join-Path $taskRoot "firmware\build\$taskFile") -Destination $taskOutput
  }
  Copy-Item -LiteralPath (Join-Path $taskRoot 'firmware\build\bootloader\bootloader.bin') -Destination $taskOutput
  Copy-Item -LiteralPath (Join-Path $taskRoot 'firmware\build\partition_table\partition-table.bin') -Destination $taskOutput
  Write-Output "Saved application, bootloader and debugging symbols: $taskOutput"
} finally { Pop-Location }
