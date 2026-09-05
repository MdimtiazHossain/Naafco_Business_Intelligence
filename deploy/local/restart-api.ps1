# Restart the API service, and prove it actually restarted.
#
# `Restart-Service AIBusinessAgentAPI` is the documented way and is usually all
# anybody needs. This exists because when it *doesn't* work it does so quietly:
# the service wrapper (nssm) keeps its own process id across a restart of the
# thing it wraps, `Get-Service` says Running either way, and a non-elevated
# shell prints one "Access is denied" line that scrolls past in a busy terminal.
# The result is a deployment still executing the code it started with while
# everyone believes it was reloaded — which is exactly how a fix came to be
# tested three times against the version that did not contain it.
#
# So this script asserts elevation before touching anything, records what was
# running, stops and starts, and then checks three independent signals: the
# python process id changed, port 8000 is listening again, and nssm rotated the
# stdout log. It says PASS or FAIL in those words.
#
# Run it from an elevated PowerShell:
#     .\deploy\local\restart-api.ps1

$ErrorActionPreference = 'Stop'

$Service = 'AIBusinessAgentAPI'
$Port    = 8000
$LogFile = Join-Path $PSScriptRoot 'run\logs\api-stdout.log'

function Get-ApiProcessId {
    # The listening socket, not the service's own process: the service id
    # belongs to nssm, which survives restarting the application under it.
    (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess)
}

# -- 1. Elevation, before anything else ------------------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ''
    Write-Host 'FAIL - this shell is not elevated, so the service cannot be stopped.' -ForegroundColor Red
    Write-Host '       Close it, right-click PowerShell, choose "Run as administrator",'
    Write-Host '       and run this script again. Nothing has been changed.'
    Write-Host ''
    exit 1
}

# -- 2. Refuse while an import is running -----------------------------------
# The service runs uvicorn without --reload precisely so a file watcher cannot
# kill an import mid-flight; restarting by hand would do the same damage, and an
# import is one transaction, so what it had written is rolled back.
$importing = $null
try {
    . (Join-Path $PSScriptRoot 'env.ps1')
    Import-ProductionEnv
    if ($env:DATABASE_URL -match '://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)$') {
        $user = $Matches[1]; $pass = $Matches[2]
        $dbHost = $Matches[3]; $dbPort = $Matches[4]; $dbName = $Matches[5]
        $psql = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' -ErrorAction SilentlyContinue |
                Select-Object -Last 1
        if ($psql) {
            $env:PGPASSWORD = $pass
            $sql = "SELECT count(*) FROM upload_batches WHERE status IN ('QUEUED','VALIDATING','IMPORTING');"
            $importing = (& $psql.FullName -U $user -h $dbHost -p $dbPort -d $dbName -t -A -c $sql 2>$null)
            $env:PGPASSWORD = ''
        }
    }
} catch {
    Write-Host "note: could not check for running imports ($($_.Exception.Message))" -ForegroundColor Yellow
}
if ($importing -and [int]$importing -gt 0) {
    Write-Host ''
    Write-Host "FAIL - $importing import(s) are still running. Restarting now would" -ForegroundColor Red
    Write-Host '       roll them back. Wait for the Upload Centre to finish, then re-run.'
    Write-Host ''
    exit 1
}

# -- 3. Restart --------------------------------------------------------------
# Everything from here is transcribed, because the useful diagnosis is what the
# service control calls *said*, and that scrolls away in a terminal.
$LogDir     = Join-Path $PSScriptRoot 'run\logs'
$Transcript = Join-Path $LogDir ("restart-api-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
try { Start-Transcript -Path $Transcript -Force | Out-Null } catch { }

# nssm renames api-stdout.log to api-stdout-<timestamp>.log each time it starts
# the application, so a new rotated file is its restart signature. The live
# file's own timestamp proves nothing: request logging appends to it every few
# seconds whether or not anything restarted, which is what made an earlier
# version of this script report PASS over a service that had not moved.
$rotatedBefore = @(Get-ChildItem (Join-Path $LogDir 'api-stdout-*.log') -ErrorAction SilentlyContinue).Count
$before = Get-ApiProcessId
Write-Host "API process before : $(if ($before) { $before } else { '(nothing listening)' })"

Stop-Service $Service -Force
$deadline = (Get-Date).AddSeconds(30)
while ((Get-ApiProcessId) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }

# The stop does not always take the server with it, and when it does not the
# failure is silent and total. nssm launches `.venv\Scripts\python.exe`, which
# runs the real interpreter as a child, and that child is the process holding
# port 8000. `AppKillProcessTree` is set, but nssm allows only 1500 ms per stop
# method, so the parent dies first, the server is reparented, and the tree walk
# no longer finds it. It then keeps the socket: the service restarts, the new
# server cannot bind, it exits, and the ORPHAN goes on answering requests with
# whatever code it started with.
#
# That is not a hypothetical. It cost a deployment four restarts that all
# appeared to succeed — `Get-Service` said Running throughout — while the API
# served code from hours earlier. So the port is checked, and an orphan holding
# it is ended by hand rather than left to defeat the restart.
$orphan = Get-ApiProcessId
if ($orphan) {
    Write-Host "Port $Port still held by PID $orphan after the stop - orphaned; ending it." `
        -ForegroundColor Yellow
    Stop-Process -Id $orphan -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-ApiProcessId) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}
$stopped = -not (Get-ApiProcessId)
Write-Host "Port $Port released  : $stopped"

Start-Service $Service
$deadline = (Get-Date).AddSeconds(45)
while (-not (Get-ApiProcessId) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
$after = Get-ApiProcessId
Write-Host "API process after  : $(if ($after) { $after } else { '(nothing listening)' })"

$rotatedAfter = @(Get-ChildItem (Join-Path $LogDir 'api-stdout-*.log') -ErrorAction SilentlyContinue).Count
Write-Host "Log rotations      : $rotatedBefore -> $rotatedAfter"

# -- 4. Verify. Anything short of proof of a new process is a failure -------
$problems = @()
if (-not $after) {
    $problems += "nothing is listening on port $Port - the API did not come back up"
}
if (-not $stopped) {
    $problems += 'the old process never released the port - the stop was refused or timed out'
}
if ($after -and $before -and $after -eq $before) {
    $problems += "the process id did not change ($before) - the application was never restarted"
}
if (-not $before) {
    # Cannot compare against a process that was not found. Say so rather than
    # passing by default: an unprovable restart is the thing this script exists
    # to catch.
    $problems += "nothing was listening on port $Port beforehand, so the restart could not be verified by process id"
}
if ($rotatedAfter -le $rotatedBefore) {
    $problems += 'nssm did not rotate the service log - it did not start a new process'
}

Write-Host ''
if ($problems) {
    Write-Host 'FAIL - the API is NOT running the current code:' -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "       - $_" -ForegroundColor Red }
    Write-Host ''
    Write-Host "Full transcript: $Transcript"
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

Write-Host "PASS - the API restarted ($before -> $after) and is serving the current code." -ForegroundColor Green
Write-Host '       A backend change needs this; a frontend change needs'
Write-Host '       "cd frontend; npm run build" instead, and no restart.'
Write-Host ''
Write-Host "Full transcript: $Transcript"
try { Stop-Transcript | Out-Null } catch { }
