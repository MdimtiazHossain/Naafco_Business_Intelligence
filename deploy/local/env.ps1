# Shared environment loader for the localhost production stack. Dot-source it.
#
# Why this exists rather than a second .env: app/config.py reads only the
# repo-root .env, and it reads it with os.environ.setdefault -- a real
# environment variable therefore wins. Exporting the production values here
# leaves the SQLite dev configuration in .env untouched and still in force for
# start-backend.bat on :8010, so both instances run side by side.

$ErrorActionPreference = 'Stop'

$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$BackendDir = Join-Path $RepoRoot 'backend'
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$EnvFile    = Join-Path $PSScriptRoot '.env.production'

function Import-ProductionEnv {
    if (-not (Test-Path $EnvFile)) {
        throw "Missing $EnvFile. See deploy/local/README.md."
    }
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) {
            continue
        }
        $parts = $trimmed.Split('=', 2)
        $key   = $parts[0].Trim()
        # Only surrounding quotes are stripped, matching the loader in
        # config.py exactly -- a backslash in a Windows path stays literal.
        $value = $parts[1].Trim().Trim('"').Trim("'")
        Set-Item -LiteralPath "Env:$key" -Value $value
    }
}
