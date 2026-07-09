; uninstaller-hooks.nsh — Hermes Lite NSIS 卸载清理钩子
;
; 由 tauri.conf.json 的 bundle.windows.nsis.installerHooks 引用。
; 在 NSIS 卸载流程中插入自定义逻辑，确保卸载后无残留。
;
; 清理目标：
;   0. 残留进程                       — 强制终止 Hermes Lite.exe 和孤立的 sidecar (python src.server)
;   1. %APPDATA%\hermes-lite        — 用户数据目录（config.yaml、sidecar.log、向量库、用户画像）
;   2. %APPDATA%\Hermes Lite        — Tauri 应用配置目录（window-state、store、EBWebView 缓存）
;   3. Windows Credential Manager   — API 密钥条目（service = com.hermeslite.desktop.apikeys）
;
; NSIS 自动处理（无需手动）：
;   - 程序文件目录 (%LOCALAPPDATA%\Hermes Lite)
;   - 开始菜单快捷方式
;   - 卸载注册表项 (HKCU\...\Uninstall\Hermes Lite)
;
; NSIS 转义规则：$$ → $, $\r$\n → CRLF, $\n → LF, $\" → "
; PowerShell 变量用 $$ 前缀转义（如 $$matches → $matches）

!macro NSIS_HOOK_PREINSTALL
  ; 安装前杀死残留 sidecar 进程，避免文件锁定导致覆盖失败
  DetailPrint "Hermes Lite: killing residual sidecar processes before install..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like ''*Hermes Lite*\python\python.exe'' } | Stop-Process -Force -ErrorAction SilentlyContinue"'
  Sleep 500
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; 经深度审查确认：Tauri 2 已正确生成 web/ 目录的 File 指令
  ; （installer.nsi 第 2273-2301 行，共 29 个文件）。
  ; 之前认为"frontendDist 与 resources 冲突导致跳过"的结论是错误的。
  ; 此宏保留为空，以备将来需要手动释放额外文件。
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; 0. 强制终止残留进程，避免文件锁定导致删除失败
  ;    NSIS 会提示用户关闭应用，但 sidecar (python) 可能被孤立
  DetailPrint "Hermes Lite: terminating residual processes..."
  ; 终止 Tauri 主进程（NSIS 兜底）
  nsExec::ExecToLog 'taskkill /F /IM "Hermes Lite.exe" /T 2>nul'
  ; 终止孤立的 sidecar：匹配安装目录下的 python.exe（精确路径，避免误杀系统 Python）
  ;    PowerShell 变量 $$ → $, '' → '
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like ''*Hermes Lite*\python\python.exe'' } | Stop-Process -Force -ErrorAction SilentlyContinue"'
  ; 等待文件句柄释放
  Sleep 500

  ; 卸载前提示：是否同时删除用户数据
  ; 用「」替代""避免引号转义问题，用反引号定界消息文本
  MessageBox MB_YESNO `是否同时删除用户数据（配置、向量库、API 密钥）?$\n$\n选择「是」将完全清除所有痕迹，选择「否」仅卸载程序文件。` IDYES _wipe_data IDNO _keep_data

  _wipe_data:
    SetShellVarContext current

    ; 1. 删除用户数据目录（config / sidecar.log / 向量库 / 用户画像）
    IfFileExists "$APPDATA\hermes-lite" 0 _skip_data_dir
      DetailPrint "Hermes Lite: deleting user data directory $APPDATA\hermes-lite"
      RMDir /r "$APPDATA\hermes-lite"
    _skip_data_dir:

    ; 2. 删除 Tauri 应用配置目录（window-state / store / EBWebView）
    IfFileExists "$APPDATA\Hermes Lite" 0 _skip_appconfig
      DetailPrint "Hermes Lite: deleting app config directory $APPDATA\Hermes Lite"
      RMDir /r "$APPDATA\Hermes Lite"
    _skip_appconfig:

    ; 3. 清理 Windows Credential Manager 中的 API 密钥
    ;    keyring crate 以 service 名存储凭据，枚举 cmdkey /list 并删除匹配项
    ;    PowerShell 变量 $$ 前缀转义为 NSIS 字面 $，'' 转义为 NSIS 字面 '
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
