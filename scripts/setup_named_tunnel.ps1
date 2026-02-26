param(
    [Parameter(Mandatory = $true)]
    [string]$Hostname,
    [string]$TunnelName = "weixin-agent"
)

$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
    throw "cloudflared not found at: $cloudflared"
}

Write-Host "Step 1/3: cloudflared tunnel login"
& $cloudflared tunnel login
if ($LASTEXITCODE -ne 0) { throw "tunnel login failed" }

Write-Host "Step 2/3: cloudflared tunnel create $TunnelName"
& $cloudflared tunnel create $TunnelName
if ($LASTEXITCODE -ne 0) { throw "tunnel create failed" }

Write-Host "Step 3/3: cloudflared tunnel route dns $TunnelName $Hostname"
& $cloudflared tunnel route dns $TunnelName $Hostname
if ($LASTEXITCODE -ne 0) { throw "tunnel route dns failed" }

Write-Host ""
Write-Host "Done."
Write-Host "Use this callback URL in WeChat:"
Write-Host "https://$Hostname/wechat"
Write-Host ""
Write-Host "Run named tunnel with:"
Write-Host "powershell -ExecutionPolicy Bypass -File .\\scripts\\start_named_tunnel.ps1 -TunnelName $TunnelName"
