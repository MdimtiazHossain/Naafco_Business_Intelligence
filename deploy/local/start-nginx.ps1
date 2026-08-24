# Starts nginx for the localhost deployment, or tests / reloads / stops it.
#
#   .\start-nginx.ps1 -Test     validate the config and exit (nginx -t)
#   .\start-nginx.ps1           start it
#   .\start-nginx.ps1 -Reload   re-read the config without dropping connections
#   .\start-nginx.ps1 -Stop     stop it
#
# nginx runs from deploy/local/nginx/ — a copy of the winget distribution rather
# than the winget path itself, because that path carries the version number
# (…\nginx-1.31.4\) and a Windows service pointing at it would break the next
# time nginx is upgraded. To take an upgrade: winget upgrade nginxinc.nginx, then
# re-copy the tree over this one.
#
# -p points the prefix at run/, so logs and temp files land there and never
# inside the distribution copy.

param(
    [switch]$Test,
    [switch]$Reload,
    [switch]$Stop
)

. (Join-Path $PSScriptRoot 'env.ps1')

$NginxExe = Join-Path $PSScriptRoot 'nginx\nginx.exe'
$Prefix   = Join-Path $PSScriptRoot 'run'
$Config   = Join-Path $PSScriptRoot 'nginx.conf'

if (-not (Test-Path $NginxExe)) {
    throw "nginx not found at $NginxExe. See deploy/local/README.md."
}
New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'temp') | Out-Null

# nginx resolves -p relative to its own working directory, so run from the
# distribution directory and pass both as absolute paths.
$common = @('-p', "$Prefix", '-c', "$Config")

if ($Test) {
    & $NginxExe @common -t
    exit $LASTEXITCODE
}
if ($Reload) {
    & $NginxExe @common -s reload
    exit $LASTEXITCODE
}
if ($Stop) {
    & $NginxExe @common -s stop
    exit $LASTEXITCODE
}

# Validate before binding: a config error after the old process has already been
# told to stop leaves nothing serving port 80.
& $NginxExe @common -t
if ($LASTEXITCODE -ne 0) { throw 'nginx configuration test failed; not starting.' }

& $NginxExe @common
Write-Host "nginx started. http://localhost" -ForegroundColor Green
