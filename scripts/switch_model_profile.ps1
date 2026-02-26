param(
    [ValidateSet("qwen3", "deepseek")]
    [string]$Profile = "qwen3",
    [string]$ProjectRoot = ""
)

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

Set-Location $ProjectRoot

$envPath = Join-Path $ProjectRoot ".env"
$envExamplePath = Join-Path $ProjectRoot ".env.example"

if (-not (Test-Path $envPath)) {
    if (Test-Path $envExamplePath) {
        Copy-Item $envExamplePath $envPath -Force
    } else {
        New-Item -ItemType File -Path $envPath | Out-Null
    }
}

function Set-Or-AddEnvLine {
    param(
        [string[]]$Lines,
        [string]$Key,
        [string]$Value
    )
    $pattern = "^\s*$([regex]::Escape($Key))="
    $found = $false
    $result = @()
    foreach ($line in $Lines) {
        if ($line -match $pattern) {
            $result += "$Key=$Value"
            $found = $true
        } else {
            $result += $line
        }
    }
    if (-not $found) {
        $result += "$Key=$Value"
    }
    return ,$result
}

$chatModel = "qwen3:4b"
$embedModel = "qwen3-embedding:4b"

if ($Profile -eq "deepseek") {
    $chatModel = "deepseek-r1:7b"
}

$lines = @()
if (Test-Path $envPath) {
    $lines = Get-Content $envPath
}

$lines = Set-Or-AddEnvLine -Lines $lines -Key "OLLAMA_CHAT_MODEL" -Value $chatModel
$lines = Set-Or-AddEnvLine -Lines $lines -Key "OLLAMA_EMBED_MODEL" -Value $embedModel

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($envPath, $lines, $utf8NoBom)

Write-Host "Switched profile: $Profile"
Write-Host "OLLAMA_CHAT_MODEL=$chatModel"
Write-Host "OLLAMA_EMBED_MODEL=$embedModel"
Write-Host "Restart app to apply model changes."
