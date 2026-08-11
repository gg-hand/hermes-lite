# 停止两个本地 worker 服务脚本
# 用法：在 teage-liu 目录运行：powershell -ExecutionPolicy Bypass -File stop_dual_workers.ps1
#
# 停止流程：
#   1. 通过 .dual_workers.pid 文件停止两个 worker（优先，触发优雅关闭）
#   2. 通过端口 8000/8001 查找并终止残留进程（兜底）
#   3. 等待端口释放
#   4. 清理 PID 文件

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$Ports = @(8000, 8001)
$PidFile = Join-Path $root ".dual_workers.pid"

Write-Host "=== 停止双 Worker 本地协作 ===" -ForegroundColor Cyan
Write-Host "时间:   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "端口:   $($Ports -join ', ')"
Write-Host ""

# ========== 1. 通过 PID 文件停止 ==========
Write-Host "[1/2] 通过 PID 文件停止..." -ForegroundColor Yellow
$killedFromPid = @()
if (Test-Path $PidFile) {
    try {
        $pids = Get-Content $PidFile | Where-Object { $_ -match '^\s*\d+\s*$' } | ForEach-Object { [int]$_.Trim() }
        foreach ($p in $pids) {
            $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  终止 PID $p ..." -ForegroundColor Gray
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
                $killedFromPid += $p
            } else {
                Write-Host "  PID $p 已不存在" -ForegroundColor Gray
            }
        }
    } catch {
        Write-Host "  [WARN] 读取 PID 文件失败：$_" -ForegroundColor Yellow
    }
} else {
    Write-Host "  PID 文件不存在" -ForegroundColor Gray
}

# 等待优雅退出
if ($killedFromPid.Count -gt 0) {
    Start-Sleep -Seconds 3
    foreach ($p in $killedFromPid) {
        $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "  PID $p 未退出，执行强杀..." -ForegroundColor Yellow
            try { $proc.Kill() } catch { }
            Start-Sleep -Seconds 1
        } else {
            Write-Host "  PID $p 已退出" -ForegroundColor Green
        }
    }
}

# ========== 2. 端口兜底清理 ==========
Write-Host "[2/2] 端口兜底清理..." -ForegroundColor Yellow
foreach ($port in $Ports) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        $listeners = $conns | Where-Object { $_.State -eq 'Listen' -and $_.OwningProcess -and $_.OwningProcess -ne 0 }
        if ($listeners) {
            foreach ($l in $listeners) {
                $lpid = $l.OwningProcess
                $proc = Get-Process -Id $lpid -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "  端口 $port 仍被 PID $lpid 占用，强制终止..." -ForegroundColor Gray
                    Stop-Process -Id $lpid -Force -ErrorAction SilentlyContinue
                }
            }
            Start-Sleep -Seconds 2
            # 二次强杀
            foreach ($l in $listeners) {
                $lpid = $l.OwningProcess
                $proc = Get-Process -Id $lpid -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "  PID $lpid 未退出，执行强杀..." -ForegroundColor Yellow
                    try { $proc.Kill() } catch { }
                }
            }
        }
    } catch {
        # Get-NetTCPConnection 不可用时忽略
    }
}

# ========== 3. 确认端口释放 ==========
Write-Host ""
Write-Host "确认端口释放..." -ForegroundColor Gray
foreach ($port in $Ports) {
    $released = $false
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $remain = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
                        Where-Object { $_.State -eq 'Listen' }).Count
        } catch { $remain = 0 }
        if ($remain -eq 0) {
            Write-Host "  端口 $port 已释放" -ForegroundColor Green
            $released = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $released) {
        Write-Host "  [WARN] 端口 $port 仍被占用，可能需要手动处理" -ForegroundColor Yellow
    }
}

# ========== 4. 清理 PID 文件 ==========
if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "已清理 PID 文件 $PidFile" -ForegroundColor Gray
}

Write-Host ""
Write-Host "=== 停止完成 ===" -ForegroundColor Cyan
Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
