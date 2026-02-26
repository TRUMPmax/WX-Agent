param(
    [string]$TunnelName = "weixin-agent",
    [string]$ProjectRoot = ""
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

Set-Location $ProjectRoot

$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
    throw "cloudflared not found at: $cloudflared"
}

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "^cloudflared(\.exe)?$"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Remove-Item -ErrorAction SilentlyContinue "$ProjectRoot\.cf_named_stdout.log", "$ProjectRoot\.cf_named_stderr.log"

Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--no-autoupdate", "run", "--url", "http://127.0.0.1:8000", $TunnelName) `
    -RedirectStandardOutput "$ProjectRoot\.cf_named_stdout.log" `
    -RedirectStandardError "$ProjectRoot\.cf_named_stderr.log" `
    -WorkingDirectory $ProjectRoot | Out-Null

Start-Sleep -Seconds 3

Write-Host "Named tunnel started: $TunnelName"
Write-Host "Logs:"
Write-Host "  .cf_named_stdout.log"
Write-Host "  .cf_named_stderr.log"
