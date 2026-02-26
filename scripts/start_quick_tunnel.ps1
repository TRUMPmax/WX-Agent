param(
    [string]$ProjectRoot = "",
    [string]$LocalUrl = "http://127.0.0.1:8000"
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

Remove-Item -ErrorAction SilentlyContinue "$ProjectRoot\.cf_stdout.log", "$ProjectRoot\.cf_stderr.log"

Start-Process -FilePath $cloudflared `
    -ArgumentList "tunnel --url $LocalUrl --no-autoupdate" `
    -RedirectStandardOutput "$ProjectRoot\.cf_stdout.log" `
    -RedirectStandardError "$ProjectRoot\.cf_stderr.log" `
    -WorkingDirectory $ProjectRoot | Out-Null

Start-Sleep -Seconds 4

$domain = ""
if (Test-Path "$ProjectRoot\.cf_stderr.log") {
    $matches = Select-String -Path "$ProjectRoot\.cf_stderr.log" -Pattern "https://[a-zA-Z0-9\.-]+\.trycloudflare\.com" -AllMatches
    if ($matches) {
        $all = @()
        foreach ($m in $matches) {
            $all += $m.Matches.Value
        }
        $domain = $all[-1]
    }
}

if (-not $domain) {
    Write-Host "Tunnel started, but URL not parsed yet. Check .cf_stderr.log"
    exit 1
}

Write-Host "Public URL: $domain"
Write-Host "WeChat callback URL: $domain/wechat"
