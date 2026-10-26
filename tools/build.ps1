param([ValidateSet(1,2)][int]$Version = 2)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskCli = Join-Path $env:LOCALAPPDATA 'Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe'
if (-not (Test-Path -LiteralPath $taskCli)) {
    $taskCli = (Get-Command arduino-cli -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'simple_ota\config.h'))) {
    throw 'Copy simple_ota/config.example.h to config.h and enter your Wi-Fi / OTA credentials first.'
}
$taskBuild = Join-Path $taskRoot 'build\work'
$taskArtifacts = Join-Path $taskRoot 'artifacts'
New-Item -ItemType Directory -Force -Path $taskBuild,$taskArtifacts | Out-Null
$taskBoard = 'esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,FlashMode=dio,PSRAM=disabled,CDCOnBoot=default,UploadSpeed=921600'
$taskVersionHeader = "#pragma once`n#ifndef DEMO_VERSION`n#define DEMO_VERSION $Version`n#endif`n"
[System.IO.File]::WriteAllText((Join-Path $taskRoot 'simple_ota\demo_version.h'), $taskVersionHeader)
& $taskCli compile --jobs 6 --fqbn $taskBoard --build-path $taskBuild (Join-Path $taskRoot 'simple_ota')
if ($LASTEXITCODE -ne 0) { throw 'Firmware compilation failed.' }
Copy-Item -LiteralPath (Join-Path $taskBuild 'simple_ota.ino.bin') -Destination (Join-Path $taskArtifacts "ota-v$Version.bin")
$taskInitial = Join-Path $taskArtifacts "usb-v$Version"
New-Item -ItemType Directory -Force -Path $taskInitial | Out-Null
foreach ($taskFile in @('simple_ota.ino.bootloader.bin','simple_ota.ino.partitions.bin','simple_ota.ino.bin')) {
    Copy-Item -LiteralPath (Join-Path $taskBuild $taskFile) -Destination (Join-Path $taskInitial $taskFile)
}
Write-Output "OTA application: $taskArtifacts\ota-v$Version.bin"
