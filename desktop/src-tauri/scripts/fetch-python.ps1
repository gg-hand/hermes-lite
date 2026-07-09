$ErrorActionPreference = 'Stop'

# fetch-python.ps1 - Download python-build-standalone OR copy system Python to pyembed/python/
# Tries multiple mirrors; if all fail, copies the local system Python installation.

$PyTag = "20260623"
$PyVer = "3.10.20"
$TargetTriple = "x86_64-pc-windows-msvc"
$FileName = "cpython-$PyVer+$PyTag-$TargetTriple-install_only_stripped.tar.gz"
$GithubUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PyTag/$FileName"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcTauriDir = Split-Path -Parent $ScriptDir
$PyembedDir = Join-Path $SrcTauriDir "pyembed"
$PythonDir = Join-Path $PyembedDir "python"

$TempZip = Join-Path $env:TEMP "python-build-standalone.tar.gz"
$TempExtract = Join-Path $env:TEMP "pbs-extract"

Write-Host "=== fetch-python.ps1 ==="
Write-Host "Python version: $PyVer (tag $PyTag)"
Write-Host "Target dir: $PythonDir"
Write-Host ""

# Skip if already exists
if (Test-Path (Join-Path $PythonDir "python.exe")) {
    Write-Host "[skip] python.exe already exists at $PythonDir, delete the dir first to re-download"
    exit 0
}

# Clean old dirs
if (Test-Path $PythonDir) {
    Remove-Item $PythonDir -Recurse -Force
}
if (Test-Path $TempExtract) {
    Remove-Item $TempExtract -Recurse -Force
}
New-Item -ItemType Directory -Path $PyembedDir -Force | Out-Null

# Try multiple mirrors
$mirrors = @(
    @{ Name = "ghproxy.com"; Prefix = "https://ghproxy.com/" },
    @{ Name = "ghfast.top"; Prefix = "https://ghfast.top/" },
    @{ Name = "mirror.ghproxy.com"; Prefix = "https://mirror.ghproxy.com/" },
    @{ Name = "direct"; Prefix = "" }
)

$downloaded = $false
foreach ($m in $mirrors) {
    $url = "$($m.Prefix)$GithubUrl"
    Write-Host "[try:$($m.Name)] $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $TempZip -UseBasicParsing -TimeoutSec 300
        $zipSize = (Get-Item $TempZip).Length
        if ($zipSize -gt 1000000) {
            Write-Host "[ok] download via $($m.Name): $([math]::Round($zipSize / 1MB, 2)) MB"
            $downloaded = $true
            break
        } else {
            Write-Host "[warn] file too small ($zipSize bytes), might be error page, trying next mirror"
            Remove-Item $TempZip -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Host "[fail] $($m.Name): $_"
        Remove-Item $TempZip -Force -ErrorAction SilentlyContinue
    }
}

if ($downloaded) {
    # Extract
    Write-Host "[extract] extracting to $TempExtract ..."
    New-Item -ItemType Directory -Path $TempExtract -Force | Out-Null
    & tar -xzf $TempZip -C $TempExtract
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[error] tar extraction failed (exit $LASTEXITCODE)"
        exit 1
    }

    # Find python subdir
    $extractedPython = Join-Path $TempExtract "python"
    if (-not (Test-Path $extractedPython)) {
        $altPath = Get-ChildItem $TempExtract -Directory | Select-Object -First 1
        if ($altPath -and (Test-Path (Join-Path $altPath.FullName "python.exe"))) {
            $extractedPython = $altPath.FullName
        } else {
            Write-Host "[error] python/ subdir not found after extraction"
            Get-ChildItem $TempExtract -Recurse -Depth 1 | Select-Object FullName | Format-Table
            exit 1
        }
    }

    # Verify required files
    $checks = @("python.exe", "python3.dll", "Lib")
    foreach ($c in $checks) {
        if (-not (Test-Path (Join-Path $extractedPython $c))) {
            Write-Host "[error] missing required file: $c"
            exit 1
        }
    }
    Write-Host "[ok] verification passed: python.exe, python3.dll, Lib/"

    # Move to target location
    Write-Host "[move] $extractedPython -> $PythonDir"
    Move-Item $extractedPython $PythonDir -Force

    # Clean temp extract dir
    Remove-Item $TempExtract -Recurse -Force -ErrorAction SilentlyContinue

    # Print final size
    $finalSize = (Get-ChildItem $PythonDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Host ""
    Write-Host "[done] python-build-standalone ready at $PythonDir"
    Write-Host "       total size: $([math]::Round($finalSize / 1MB, 2)) MB"
    exit 0
}

# All mirrors failed - fallback to system Python
Write-Host ""
Write-Host "[fallback] all mirrors failed, trying system Python copy..."

$systemPythonDirs = @(
    "C:\Users\f'gu'y\AppData\Local\Programs\Python\Python311",
    "C:\Program Files\Python311",
    "C:\Program Files (x86)\Python311"
)

$systemPy = $null
foreach ($d in $systemPythonDirs) {
    if (Test-Path (Join-Path $d "python.exe")) {
        $systemPy = $d
        break
    }
}

# Also try generic discovery via where.exe
if (-not $systemPy) {
    $pyExePath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($pyExePath) {
        $systemPy = Split-Path -Parent $pyExePath
        # If in WindowsApps, check for real install
        if ($systemPy -like "*WindowsApps*") {
            $systemPy = $null
        }
    }
}

if (-not $systemPy) {
    Write-Host "[error] system Python not found either. Please download $FileName manually:"
    Write-Host "       https://github.com/astral-sh/python-build-standalone/releases/tag/$PyTag"
    Write-Host "       Place it at $TempZip and re-run this script."
    exit 1
}

Write-Host "[copy] copying system Python from $systemPy to $PythonDir ..."
New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null

# Copy essential directories and files
$itemsToCopy = @("python.exe", "pythonw.exe", "python311.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "Lib", "DLLs", "LICENSE.txt")
$copiedCount = 0
foreach ($item in $itemsToCopy) {
    $src = Join-Path $systemPy $item
    if (Test-Path $src) {
        Copy-Item $src -Destination $PythonDir -Recurse -Force
        $copiedCount++
    }
}

# Remove site-packages from copied Lib (we'll install fresh deps)
$sitePackagesPath = Join-Path $PythonDir "Lib\site-packages"
if (Test-Path $sitePackagesPath) {
    Remove-Item $sitePackagesPath -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "[clean] removed existing site-packages (will install fresh deps)"
}

# Verify
if (-not (Test-Path (Join-Path $PythonDir "python.exe"))) {
    Write-Host "[error] copy failed - python.exe not found in target"
    exit 1
}

$finalSize = (Get-ChildItem $PythonDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host "[done] system Python copied to $PythonDir"
Write-Host "       total size: $([math]::Round($finalSize / 1MB, 2)) MB"
Write-Host "       source: $systemPy"
Write-Host "[note] this is a system Python copy, not python-build-standalone."
Write-Host "       Target machines need VC++ Runtime Redistributable installed."
