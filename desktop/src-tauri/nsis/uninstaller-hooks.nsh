; uninstaller-hooks.nsh — Hermes Lite NSIS 安装/卸载清理钩子
;
; 由 tauri.conf.json 的 bundle.windows.nsis.installerHooks 引用。
; 在 NSIS 安装/卸载流程中插入自定义逻辑，确保无进程残留、无文件残留。
;
; 清理目标：
;   0. 残留进程 — 主进程 / WebView2 子进程 / Python sidecar / MCP 服务器 / execute_command 子进程
;   1. %APPDATA%\hermes-lite        — 用户数据目录（config、向量库、用户画像、schedules、logs）
;   2. %APPDATA%\Hermes Lite        — Tauri 应用配置目录（window-state、store、EBWebView 缓存）
;   3. %PROFILE%\.cache\chroma       — chromadb ONNX 模型缓存（仅完全清除时删除）
;   4. Windows Credential Manager   — API 密钥条目（service = com.hermeslite.desktop.apikeys）
;
; NSIS 自动处理（无需手动）：
;   - 程序文件目录 (%LOCALAPPDATA%\Hermes Lite)
;   - 开始菜单快捷方式
;   - 卸载注册表项 (HKCU\...\Uninstall\Hermes Lite)
;
; NSIS 转义规则：$$ → $, $\r$\n → CRLF, $\n → LF, $\" → "
; PowerShell 变量用 $$ 前缀转义（如 $$matches → $matches）

; ---------------------------------------------------------------------------
; 进程清理宏（安装前 & 卸载前共用）
; 策略：
;   1. taskkill 杀主进程及其进程树（/T 递归杀子进程）
;   2. PowerShell 按路径匹配杀安装目录下所有进程（python.exe 等）
;   3. PowerShell 按命令行匹配杀 WebView2 子进程和 MCP 服务器进程
;      （命令行包含 "Hermes Lite" 路径的进程，覆盖 --user-data-dir 参数）
; ---------------------------------------------------------------------------

!macro _KILL_HERMES_PROCESSES
  DetailPrint "Hermes Lite: terminating all related processes..."
  ; 1. 杀主进程及其进程树（WebView2 直接子进程随之退出）
  nsExec::ExecToLog 'taskkill /F /IM "hermes-lite-desktop.exe" /T 2>nul'
  ; 2. 杀安装目录下的所有进程 + 命令行包含 "Hermes Lite" 的进程
  ;    覆盖：python.exe（sidecar）、msedgewebview2.exe（WebView2 子进程）、
  ;          node.exe（MCP 服务器）、execute_command 启动的子进程等
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $$_.CommandLine -like ''*Hermes Lite*'' -or $$_.ExecutablePath -like ''*Hermes Lite*'' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"'
  ; 等待文件句柄释放
  Sleep 1000
!macroend

!macro NSIS_HOOK_PREINSTALL
  ; 安装前杀死残留进程，避免文件锁定导致覆盖失败
  !insertmacro _KILL_HERMES_PROCESSES
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; 经深度审查确认：Tauri 2 已正确生成 web/ 目录的 File 指令
  ; （installer.nsi 第 2273-2301 行，共 29 个文件）。
  ; 之前认为"frontendDist 与 resources 冲突导致跳过"的结论是错误的。
  ; 此宏保留为空，以备将来需要手动释放额外文件。
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; 0. 强制终止所有相关进程
  !insertmacro _KILL_HERMES_PROCESSES

  ; 卸载前提示：是否同时删除用户数据
  MessageBox MB_YESNO `是否同时删除用户数据（配置、向量库、API 密钥、模型缓存）?$\n$\n选择「是」将完全清除所有痕迹，选择「否」仅卸载程序文件。` IDYES _wipe_data IDNO _keep_data

  _wipe_data:
    SetShellVarContext current

    ; 1. 删除用户数据目录（config / sidecar.log / 向量库 / 用户画像 / schedules / server.log）
    IfFileExists "$APPDATA\hermes-lite" 0 _skip_data_dir
      DetailPrint "Hermes Lite: deleting user data directory $APPDATA\hermes-lite"
      RMDir /r "$APPDATA\hermes-lite"
    _skip_data_dir:

    ; 2. 删除 Tauri 应用配置目录（window-state / store / EBWebView 缓存）
    IfFileExists "$APPDATA\Hermes Lite" 0 _skip_appconfig
      DetailPrint "Hermes Lite: deleting app config directory $APPDATA\Hermes Lite"
      RMDir /r "$APPDATA\Hermes Lite"
    _skip_appconfig:

    ; 3. 删除 chromadb ONNX 模型缓存（~80MB）
    IfFileExists "$PROFILE\.cache\chroma\onnx_models" 0 _skip_chroma_cache
      DetailPrint "Hermes Lite: deleting chromadb ONNX model cache"
      RMDir /r "$PROFILE\.cache\chroma\onnx_models"
    _skip_chroma_cache:

    ; 4. 清理 Windows Credential Manager 中的 API 密钥
    Push $0
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference=''SilentlyContinue''; $$out = cmdkey /list 2>$$null; if ($$out) { $$out | ForEach-Object { if ($$_ -match ''Target:\s*(.+)'' ) { $$t = $$matches[1].Trim(); if ($$t -like ''*com.hermeslite.desktop.apikeys*'') { cmdkey /delete:$$t 2>$$null } } } }"'
    Pop $0
    Pop $0
    Goto _uninstall_done

  _keep_data:
    DetailPrint "Hermes Lite: user chose to keep data, skipping cleanup"

  _uninstall_done:
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
!macroend
