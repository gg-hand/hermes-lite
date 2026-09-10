# scripts/audit_liu2.ps1 —— liu2 一致性审计一键入口（协议行为套件 + 契约测试）
#
# 用途：本地/CI 一致的门禁。CI 见 .github/workflows/liu2-audit.yml（同两步，最小依赖）。
# 用法：powershell -File scripts\audit_liu2.ps1
# 说明：解释器固定 D:\soft\Python311\python.exe（python/py 为 Store stub 不可用）。

# 说明:不用 'Stop' —— PowerShell 5.1 会把原生命令的 stderr 输出(如 Python 警告)
# 当成 ErrorRecord 而中断脚本;统一由退出码判定,并用 *>&1 合并所有流。
$ErrorActionPreference = 'Continue'
$py = 'D:\soft\Python311\python.exe'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not (Test-Path $py)) {
    Write-Host "未找到 Python 解释器: $py" -ForegroundColor Red
    exit 2
}

Write-Host '[1/2] 协议行为套件(teage_liu2/PROTOCOL/behavior-suite)' -ForegroundColor Cyan
& $py 'teage_liu2\PROTOCOL\behavior-suite\runner.py' *>&1 | Write-Host
if ($LASTEXITCODE -ne 0) { Write-Host '行为套件失败' -ForegroundColor Red; exit 1 }

Write-Host '[2/2] 契约测试(teage_liu2/tests_core + tests_branches)' -ForegroundColor Cyan
& $py -m pytest 'teage_liu2\tests_core' 'teage_liu2\tests_branches' -q *>&1 | Write-Host
if ($LASTEXITCODE -ne 0) { Write-Host '契约测试失败' -ForegroundColor Red; exit 1 }

Write-Host '审计通过' -ForegroundColor Green
exit 0
