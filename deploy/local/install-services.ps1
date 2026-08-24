# Registers the localhost stack as Windows services, so the platform comes back
# on its own after a restart. MUST run elevated.
#
#   powershell -ExecutionPolicy Bypass -File install-services.ps1
#
# Three services end up in the chain, and only two are created here:
#
#   postgresql-x64-17     already Automatic — the PostgreSQL installer made it
#   AIBusinessAgentAPI    uvicorn on 127.0.0.1:8000, depends on PostgreSQL
#   AIBusinessAgentWeb    nginx on 127.0.0.1:80,    depends on the API
#
# Why NSSM rather than sc.exe: neither uvicorn nor nginx is a service binary —
# they are console programs, and Windows kills a console program that does not
# answer the service control manager within 30 seconds. NSSM is the supervisor
# that sits between: it starts the program, restarts it if it exits, and
# translates Stop into a signal the program understands.
#
# Both services run as LocalSystem (NSSM's default). Running them as the desktop
# user would need that account's password, and LocalSystem already has the local
# rights they need: read the virtualenv, write data\uploads and reports. One
# consequence worth knowing — files an import stages are then owned by SYSTEM
# rather than by you.

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
          ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this elevated: creating a Windows service needs Administrator.'
}

$Here       = $PSScriptRoot
$RepoRoot   = (Resolve-Path (Join-Path $Here '..\..')).Path
$BackendDir = Join-Path $RepoRoot 'backend'
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Nssm       = Join-Path $Here 'nssm.exe'
$NginxDir   = Join-Path $Here 'nginx'
$NginxExe   = Join-Path $NginxDir 'nginx.exe'
$Prefix     = Join-Path $Here 'run'
$LogDir     = Join-Path $Prefix 'logs'
$EnvFile    = Join-Path $Here '.env.production'

foreach ($p in @($Nssm, $VenvPython, $NginxExe, $EnvFile)) {
    if (-not (Test-Path $p)) { throw "Missing prerequisite: $p" }
}
New-Item -ItemType Directory -Force -Path $LogDir, (Join-Path $Prefix 'temp') | Out-Null

$API = 'AIBusinessAgentAPI'
$WEB = 'AIBusinessAgentWeb'
$PG  = 'postgresql-x64-17'

# --- the environment the API service runs with ------------------------------
# Read from .env.production so there is ONE source of truth, not a second copy
# living in the service definition. Written to the registry as a REG_MULTI_SZ
# rather than passed on nssm's command line: MASTER_DATA_FILE contains a space,
# and quoting it through two layers of argument parsing is the kind of detail
# that fails silently and leaves the service reading the wrong file.
$envPairs = foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
    $kv = $t.Split('=', 2)
    '{0}={1}' -f $kv[0].Trim(), $kv[1].Trim().Trim('"').Trim("'")
}
Write-Host "Loaded $($envPairs.Count) environment entries from .env.production" -ForegroundColor Cyan

function Remove-ServiceIfPresent([string]$name) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "  removing existing service $name"
        & $Nssm stop   $name confirm | Out-Null
        & $Nssm remove $name confirm | Out-Null
        Start-Sleep -Seconds 2
    }
}

function Set-CommonServiceOptions([string]$name, [string]$stem) {
    & $Nssm set $name Start          SERVICE_AUTO_START     | Out-Null
    & $Nssm set $name AppStdout      (Join-Path $LogDir "$stem-stdout.log") | Out-Null
    & $Nssm set $name AppStderr      (Join-Path $LogDir "$stem-stderr.log") | Out-Null
    & $Nssm set $name AppRotateFiles 1                      | Out-Null
    & $Nssm set $name AppRotateBytes 10485760               | Out-Null
    # Restart on an unexpected exit, but back off so a service that cannot start
    # at all does not spin: 5 s between attempts, and give up after 3 in 90 s.
    & $Nssm set $name AppExit Default Restart               | Out-Null
    & $Nssm set $name AppRestartDelay 5000                  | Out-Null
    & $Nssm set $name AppThrottle     10000                 | Out-Null
    & $Nssm set $name AppKillProcessTree 1                  | Out-Null
}

# ---------------------------------------------------------------- API service
Write-Host "`n== $API ==" -ForegroundColor Yellow
Remove-ServiceIfPresent $API

# One worker, no --reload. Both matter and neither is a preference: live import
# progress lives in this process's memory (utils/progress.py) and the import
# thread pool is per-process, so a second worker would answer a progress poll
# with "unknown job"; --reload would restart the process mid-import.
& $Nssm install $API $VenvPython '-m' 'uvicorn' 'app.main:app' `
    '--host' '127.0.0.1' '--port' '8000' '--workers' '1' `
    '--log-level' 'info' '--proxy-headers' '--forwarded-allow-ips' '127.0.0.1' | Out-Null

& $Nssm set $API AppDirectory   $BackendDir | Out-Null
& $Nssm set $API DisplayName    'AI Business Agent - API' | Out-Null
& $Nssm set $API Description    'FastAPI backend for the AI Business Reporting Agent (uvicorn, 127.0.0.1:8000).' | Out-Null
& $Nssm set $API DependOnService $PG | Out-Null
Set-CommonServiceOptions $API 'api'

# NSSM reads AppEnvironmentExtra from here as a REG_MULTI_SZ.
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$API\Parameters" `
                 -Name 'AppEnvironmentExtra' -Value $envPairs -Type MultiString
Write-Host "  installed, depends on $PG, $($envPairs.Count) env vars set"

# ---------------------------------------------------------------- web service
Write-Host "`n== $WEB ==" -ForegroundColor Yellow
Remove-ServiceIfPresent $WEB

& $Nssm install $WEB $NginxExe '-p' $Prefix '-c' (Join-Path $Here 'nginx.conf') | Out-Null
& $Nssm set $WEB AppDirectory   $NginxDir | Out-Null
& $Nssm set $WEB DisplayName    'AI Business Agent - Web (nginx)' | Out-Null
& $Nssm set $WEB Description    'Serves the React bundle and proxies /api and /health to the API (127.0.0.1:80).' | Out-Null
& $Nssm set $WEB DependOnService $API | Out-Null
Set-CommonServiceOptions $WEB 'nginx'
Write-Host "  installed, depends on $API"

Write-Host "`nDone. Start them with:" -ForegroundColor Green
Write-Host "  Start-Service $API; Start-Service $WEB"
Write-Host "Both are SERVICE_AUTO_START, so a restart brings the whole stack back."
