param([string]$Port='COM9',[string]$DeviceId='s3-fc012cd58460',[string]$Version='3.0.0')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
if ($DeviceId -notmatch '^s3-[0-9a-f]{12}$' -or $Version -notmatch '^[A-Za-z0-9.-]{1,31}$') { throw 'Invalid device ID or version.' }
$taskRelease = Join-Path $taskRoot "artifacts\$Version"
$taskNvs = Join-Path $taskRoot ".state\$DeviceId.nvs.local.bin"
$taskBackup = Join-Path (Split-Path $taskRoot -Parent) "backup\$DeviceId-before-fleet-flash-16MB.bin"
if (-not (Test-Path -LiteralPath $taskBackup) -or (Get-Item -LiteralPath $taskBackup).Length -ne 16777216) { throw 'Make a complete 16 MB backup before the first installation.' }
foreach ($taskPath in @($taskNvs,(Join-Path $taskRelease 'bootloader.bin'),(Join-Path $taskRelease 'partition-table.bin'),(Join-Path $taskRelease 'fleet_agent.bin'))) {
  if (-not (Test-Path -LiteralPath $taskPath)) { throw "Missing installation file: $taskPath" }
}
$taskIdentity = & python -m esptool --chip esp32s3 --port $Port --no-stub read_mac 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify board identity.' }
$taskMac = [regex]::Match($taskIdentity,'MAC: ([0-9a-fA-F:]{17})')
if (-not $taskMac.Success -or ('s3-' + $taskMac.Groups[1].Value.Replace(':','').ToLowerInvariant()) -ne $DeviceId) { throw 'Serial port board MAC does not match the requested device ID. Nothing was flashed.' }
& python -m esptool --chip esp32s3 --port $Port --baud 921600 --after no_reset erase_region 0xe000 0x2000
if ($LASTEXITCODE -ne 0) { throw 'Failed to clear OTA metadata.' }
& python -m esptool --chip esp32s3 --port $Port --baud 921600 write_flash --flash_mode dio --flash_freq 80m --flash_size 16MB 0x0 (Join-Path $taskRelease 'bootloader.bin') 0x8000 (Join-Path $taskRelease 'partition-table.bin') 0x9000 $taskNvs 0x10000 (Join-Path $taskRelease 'fleet_agent.bin')
if ($LASTEXITCODE -ne 0) { throw 'Initial installation failed.' }
