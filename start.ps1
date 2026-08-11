# =============================================================
# Teage Liu start script (PowerShell, Windows 本地开发，前台运行)
# Usage:
#   .\start.ps1                          # default 0.0.0.0:8000
#   $env:TEAGE_PORT=7007; .\start.ps1    # custom port
# 与 restart.ps1 的区别：前台运行（Ctrl+C 退出），不写 PID 文件
# =============================================================
$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

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
if (-not $env:TEAGE_HOST)   { $env:TEAGE_HOST   = "0.0.0.0" }
if (-not $env:TEAGE_PORT)   { $env:TEAGE_PORT   = "8000" }
if (-not $env:TEAGE_CONFIG) { $env:TEAGE_CONFIG = "config.yaml" }

$port = [int]$env:TEAGE_PORT

# ========== 2. API Key validation ==========
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

# ========== 3. 端口占用检查 ==========
$portConns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
$listener  = $portConns | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1
if ($listener) {
    $portPid = $listener.OwningProcess
    Write-Host "[ERROR] 端口 $port 已被占用（PID: $portPid）。" -ForegroundColor Red
    Write-Host "        请先停止旧服务："
    Write-Host "          Stop-Process -Id $portPid -Force"
    Write-Host "        或使用 restart.ps1（自动停止旧服务后重启）："
    Write-Host "          .\restart.ps1"
    exit 1
}

# ========== 4. Print banner ==========
Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
Write-Host " Teage Liu 本地开发模式（前台）"
Write-Host "   Host:     $env:TEAGE_HOST"
Write-Host "   Port:     $env:TEAGE_PORT"
Write-Host "   Config:   $env:TEAGE_CONFIG"
Write-Host "   健康检查: http://127.0.0.1:$port/health"
Write-Host "   API 文档: http://127.0.0.1:$port/docs"
Write-Host "   退出:     Ctrl+C"
Write-Host "------------------------------------------------------------" -ForegroundColor Cyan

# ========== 5. Start service (foreground) ==========
# 前台运行：Ctrl+C 直接退出，不写 PID 文件
& python -m uvicorn teage_liu.app:app `
    --host $env:TEAGE_HOST `
    --port $port `
    --workers 1
