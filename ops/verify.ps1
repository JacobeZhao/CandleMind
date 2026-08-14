[CmdletBinding()]
param(
    [switch]$InstallFrontend
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$verificationRoot = Join-Path $repositoryRoot ".tmp\verify"
$marketRoot = Join-Path $verificationRoot "market-data"
$runtimeRoot = Join-Path $verificationRoot "runtime"
$pytestRoot = Join-Path $verificationRoot ("pytest-" + [guid]::NewGuid().ToString("N"))
$requiredMarketDirectories = @(
    "raw",
    "raw\klines_archive",
    "raw\funding",
    "raw\derivatives_archive",
    "normalized",
    "normalized\ohlcv_parquet",
    "normalized\ema",
    "normalized\ema\releases",
    "normalized\derivatives",
    "normalized\derivatives\releases",
    "processed",
    "processed\features_app",
    "experiments",
    "experiments\backtests",
    "experiments\reports",
    "runtime",
    "runtime\app",
    "manifests"
)

foreach ($relativePath in $requiredMarketDirectories) {
    New-Item -ItemType Directory -Force -Path (Join-Path $marketRoot $relativePath) | Out-Null
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$previousMarketDataDir = $env:MARKET_DATA_DIR
$previousDataDir = $env:DATA_DIR
$env:MARKET_DATA_DIR = $marketRoot
$env:DATA_DIR = $runtimeRoot

Push-Location $repositoryRoot
try {
    python -m pytest backend/tests -q -p no:cacheprovider --basetemp $pytestRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Backend tests failed."
    }

    python -m compileall -q backend/app backend/scripts
    if ($LASTEXITCODE -ne 0) {
        throw "Python compilation failed."
    }

    Push-Location frontend
    try {
        if ($InstallFrontend -or -not (Test-Path "node_modules\.package-lock.json")) {
            $env:npm_config_cache = Join-Path $PWD ".tmp\npm-cache"
            & npm.cmd ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend dependency installation failed."
            }
        }

        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed."
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "Verification passed."
}
finally {
    $env:MARKET_DATA_DIR = $previousMarketDataDir
    $env:DATA_DIR = $previousDataDir
    Pop-Location
}
