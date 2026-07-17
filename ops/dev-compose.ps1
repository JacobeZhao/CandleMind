[CmdletBinding()]
param(
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repositoryRoot

try {
    & docker compose config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose configuration is invalid."
    }

    $composeArgs = @("compose", "up", "-d")
    if (-not $NoBuild) {
        $composeArgs += "--build"
    }

    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to start."
    }

    $ready = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri "http://localhost:8000/api/ping" -TimeoutSec 2
            if ($response.ok -eq $true) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }

    if (-not $ready) {
        & docker compose ps
        throw "Backend did not become ready within 30 seconds."
    }

    Write-Host "CandleMind is ready at http://localhost:3000"
    Write-Host "API documentation: http://localhost:8000/docs"
    Write-Host "Logs: docker compose logs -f"
}
finally {
    Pop-Location
}
