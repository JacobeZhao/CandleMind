[CmdletBinding()]
param(
    [switch]$InstallFrontend
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
$verificationLeaf = "candlemind-verify-" + [guid]::NewGuid().ToString("N")
$verificationRoot = Join-Path $tempParent $verificationLeaf
$marketRoot = Join-Path $verificationRoot "market-data"
$runtimeRoot = Join-Path $verificationRoot "runtime"
$pytestRoot = Join-Path $verificationRoot "pytest"
$pycacheRoot = Join-Path $verificationRoot "pycache"
$npmCacheRoot = Join-Path $verificationRoot "npm-cache"
$frontendBuildRoot = Join-Path $verificationRoot "frontend-build"
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

$savedEnvironment = @{}
foreach ($name in @("MARKET_DATA_DIR", "DATA_DIR", "PYTHONPYCACHEPREFIX", "npm_config_cache")) {
    $savedEnvironment[$name] = @{
        Exists = Test-Path -LiteralPath ("Env:" + $name)
        Value = [Environment]::GetEnvironmentVariable($name, "Process")
    }
}

$createdVerificationRoot = $false
$repositoryLocationPushed = $false
$frontendLocationPushed = $false
try {
    New-Item -ItemType Directory -Path $verificationRoot | Out-Null
    $createdVerificationRoot = $true
    foreach ($relativePath in $requiredMarketDirectories) {
        New-Item -ItemType Directory -Force -Path (Join-Path $marketRoot $relativePath) | Out-Null
    }
    New-Item -ItemType Directory -Force -Path $runtimeRoot, $pytestRoot, $pycacheRoot, $npmCacheRoot, $frontendBuildRoot | Out-Null

    $env:MARKET_DATA_DIR = $marketRoot
    $env:DATA_DIR = $runtimeRoot
    $env:PYTHONPYCACHEPREFIX = $pycacheRoot
    $env:npm_config_cache = $npmCacheRoot

    Push-Location $repositoryRoot
    $repositoryLocationPushed = $true
    python -m pytest backend/tests -q -p no:cacheprovider --basetemp $pytestRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Backend tests failed."
    }

    python -m compileall -q backend/app backend/scripts
    if ($LASTEXITCODE -ne 0) {
        throw "Python compilation failed."
    }

    Push-Location frontend
    $frontendLocationPushed = $true
    try {
        if ($InstallFrontend -or -not (Test-Path "node_modules\.package-lock.json")) {
            & npm.cmd ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend dependency installation failed."
            }
        }

        & npm.cmd test
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend tests failed."
        }

        & npm.cmd run build -- --outDir $frontendBuildRoot --emptyOutDir
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed."
        }
    }
    finally {
        if ($frontendLocationPushed) {
            try {
                Pop-Location -ErrorAction Stop
                $frontendLocationPushed = $false
            }
            catch {
                Write-Warning "Could not restore the frontend location: $($_.Exception.Message)" -WarningAction Continue
            }
        }
    }

    Write-Host "Verification passed."
}
finally {
    if ($frontendLocationPushed) {
        try {
            Pop-Location -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not restore the frontend location: $($_.Exception.Message)" -WarningAction Continue
        }
    }
    if ($repositoryLocationPushed) {
        try {
            Pop-Location -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not restore the repository location: $($_.Exception.Message)" -WarningAction Continue
        }
    }

    foreach ($name in $savedEnvironment.Keys) {
        try {
            if ($savedEnvironment[$name].Exists) {
                [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name].Value, "Process")
            }
            else {
                Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
            }
        }
        catch {
            Write-Warning "Could not restore environment variable '$name': $($_.Exception.Message)" -WarningAction Continue
        }
    }

    if ($createdVerificationRoot) {
        try {
            $item = Get-Item -LiteralPath $verificationRoot -Force
            $resolvedRoot = [IO.Path]::GetFullPath($item.FullName).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
            $resolvedParent = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedRoot)).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
            if ($resolvedParent -ne $tempParent -or (Split-Path -Leaf $resolvedRoot) -cne $verificationLeaf) {
                throw "Refusing to clean an unexpected verification path: $resolvedRoot"
            }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to clean a verification root that is a reparse point: $resolvedRoot"
            }
            Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
        }
        catch {
            Write-Warning "Could not clean verification directory '$verificationRoot': $($_.Exception.Message)" -WarningAction Continue
        }
    }
}
