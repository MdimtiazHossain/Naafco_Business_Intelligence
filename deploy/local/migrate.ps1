# Builds the schema on PostgreSQL: alembic 0001 -> head, in one place.
#
# Alembic must run from backend/ (alembic.ini lives there and its
# script_location is relative), and the URL comes from app.config rather than
# alembic.ini -- which is why this loads the environment first and passes no -x
# flag. This creates tables, indexes and the eight reporting views; it copies no
# data. Re-running it is a no-op once the database is at head.

. (Join-Path $PSScriptRoot 'env.ps1')
Import-ProductionEnv

$alembic = Join-Path $RepoRoot '.venv\Scripts\alembic.exe'

Write-Host "Target: $($env:DATABASE_URL -replace ':[^:@/]+@', ':****@')" -ForegroundColor Cyan

Push-Location $BackendDir
try {
    & $alembic current
    & $alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed with exit code $LASTEXITCODE" }
    & $alembic current
}
finally {
    Pop-Location
}
