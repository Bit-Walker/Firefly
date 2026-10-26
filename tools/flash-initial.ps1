param([string]$Port = 'COM9')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskBuild = Join-Path $taskRoot 'artifacts\usb-v1'
foreach ($taskFile in @('simple_ota.ino.bootloader.bin','simple_ota.ino.partitions.bin','simple_ota.ino.bin')) {
    if (-not (Test-Path -LiteralPath (Join-Path $taskBuild $taskFile))) { throw 'Build v1 first: .\tools\build.ps1 -Version 1' }
}
# OTA metadata must be reset when installing the initial partition layout.
# The original full-Flash backup should be made BEFORE this script is used.
& python -m esptool --chip esp32s3 --port $Port --baud 921600 erase_region 0xe000 0x2000
if ($LASTEXITCODE -ne 0) { throw 'Could not clear the OTA selection metadata.' }
& python -m esptool --chip esp32s3 --port $Port --baud 921600 write_flash --flash_mode dio --flash_freq 80m --flash_size 16MB 0x0 (Join-Path $taskBuild 'simple_ota.ino.bootloader.bin') 0x8000 (Join-Path $taskBuild 'simple_ota.ino.partitions.bin') 0x10000 (Join-Path $taskBuild 'simple_ota.ino.bin')
if ($LASTEXITCODE -ne 0) { throw 'Initial USB flash failed.' }
