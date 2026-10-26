$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
if (Get-NetTCPConnection -State Listen -LocalPort 8088,8443 -ErrorAction SilentlyContinue) { throw 'Port 8088 or 8443 is already in use. Stop the existing service before starting another.' }
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot '.state\server.pem'))) { throw 'Run setup_local.py first.' }
$taskPython = (Get-Command python).Source
$taskConsole = Start-Process -FilePath $taskPython -ArgumentList @('-m','uvicorn','server:app','--host','127.0.0.1','--port','8088') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRoot '.state\console.log') -RedirectStandardError (Join-Path $taskRoot '.state\console-error.log')
$taskTLS = Start-Process -FilePath $taskPython -ArgumentList @('-m','uvicorn','server:app','--host','0.0.0.0','--port','8443','--ssl-certfile','.state/server.pem','--ssl-keyfile','.state/server.key') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRoot '.state\tls.log') -RedirectStandardError (Join-Path $taskRoot '.state\tls-error.log')
@{ console=$taskConsole.Id; tls=$taskTLS.Id } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskRoot '.state\processes.local.json')
Write-Output 'Console running at http://127.0.0.1:8088; device TLS service at port 8443.'
