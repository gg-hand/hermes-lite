# =============================================================
# Teage Liu stop script (PowerShell, Windows)
# 停止服务及其相关后台线程任务（cleanup_loop, file_cleanup_loop,
# metrics_persist_loop, cron_scheduler, SSE 流等）
#
# 停止流程：
#   1. 通过 PID 文件发送 SIGTERM（触发 FastAPI lifespan 关闭 →
#      BackgroundTaskRegistry.cancel_all() → container.close()）
#   2. 通过端口占用查找并终止残留进程（fallback）
#   3. 等待确认端口已释放
#   4. 清理 PID 文件
#   5. 可选：同时停止根目录 server.py（端口 3000）
#
# Usage:
#   .\stop.ps1                          # 停止默认端口 8000
#   $env:TEAGE_PORT=7007; .\stop.ps1    # 停止自定义端口
#   .\stop.ps1 -AlsoRootServer          # 同时停止根目录 server.py（端口 3000）
# =============================================================
param(
    [switch]$AlsoRootServer
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PidFile = Join-Path $ScriptDir ".server.pid"
$LogDir  = if ($env:TEAGE_LOG_DIR) { $env:TEAGE_LOG_DIR } else { Join-Path $ScriptDir "data" }

# ========== 0. Load .env ==========
$envFile = Join-Path $ScriptDir ".env"
if (Test-Path $envFile) {
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

# ========== 1. Defaults ==========
if (-not $env:TEAGE_PORT) { $env:TEAGE_PORT = "8000" }
$port = [int]$env:TEAGE_PORT

Write-Host "=== Teage Liu 停止服务 ===" -ForegroundColor Cyan
Write-Host "时间:   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "端口:   $port"
Write-Host ""

# ========== 2. 停止主服务 ==========
Write-Host "[1/3] 停止主服务 (端口 $port) ..." -ForegroundColor Yellow

$stoppedViaPid = $false

# 方式 A: 通过 PID 文件（优先 — 能触发 lifespan 优雅关闭）
if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -Raw).Trim()
    if ($oldPid -match '^\d+$') {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "  通过 PID 文件 → 终止 PID $oldPid ..." -ForegroundColor Gray
            # 先发 SIGTERM（相当于 Ctrl+C），让 uvicorn 触发 lifespan 关闭
            # FastAPI lifespan 关闭阶段自动执行：
            #   task_registry.cancel_all() — 取消后台 asyncio 任务
            #   close_container() — 关闭 DI 组件
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3  # 等待 lifespan 优雅关闭
            $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  进程未退出，执行强杀 PID $oldPid ..." -ForegroundColor Yellow
                $proc.Kill()
                Start-Sleep -Seconds 1
            } else {
                Write-Host "  进程 $oldPid 已优雅退出" -ForegroundColor Green
            }
            $stoppedViaPid = $true
        } else {
            Write-Host "  PID 文件中的 $oldPid 已不存在（可能已退出）" -ForegroundColor Gray
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# 方式 B: 通过端口占用查找（fallback，处理 PID 文件不存在或失效的情况）
if (-not $stoppedViaPid) {
    Write-Host "  通过端口 $port 查找残留进程..." -ForegroundColor Gray
    $portConns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    $listener  = $portConns | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1

    if ($listener) {
        $portPid = $listener.OwningProcess
        if ($portPid -and $portPid -ne 0) {
            Write-Host "  端口 $port 被 PID $portPid 占用，终止中..." -ForegroundColor Yellow
            $proc = Get-Process -Id $portPid -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 3
                $proc = Get-Process -Id $portPid -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "  强杀 PID $portPid ..." -ForegroundColor Yellow
                    $proc.Kill()
                    Start-Sleep -Seconds 1
                }
            }
            $stoppedViaPid = $true
        }
    }
}

if (-not $stoppedViaPid) {
    Write-Host "  未发现运行中的服务" -ForegroundColor Green
}

# 确认端口已释放（最多等 10 秒）
Write-Host "  确认端口 $port 释放..." -ForegroundColor Gray
$portReleased = $false
for ($i = 0; $i -lt 20; $i++) {
    $remain = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue `
        | Where-Object { $_.State -eq 'Listen' }).Count
    if ($remain -eq 0) {
        Write-Host "  端口 $port 已释放" -ForegroundColor Green
        $portReleased = $true
        break
    }
    Start-Sleep -Milliseconds 500
}
if (-not $portReleased) {
    Write-Host "  [WARN] 端口 $port 仍被占用，可能需要手动处理" -ForegroundColor Yellow
}

# 再次确保 PID 文件已清理
if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "  已清理 PID 文件" -ForegroundColor Gray
}

Write-Host ""

# ========== 3.（可选）停止根目录 server.py ==========
if ($AlsoRootServer) {
    Write-Host "[2/3] 停止根目录 server.py (端口 3000) ..." -ForegroundColor Yellow

    $rootPortConns = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue
    $rootListener  = $rootPortConns | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1

    if ($rootListener) {
        $rootPid = $rootListener.OwningProcess
        if ($rootPid -and $rootPid -ne 0) {
            Write-Host "  端口 3000 被 PID $rootPid 占用，终止中..." -ForegroundColor Yellow
            $proc = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $rootPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
                $proc = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "  强杀 PID $rootPid ..." -ForegroundColor Yellow
                    $proc.Kill()
                    Start-Sleep -Seconds 1
                }
            }
        }
    } else {
        Write-Host "  未发现 server.py 运行" -ForegroundColor Gray
    }

    # 确认端口 3000 释放
    for ($i = 0; $i -lt 10; $i++) {
        $remain = @(Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue `
            | Where-Object { $_.State -eq 'Listen' }).Count
        if ($remain -eq 0) {
            Write-Host "  端口 3000 已释放" -ForegroundColor Green
            break
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Host ""
    Write-Host "[3/3] 所有服务已停止" -ForegroundColor Cyan
} else {
    Write-Host "[2/2] 主服务已停止" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "提示: 如需同时停止根目录 server.py（端口 3000），请使用:" -ForegroundColor DarkGray
    Write-Host "  .\stop.ps1 -AlsoRootServer" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== 停止完成 ===" -ForegroundColor Cyan
Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
