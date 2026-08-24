# Removes the two Windows services this deployment created. MUST run elevated.
#
#   powershell -ExecutionPolicy Bypass -File uninstall-services.ps1
#
# Removes ONLY the two services. It does not touch postgresql-x64-17, the
# database, the data directory, the bundle or the env file — so the stack can
# still be run by hand with start-backend.ps1 and start-nginx.ps1 afterwards.

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
          ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this elevated: removing a Windows service needs Administrator.'
}

$Nssm = Join-Path $PSScriptRoot 'nssm.exe'

# Web first: it depends on the API, and Windows refuses to stop a service another
# running service depends on.
foreach ($name in @('AIBusinessAgentWeb', 'AIBusinessAgentAPI')) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "removing $name"
        & $Nssm stop   $name confirm | Out-Null
        & $Nssm remove $name confirm | Out-Null
    } else {
        Write-Host "$name is not installed"
    }
}

Write-Host "`nDone. PostgreSQL (postgresql-x64-17) was left running and Automatic." -ForegroundColor Green
