# =============================================================
# Teage Liu restart script (PowerShell, Windows 本地开发)
# Usage:
#   .\restart.ps1                  # default 0.0.0.0:8000
#   $env:TEAGE_PORT=7007; .\restart.ps1   # custom port
# =============================================================
$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PidFile   = Join-Path $ScriptDir ".server.pid"
$LogDir    = if ($env:TEAGE_LOG_DIR) { $env:TEAGE_LOG_DIR } else { Join-Path $ScriptDir "data" }
$ServerLog = if ($env:TEAGE_SERVER_LOG) { $env:TEAGE_SERVER_LOG } else { Join-Path $LogDir "server.log" }

# ensure log directory
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# ========== 0. Load .env ==========
$envFile = Join-Path $ScriptDir ".env"
if (Test-Path $envFile) {
    Write-Host "  Loading .env file" -ForegroundColor DarkGray
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

# ========== 1. Defaults (env-first) ==========
if (-not $env:TEAGE_HOST)        { $env:TEAGE_HOST        = "0.0.0.0" }
if (-not $env:TEAGE_PORT)        { $env:TEAGE_PORT        = "8000" }
if (-not $env:TEAGE_CONFIG)      { $env:TEAGE_CONFIG      = "config.yaml" }

Write-Host "=== Teage Liu restart ===" -ForegroundColor Cyan
Write-Host "Time:   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "Host:   $env:TEAGE_HOST"
Write-Host "Port:   $env:TEAGE_PORT"
Write-Host "Config: $env:TEAGE_CONFIG"
Write-Host "Log:    $ServerLog"
Write-Host ""

# ========== 2. Stop old process ==========
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

# B. via port
$port = [int]$env:TEAGE_PORT
Write-Host "  Checking port $port ..."
$portConns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
$listener  = $portConns | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1

if ($listener) {
    $portPid = $listener.OwningProcess
    if ($portPid -and $portPid -ne 0) {
        Write-Host "  Port $port held by PID $portPid, killing..."
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
    $remain = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue `
        | Where-Object { $_.State -eq 'Listen' }).Count
    if ($remain -eq 0) {
        Write-Host "  Port $port released"
        break
    }
    Start-Sleep -Milliseconds 500
}

Write-Host ""

# ========== 3. API Key validation ==========
# config.yaml 优先使用 ${LLM_MAIN_API_KEY}，兼容 provider 命名（DEEPSEEK_API_KEY 等）
$hasKey = $false
foreach ($k in @('LLM_MAIN_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY')) {
    $val = [Environment]::GetEnvironmentVariable($k, 'Process')
    if ($val) { $hasKey = $true; break }
}
if (-not $hasKey) {
    Write-Host "[ERROR] No API Key set (LLM_MAIN_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY ...)." -ForegroundColor Red
    Write-Host "        Please configure .env, see .env.example"
    exit 1
}

# 安全告警：TEAGE_API_KEY 未设置时所有 API 端点无认证保护
if (-not $env:TEAGE_API_KEY) {
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "WARNING: TEAGE_API_KEY 未设置，所有 API 端点无认证保护。" -ForegroundColor Yellow
    Write-Host "         生产部署请在 .env 中设置 TEAGE_API_KEY=<strong-password>" -ForegroundColor Yellow
    Write-Host "         本地开发可忽略此告警。" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
} else {
    Write-Host "TEAGE_API_KEY 已设置，API 认证将启用。" -ForegroundColor DarkGray
}

# ========== 4. Start service ==========
Write-Host "[2/3] Starting Teage Liu..." -ForegroundColor Yellow
Write-Host ""

$argList = "-m uvicorn teage_liu.app:app --host $env:TEAGE_HOST --port $port --workers 1"

# start python in background, redirect output to log file
$process = Start-Process -FilePath "python" `
    -ArgumentList $argList `
    -WorkingDirectory $ScriptDir `
    -RedirectStandardOutput $ServerLog `
    -RedirectStandardError "$ServerLog.err" `
    -PassThru `
    -WindowStyle Hidden

$NewPid = $process.Id
$NewPid | Out-File -FilePath $PidFile -NoNewline

Write-Host "  Service started (PID: $NewPid)"

# wait for ready (up to 10 sec)
Write-Host "[3/3] Waiting for health check..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 1 -ErrorAction Stop
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
    Write-Host "  [WARN] Health check timed out, please check log: $ServerLog" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Restart complete ===" -ForegroundColor Cyan
Write-Host "PID:      $NewPid"
Write-Host "URL:      http://${env:TEAGE_HOST}:$port"
Write-Host "Health:   http://127.0.0.1:$port/health"
Write-Host "API Docs: http://127.0.0.1:$port/docs"
Write-Host "PID file: $PidFile"
