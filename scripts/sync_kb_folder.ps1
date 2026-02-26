param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$ProjectRoot = ""
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $envFile)) {
    throw ".env not found at $envFile"
}

$adminToken = $null
Get-Content $envFile | ForEach-Object {
    if ($_ -match "^ADMIN_TOKEN=(.*)$") {
        $adminToken = $matches[1].Trim()
    }
}

if (-not $adminToken) {
    throw "ADMIN_TOKEN not found in .env"
}

$uri = "$ApiBase/kb/sync"
Write-Host "Trigger sync: $uri"
curl.exe -s -X POST $uri `
    -H "X-Admin-Token: $adminToken"
