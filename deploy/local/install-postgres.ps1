# Installs PostgreSQL 17 as a Windows service on :5432.
#
# Run elevated. The EnterpriseDB installer is driven in unattended mode so no
# wizard appears; winget verifies the download's SHA256 before running it.
#
# The cluster locale is deliberately left at the installer default: the
# application database is created afterwards from template0 with an explicit
# UTF8 encoding, so the cluster's own default never decides how Bangla text is
# stored.

$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'install-postgres.log'
Start-Transcript -Path $log -Force | Out-Null

$superPassword = $args[0]
if (-not $superPassword) { throw 'Superuser password not supplied.' }

$override = "--mode unattended --unattendedmodeui none " +
            "--superpassword `"$superPassword`" " +
            "--serverport 5432 " +
            "--enable-components server,commandlinetools,pgAdmin " +
            "--disable-components stackbuilder"

winget install --id PostgreSQL.PostgreSQL.17 --exact `
    --accept-package-agreements --accept-source-agreements `
    --disable-interactivity `
    --override $override

Write-Output "winget exit code: $LASTEXITCODE"
Stop-Transcript | Out-Null
