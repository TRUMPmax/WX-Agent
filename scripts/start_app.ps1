param(
    [string]$ProjectRoot = ""
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

Set-Location $ProjectRoot
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pythonExe = if ($pythonCmd) { $pythonCmd.Source } else { "" }

function Start-OllamaIfNeeded {
    $running = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "^ollama(\\.exe)?$" -and $_.CommandLine -and $_.CommandLine -like "*serve*"
    }
    if (-not $running) {
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WorkingDirectory $ProjectRoot | Out-Null
    }
}

function Start-UvicornIfNeeded {
    $running = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "^python(\\.exe)?$" -and $_.CommandLine -and $_.CommandLine -like "*uvicorn app.main:app*"
    }
    if (-not $running) {
        if (-not $pythonExe) {
            throw "python not found in PATH"
        }
        Start-Process -FilePath $pythonExe `
            -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --access-log" `
            -RedirectStandardOutput "$ProjectRoot\.uvicorn_stdout.log" `
            -RedirectStandardError "$ProjectRoot\.uvicorn_stderr.log" `
            -WorkingDirectory $ProjectRoot | Out-Null
    }
}

New-Item -ItemType Directory -Force -Path "$ProjectRoot\data" | Out-Null

$kbSourceDir = Join-Path $ProjectRoot "kb_source"
$envPath = Join-Path $ProjectRoot ".env"
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        if ($_ -match "^KB_SOURCE_DIR=(.*)$") {
            $configured = $matches[1].Trim()
            if ($configured) {
                if ([System.IO.Path]::IsPathRooted($configured)) {
                    $kbSourceDir = $configured
                } else {
                    $kbSourceDir = Join-Path $ProjectRoot $configured
                }
            }
        }
    }
}
New-Item -ItemType Directory -Force -Path $kbSourceDir | Out-Null

Start-OllamaIfNeeded
Start-UvicornIfNeeded

Start-Sleep -Seconds 1
Write-Host "App health:"
$ok = $false
for ($i = 0; $i -lt 15; $i++) {
    try {
        $h = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 5
        $h | ConvertTo-Json
        Write-Host "KB source directory: $kbSourceDir"
        $ok = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}

if (-not $ok) {
    Write-Host "Health check failed after retries. See .uvicorn_stderr.log"
}
