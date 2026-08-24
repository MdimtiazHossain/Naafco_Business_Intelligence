# Starts the API for the localhost deployment: uvicorn on 127.0.0.1:8000.
#
# 127.0.0.1 rather than 0.0.0.0: nginx is the only thing that talks to this
# process, and it does so over the loopback. Binding the wildcard would publish
# the API on every interface of this machine, past nginx and past the same-origin
# assumption the CORS list is written for.
#
# ONE worker, deliberately, and not a number to tune later. Two pieces of state
# live in this process rather than in the database:
#
#   * utils/progress.py holds live import progress in process memory, keyed by
#     the batch uuid, precisely because an import is one transaction and a row
#     written inside it is invisible until commit. With a second worker, the
#     progress poll would land on whichever process the OS chose and report
#     "unknown job" for an import that is running perfectly well next door.
#   * upload/jobs.py owns a bounded thread pool. A second worker would mean a
#     second pool, so IMPORT_WORKER_COUNT would silently stop being the number of
#     concurrent imports.
#
# No --reload either: it runs a file watcher and restarts the process on any
# edit, which would kill a running import.

. (Join-Path $PSScriptRoot 'env.ps1')
Import-ProductionEnv

Write-Host "APP_ENV        : $env:APP_ENV"
Write-Host "Database       : $($env:DATABASE_URL -replace ':[^:@/]+@', ':****@')"
Write-Host "Header auth    : $env:ALLOW_HEADER_AUTH"
Write-Host "CORS origins   : $env:CORS_ORIGINS"
Write-Host "Upload staging : $((Split-Path $env:TRANSACTIONS_DIR -Parent))\uploads"
Write-Host ''

Push-Location $BackendDir
try {
    & $VenvPython -m uvicorn app.main:app `
        --host 127.0.0.1 `
        --port 8000 `
        --workers 1 `
        --log-level info `
        --proxy-headers `
        --forwarded-allow-ips 127.0.0.1
}
finally {
    Pop-Location
}
