# Makes the deployment reachable from other PCs on the LAN. MUST run elevated.
#
#   powershell -ExecutionPolicy Bypass -File enable-lan-access.ps1
#
# Three changes, and only nginx's listener is widened:
#
#   1. Restart AIBusinessAgentWeb so nginx picks up `listen 80` (was
#      `listen 127.0.0.1:80`). uvicorn stays on 127.0.0.1:8000 and PostgreSQL on
#      127.0.0.1:5432 — neither is touched here or anywhere else.
#   2. Reclassify the Wi-Fi adapter from Public to Private. Without this the
#      firewall rule below admits nothing, because a Private-profile rule is
#      inert while the active network is classified Public.
#   3. Scope the existing "AI Business Agent HTTP" rule to the LAN subnet
#      instead of Any. This narrows a rule that already existed; it opens
#      nothing new.
#
# The pre-existing "WWW" rule (TCP/80, Public, remote=Any) is deliberately NOT
# touched — it was not created by this deployment and may belong to other
# software. Once Wi-Fi is Private it no longer governs LAN access here.
#
# To reverse: set the listener back to `listen 127.0.0.1:80` in nginx.conf and
# restart the service. The network category reverts with
#   Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Public

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
          ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this elevated: changing a network profile and a firewall rule needs Administrator.'
}

$Iface  = 'Wi-Fi'
$Subnet = '172.16.1.0/24'
$Rule   = 'AI Business Agent HTTP'

Write-Host '== 1. network profile ==' -ForegroundColor Yellow
$before = (Get-NetConnectionProfile -InterfaceAlias $Iface).NetworkCategory
Write-Host "   $Iface was: $before"
if ($before -ne 'Private') {
    Set-NetConnectionProfile -InterfaceAlias $Iface -NetworkCategory Private
    Start-Sleep -Seconds 2
}
Write-Host "   $Iface now: $((Get-NetConnectionProfile -InterfaceAlias $Iface).NetworkCategory)"

Write-Host "`n== 2. firewall rule '$Rule' ==" -ForegroundColor Yellow
$r = Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue
if (-not $r) {
    Write-Host '   not present - creating it'
    New-NetFirewallRule -DisplayName $Rule -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort 80 -Profile Private -RemoteAddress $Subnet `
        -Description 'AI Business Reporting Agent - nginx on TCP/80, LAN only.' | Out-Null
} else {
    Write-Host '   present - scoping RemoteAddress to the LAN subnet'
    Set-NetFirewallRule -DisplayName $Rule -Enabled True -Action Allow `
        -Profile Private -RemoteAddress $Subnet
}
$r  = Get-NetFirewallRule -DisplayName $Rule
$pf = $r | Get-NetFirewallPortFilter
$af = $r | Get-NetFirewallAddressFilter
Write-Host ("   enabled={0} action={1} profile={2} {3}/{4} remote={5}" -f `
    $r.Enabled, $r.Action, $r.Profile, $pf.Protocol, ($pf.LocalPort -join ','), ($af.RemoteAddress -join ','))

Write-Host "`n== 3. restart nginx so it binds the new listener ==" -ForegroundColor Yellow
Restart-Service AIBusinessAgentWeb -Force
Start-Sleep -Seconds 4
Write-Host "   AIBusinessAgentWeb: $((Get-Service AIBusinessAgentWeb).Status)"

Write-Host "`n== verification ==" -ForegroundColor Yellow
Get-NetTCPConnection -State Listen -LocalPort 80,8000,5432 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort | Sort-Object LocalPort,LocalAddress | Format-Table -AutoSize
