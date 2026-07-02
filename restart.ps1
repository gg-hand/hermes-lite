# =============================================================
# Hermes Lite restart script (PowerShell)
# Usage: .\restart.ps1
# =============================================================
$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PidFile = Join-Path $ScriptDir ".server.pid"
$LogDir  = Join-Path $ScriptDir "data"

# ensure log directory
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Write-Host "=== Hermes Lite restart ===" -ForegroundColor Cyan
Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host ""

# ========== 1. Stop old process ==========
Write-Host "[1/3] Stopping old process..." -ForegroundColor Yellow

# A. via PID file
if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -Raw).Trim()
    if ($oldPid -match '^\d+$') {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "  Terminating PID $oldPid ..."
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  Force kill PID $oldPid ..."
                $proc.Kill()
                Start-Sleep -Seconds 1
            }
            Write-Host "  Process $oldPid exited"
        }
        else {
            Write-Host "  PID $oldPid from file no longer exists"
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# B. via port 8000
Write-Host "  Checking port 8000 ..."
$portConns = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
$listener  = $portConns | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1

if ($listener) {
    $portPid = $listener.OwningProcess
    if ($portPid -and $portPid -ne 0) {
        Write-Host "  Port 8000 held by PID $portPid, killing..."
        $proc = Get-Process -Id $portPid -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $proc = Get-Process -Id $portPid -ErrorAction SilentlyContinue
            if ($proc) { $proc.Kill(); Start-Sleep -Seconds 1 }
        }
    }
}

# confirm port released (wait up to 5 sec)
for ($i = 0; $i -lt 10; $i++) {
    $remain = @(Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue `
        | Where-Object { $_.State -eq 'Listen' }).Count
    if ($remain -eq 0) {
        Write-Host "  Port 8000 released"
        break
    }
    Start-Sleep -Milliseconds 500
}

Write-Host ""

# ========== 2. Load config ==========
Write-Host "[2/3] Loading config..." -ForegroundColor Yellow

$envFile = Join-Path $ScriptDir ".env"
if (Test-Path $envFile) {
    Write-Host "  Loading .env file"
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $parts = $line.Split('=', 2)
            $key   = $parts[0].Trim()
            $val   = $parts[1].Trim()
            [Environment]::SetEnvironmentVariable($key, $val, 'Process')
        }
    }
}

if (-not $env:HERMES_CONFIG)     { $env:HERMES_CONFIG     = "config.yaml" }
if (-not $env:HERMES_SERVER_LOG) { $env:HERMES_SERVER_LOG = "data/server.log" }

Write-Host "  Config: $env:HERMES_CONFIG"
Write-Host "  Log:    $env:HERMES_SERVER_LOG"
Write-Host ""

# ========== 3. Start service ==========
Write-Host "[3/3] Starting Hermes Lite..." -ForegroundColor Yellow
Write-Host ""

# start python in background, let it own its own log file
$process = Start-Process -FilePath "python" `
    -ArgumentList "-m uvicorn src.server:app --host 0.0.0.0 --port 8000 --workers 1" `
    -WorkingDirectory $ScriptDir `
    -PassThru `
    -WindowStyle Hidden

$NewPid = $process.Id
$NewPid | Out-File -FilePath $PidFile -NoNewline

Write-Host "  Service started (PID: $NewPid)"

# wait for ready (up to 10 sec)
Write-Host "  Waiting for health check..."
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -TimeoutSec 1 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Write-Host "  [OK] Service is ready" -ForegroundColor Green
            $ready = $true
            break
        }
    }
    catch { }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    Write-Host "  [WARN] Health check timed out, please check log" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Restart complete ===" -ForegroundColor Cyan
Write-Host "PID:     $NewPid"
Write-Host "URL:     http://0.0.0.0:8000"
Write-Host "Health:  http://0.0.0.0:8000/health"
