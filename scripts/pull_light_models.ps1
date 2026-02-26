param(
    [string[]]$Models = @("qwen3:4b", "deepseek-r1:7b", "qwen3-embedding:4b")
)

foreach ($model in $Models) {
    Write-Host "Pulling model: $model"
    ollama pull $model
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to pull model: $model"
    }
}

Write-Host ""
Write-Host "Installed models:"
ollama list
