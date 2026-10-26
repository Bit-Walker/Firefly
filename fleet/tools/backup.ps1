param([string]$Port='COM9')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskInfo = & python -m esptool --chip esp32s3 --port $Port --no-stub flash_id 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the board.' }
$taskMac = [regex]::Match($taskInfo,'MAC: ([0-9a-fA-F:]{17})')
if (-not $taskMac.Success -or $taskInfo -notmatch 'Detected flash size: 16MB') { throw 'Expected an ESP32-S3 with 16 MB Flash.' }
$taskDeviceId = 's3-' + $taskMac.Groups[1].Value.Replace(':','').ToLowerInvariant()
$taskBackupDirectory = Join-Path (Split-Path $taskRoot -Parent) 'backup'
New-Item -ItemType Directory -Force -Path $taskBackupDirectory | Out-Null
$taskOutput = Join-Path $taskBackupDirectory "$taskDeviceId-before-fleet-flash-16MB.bin"
if (Test-Path -LiteralPath $taskOutput) { Write-Output "Existing backup retained: $taskOutput"; return }
& python -m esptool --chip esp32s3 --port $Port --baud 921600 read_flash 0 0x1000000 $taskOutput
if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $taskOutput).Length -ne 16777216) { throw 'Backup incomplete. Do not flash this device yet.' }
Get-FileHash -Algorithm SHA256 -LiteralPath $taskOutput | Format-List | Out-String | Set-Content -LiteralPath "$taskOutput.sha256.txt"
Write-Output "Device ID: $taskDeviceId"
Write-Output "Backup: $taskOutput"
