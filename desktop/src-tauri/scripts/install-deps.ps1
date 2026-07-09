$ErrorActionPreference = 'Continue'

# install-deps.ps1 - Install requirements-slim.txt deps into embedded python's site-packages

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcTauriDir = Split-Path -Parent $ScriptDir
$PyembedDir = Join-Path $SrcTauriDir "pyembed"
$PythonDir = Join-Path $PyembedDir "python"
$PyExe = Join-Path $PythonDir "python.exe"
$SitePackagesDir = Join-Path $PythonDir "Lib\site-packages"

# requirements-slim.txt is at hermes-lite/ root (3 levels up from scripts/)
$HermesRoot = Split-Path -Parent (Split-Path -Parent $SrcTauriDir)
$RequirementsFile = Join-Path $HermesRoot "requirements-slim.txt"

Write-Host "=== install-deps.ps1 ==="
Write-Host "Python exe: $PyExe"
Write-Host "Requirements: $RequirementsFile"
Write-Host ""

# Verify python.exe exists
if (-not (Test-Path $PyExe)) {
    Write-Host "[error] python.exe not found, run fetch-python.ps1 first"
    exit 1
}

# Verify requirements-slim.txt exists
if (-not (Test-Path $RequirementsFile)) {
    Write-Host "[error] not found: $RequirementsFile"
    exit 1
}

# 1. ensurepip
Write-Host "[step 1/4] ensurepip ..."
& $PyExe -m ensurepip --upgrade 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "[error] ensurepip failed"; exit 1 }

# 2. Upgrade pip
Write-Host ""
Write-Host "[step 2/4] upgrading pip ..."
& $PyExe -m pip install --upgrade pip 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "[error] pip upgrade failed"; exit 1 }

# 3. Install deps
Write-Host ""
Write-Host "[step 3/4] installing requirements-slim.txt ..."
& $PyExe -m pip install -r $RequirementsFile 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "[error] deps install failed"; exit 1 }

# 4. Clean __pycache__ and tests (reduce size)
Write-Host ""
Write-Host "[step 4/4] cleaning __pycache__ and tests ..."
$cleanedCount = 0
Get-ChildItem $SitePackagesDir -Recurse -Directory -ErrorAction SilentlyContinue | 
    Where-Object { $_.Name -in @("__pycache__", "tests", "test") } |
    ForEach-Object {
        Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        $cleanedCount++
    }
Get-ChildItem $SitePackagesDir -Recurse -Filter "*.pyc" -ErrorAction SilentlyContinue | 
    ForEach-Object {
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
        $cleanedCount++
    }
Write-Host "[ok] cleaned $cleanedCount items"

# Verify key deps can be imported
Write-Host ""
Write-Host "[verify] verifying imports ..."
& $PyExe -c "import chromadb, fastapi, uvicorn, anthropic, openai, numpy, tiktoken, pydantic; print('deps ok')" 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "[error] import verification failed"
    exit 1
}

# Print final size
$finalSize = (Get-ChildItem $PythonDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host "[done] deps installed to $SitePackagesDir"
Write-Host "       python dir total size: $([math]::Round($finalSize / 1MB, 2)) MB"
