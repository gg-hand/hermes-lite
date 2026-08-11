# 启动两个本地 worker 服务脚本（阶段1：共享 blackboard 文件协作）
# 用法：
#   1. 确保 .env 文件存在且配置了 LLM_MAIN_API_KEY 等（参考 .env.example）
#   2. 在 teage-liu 目录运行：powershell -ExecutionPolicy Bypass -File start_dual_workers.ps1
#   3. 前端工作台访问 http://localhost:8000（主实例）
#   4. 通过前端 Director 角色发广播，两个 worker 都会响应
#   5. 停止服务：运行 .\stop_dual_workers.ps1
#
# 本脚本特点：
#   - 启动前强制清理端口 8000/8001 上的残留进程（含旧 PID 文件中记录的 PID）
#   - 后台启动两个 worker（Start-Process -PassThru）
#   - PID 写入 .dual_workers.pid（一行一个）
#   - 脚本本身不阻塞、不 ReadKey，启动完成后立即退出

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$Ports = @(8000, 8001)
$PidFile = Join-Path $root ".dual_workers.pid"

Write-Host "=== 启动双 Worker 本地协作（阶段1：共享 blackboard） ===" -ForegroundColor Cyan
Write-Host "实例1: 端口 8000, agent_id=teagent-lu,   config=config.yaml"
Write-Host "实例2: 端口 8001, agent_id=teagent-liu-2, config=config2.yaml"
Write-Host "共享 blackboard_dir: data/blackboard"
Write-Host ""

# ========== 0. 加载 .env ==========
$envFile = Join-Path $root ".env"
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
} else {
    Write-Host "[警告] .env 文件不存在，LLM_MAIN_API_KEY 等环境变量可能缺失" -ForegroundColor Yellow
    Write-Host "       若服务启动后 LLM 调用失败，请先创建 .env 并设置 API Key" -ForegroundColor Yellow
    Write-Host ""
}

# ========== 1. 启动前清理残留进程 ==========
Write-Host "[1/4] 清理端口 $($Ports -join ',') 上的残留进程..." -ForegroundColor Yellow

# 1a. 通过旧 PID 文件清理
if (Test-Path $PidFile) {
    try {
        $oldPids = Get-Content $PidFile | Where-Object { $_ -match '^\s*\d+\s*$' } | ForEach-Object { [int]$_.Trim() }
        foreach ($opid in $oldPids) {
            $p = Get-Process -Id $opid -ErrorAction SilentlyContinue
            if ($p) {
                Write-Host "  通过旧 PID 文件终止 PID $opid ..." -ForegroundColor Gray
                Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        Write-Host "  [WARN] 读取旧 PID 文件失败：$_" -ForegroundColor Yellow
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# 1b. 通过端口查找清理（兜底）
foreach ($port in $Ports) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        $listeners = $conns | Where-Object { $_.State -eq 'Listen' -and $_.OwningProcess -and $_.OwningProcess -ne 0 }
        foreach ($l in $listeners) {
            $lpid = $l.OwningProcess
            $p = Get-Process -Id $lpid -ErrorAction SilentlyContinue
            if ($p) {
                Write-Host "  端口 $port 被 PID $lpid 占用，强制终止..." -ForegroundColor Gray
                Stop-Process -Id $lpid -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        # Get-NetTCPConnection 在某些环境下可能不可用，忽略
    }
}

# 1c. 等待端口释放（最多 10 秒）
foreach ($port in $Ports) {
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $remain = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
                        Where-Object { $_.State -eq 'Listen' }).Count
        } catch { $remain = 0 }
        if ($remain -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    try {
        $still = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
                   Where-Object { $_.State -eq 'Listen' }).Count
    } catch { $still = 0 }
    if ($still -gt 0) {
        Write-Host "  [WARN] 端口 $port 仍被占用，可能启动失败" -ForegroundColor Yellow
    } else {
        Write-Host "  端口 $port 已释放" -ForegroundColor Green
    }
}
Write-Host ""

# ========== 2. 创建必要的 data 目录 ==========
$dirs = @("data", "data/blackboard", "data/blackboard/agents", "data/blackboard/audit",
          "data/blackboard/locks", "data/blackboard/tasks", "data/blackboard/schemas",
          "data/blackboard/snapshots", "data/uploads2", "data/history2")
foreach ($d in $dirs) {
    if (-not (Test-Path "$root\$d")) {
        New-Item -ItemType Directory -Path "$root\$d" -Force | Out-Null
    }
}

# ========== 3. 启动两个 worker ==========
Write-Host "[2/4] 启动实例1 (端口 8000)..." -ForegroundColor Green
$proc1 = Start-Process -FilePath "python" -ArgumentList "-m", "teage_liu" -WorkingDirectory $root -PassThru -WindowStyle Normal
Write-Host "      PID=$($proc1.Id), 配置=config.yaml"

Write-Host "[3/4] 启动实例2 (端口 8001)..." -ForegroundColor Green
$env:TEAGE_CONFIG = "config2.yaml"
$proc2 = Start-Process -FilePath "python" -ArgumentList "-m", "teage_liu" -WorkingDirectory $root -PassThru -WindowStyle Normal
Remove-Item Env:TEAGE_CONFIG
Write-Host "      PID=$($proc2.Id), 配置=config2.yaml"

# ========== 4. 写 PID 文件并退出 ==========
"$($proc1.Id)`n$($proc2.Id)" | Out-File $PidFile -Encoding utf8
Write-Host ""
Write-Host "[4/4] PID 已写入 $PidFile" -ForegroundColor Green
Write-Host ""
Write-Host "=== 两个服务已在后台启动 ===" -ForegroundColor Cyan
Write-Host "实例1 API: http://localhost:8000"
Write-Host "实例2 API: http://localhost:8001"
Write-Host "前端工作台: http://localhost:8000/ (主实例)"
Write-Host ""
Write-Host "停止服务：.\stop_dual_workers.ps1"
Write-Host ""
