[CmdletBinding()]
param(
    [ValidateSet("Up", "Down", "Restart", "Status", "Logs", "Validate")]
    [string]$Action = "Up"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $scriptDir "docker-compose.yml"
$envFile = Join-Path $scriptDir ".env"
$envExample = Join-Path $scriptDir ".env.example"

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker compose --env-file $envFile -f $composeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

function New-UrlSafeSecret {
    param([int]$ByteCount)
    $bytes = New-Object byte[] $ByteCount
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Initialize-EnvFile {
    if (Test-Path -LiteralPath $envFile) {
        return
    }

    if (-not (Test-Path -LiteralPath $envExample)) {
        throw "Missing template: $envExample"
    }

    $jwtSecret = New-UrlSafeSecret -ByteCount 48
    $adminPassword = New-UrlSafeSecret -ByteCount 24
    $content = [IO.File]::ReadAllText($envExample)
    $content = $content.Replace("replace-with-at-least-48-random-characters", $jwtSecret)
    $content = $content.Replace("replace-with-a-strong-password", $adminPassword)
    [IO.File]::WriteAllText($envFile, $content, [Text.UTF8Encoding]::new($false))

    Write-Host "Created $envFile"
    Write-Host "Initial administrator: admin"
    Write-Host "Initial password: $adminPassword"
    Write-Host "Save this password now. Changing .env later does not reset an existing account."
}

function Get-EnvValue {
    param([string]$Name, [string]$Default = "")
    $line = Get-Content -LiteralPath $envFile | Where-Object {
        $_ -match "^\s*$([regex]::Escape($Name))="
    } | Select-Object -Last 1
    if (-not $line) {
        return $Default
    }
    return ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker was not found. Install and start Docker Desktop first."
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose v2 is not available. Update Docker Desktop first."
    }
}

function Show-OllamaHint {
    $usesOllama = (Get-EnvValue "LLM_PROVIDER" "ollama") -eq "ollama" -or
        (Get-EnvValue "EMBEDDING_PROVIDER" "ollama") -eq "ollama"
    if (-not $usesOllama) {
        return
    }

    try {
        $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
        $installed = @($tags.models | ForEach-Object { $_.name })
        $needed = @(
            (Get-EnvValue "OLLAMA_LLM_MODEL" "qwen3:4b"),
            (Get-EnvValue "EMBEDDING_MODEL" "nomic-embed-text-v2-moe")
        ) | Select-Object -Unique
        foreach ($model in $needed) {
            if ($installed -notcontains $model) {
                Write-Warning "Ollama model '$model' was not found. Run: ollama pull $model"
            }
        }
    }
    catch {
        Write-Warning "Ollama is not reachable at localhost:11434. Start Ollama before asking questions or building an index."
    }
}

function Wait-ForApi {
    $port = Get-EnvValue "APP_PORT" "8080"
    $healthUrl = "http://127.0.0.1:$port/api/v1/health"
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
            if ($response.status -eq "ok") {
                Write-Host "RAG service is available at http://localhost:$port"
                return
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    Invoke-Compose logs --tail 100 app
    throw "The API did not become healthy within 120 seconds."
}

Assert-Docker
Initialize-EnvFile

switch ($Action) {
    "Up" {
        Show-OllamaHint
        Invoke-Compose config --quiet
        Invoke-Compose up -d --build
        Wait-ForApi
    }
    "Down" {
        Invoke-Compose down
        Write-Host "Containers were removed; the named data volume was preserved."
    }
    "Restart" {
        Show-OllamaHint
        Invoke-Compose config --quiet
        Invoke-Compose up -d --build --force-recreate
        Wait-ForApi
    }
    "Status" {
        Invoke-Compose ps
    }
    "Logs" {
        Invoke-Compose logs -f --tail 200 app
    }
    "Validate" {
        Invoke-Compose config --quiet
        Wait-ForApi
        $port = Get-EnvValue "APP_PORT" "8080"
        Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/v1/ready" -TimeoutSec 30 |
            ConvertTo-Json -Depth 8
    }
}
