param(
    [string]$ProjectRoot = "",
    [string]$TunnelName = "weixin-agent",
    [string]$PublicHost = "wxbot.haoyusun.me"
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

Set-Location $ProjectRoot

Write-Host "== Step 1/2: Start app =="
powershell -ExecutionPolicy Bypass -File "$ProjectRoot\scripts\start_app.ps1" -ProjectRoot $ProjectRoot

Write-Host ""
Write-Host "== Step 2/2: Start named tunnel ($TunnelName) =="
powershell -ExecutionPolicy Bypass -File "$ProjectRoot\scripts\start_named_tunnel.ps1" -ProjectRoot $ProjectRoot -TunnelName $TunnelName

Write-Host ""
Write-Host "== Verify =="
try {
    $local = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 8
    Write-Host "Local health: " ($local | ConvertTo-Json -Compress)
} catch {
    Write-Host "Local health failed."
}

try {
    $public = Invoke-RestMethod -Method Get -Uri "https://$PublicHost/healthz" -TimeoutSec 12
    Write-Host "Public health: " ($public | ConvertTo-Json -Compress)
    Write-Host "WeChat callback URL: https://$PublicHost/wechat"
} catch {
    Write-Host "Public health failed. Check tunnel logs: .cf_named_stdout.log / .cf_named_stderr.log"
}
