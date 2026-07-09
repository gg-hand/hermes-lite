# dev-sidecar.ps1 — 启动 Python sidecar 供桌面端 dev 模式使用
# 用法：在 desktop/ 目录下执行 pnpm dev:sidecar
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot/../.."
$env:HERMES_PORT = "18000"
$env:HERMES_DESKTOP = "1"
$env:PYTHONUNBUFFERED = "1"
Set-Location $root
Write-Host "[dev-sidecar] starting python -m src.server on port 18000 (cwd=$root)" -ForegroundColor Cyan
python -m src.server
