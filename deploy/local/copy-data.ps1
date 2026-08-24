# Copies the SQLite warehouse into the PostgreSQL one, row for row.
#
# Wraps scripts/migrate_sqlite_to_postgres.py, which does the work and owns every
# safety rule: it refuses unless both databases are at the same Alembic head and
# every target table is empty, copies in sorted_tables order inside ONE
# transaction, advances every identity sequence past the largest copied key, and
# re-counts both sides — rolling the whole copy back on any disagreement.
#
# data/dev.db is opened read-only by SQLAlchemy for reading only; it is never
# written, so it remains a complete fallback whatever happens here.
#
#   .\copy-data.ps1 -DryRun    report what would be copied, write nothing
#   .\copy-data.ps1            do it
#
# With this environment loaded, DATABASE_URL is PostgreSQL, so the script's own
# default source resolution falls through to data/dev.db and the target is the
# local database on :5432 — the direct connection, not a pooler, so its
# transaction-pooler refusal has nothing to object to.

param([switch]$DryRun)

. (Join-Path $PSScriptRoot 'env.ps1')
Import-ProductionEnv

$script = Join-Path $RepoRoot 'scripts\migrate_sqlite_to_postgres.py'

Push-Location $RepoRoot
try {
    if ($DryRun) {
        & $VenvPython $script --dry-run
    } else {
        & $VenvPython $script
    }
    if ($LASTEXITCODE -ne 0) { throw "copy failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
