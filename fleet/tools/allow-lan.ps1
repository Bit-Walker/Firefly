#Requires -RunAsAdministrator
param([int]$Port=8443)
$ErrorActionPreference = 'Stop'
$taskPython = 'C:\Python312\python.exe'
$taskExisting = Get-NetFirewallRule -Name 'ESP32FleetLocalTLS' -ErrorAction SilentlyContinue
if (-not $taskExisting) {
  New-NetFirewallRule -Name 'ESP32FleetLocalTLS' -DisplayName 'ESP32 Fleet local TLS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet -Program $taskPython | Out-Null
}
Write-Output 'Allowed only local-subnet access to Python TCP port 8443 on Private networks.'
Write-Output 'To remove later: Remove-NetFirewallRule -Name ESP32FleetLocalTLS'
