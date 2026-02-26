param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$ProjectRoot = ""
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

if (-not (Test-Path $FilePath)) {
    throw "File not found: $FilePath"
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

$uri = "$ApiBase/kb/upload"
Write-Host "Uploading: $FilePath"
curl.exe -s -X POST $uri `
    -H "X-Admin-Token: $adminToken" `
    -F "file=@$FilePath"
