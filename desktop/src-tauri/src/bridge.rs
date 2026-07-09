//! bridge.rs — WebView2 初始化脚本注入
//!
//! 职责：
//! 1. 文件选择器拦截：monkey-patch HTMLInputElement.prototype.click，触发 Tauri dialog
//! 2. 桌面环境标识：注入 window.__HERMES_DESKTOP__ = true
//! 3. API base 改写：将相对路径 /api/... 加 sidecar 端口前缀
//! 4. window.confirm 保留不动（MVP 决策）
//! 5. 自定义标题栏注入：拖拽区 + 最小化 + 关闭按钮（decorations:false 补偿）

use tauri::{Webview, WebviewWindow};

pub fn init_script(port: u16) -> String {
    let base_url = format!("http://127.0.0.1:{}", port);

    format!(
        r#"(function() {{
    // 1. 桌面环境标识
    window.__HERMES_DESKTOP__ = true;
    window.__HERMES_SIDECAR_BASE__ = {base_url:?};

    // 2. API base 改写：拦截 fetch，将相对路径 / 开头的请求指向 sidecar
    var originalFetch = window.fetch;
    window.fetch = function(input, init) {{
        if (typeof input === 'string' && input.charAt(0) === '/' && input.charAt(1) !== '/') {{
            input = window.__HERMES_SIDECAR_BASE__ + input;
        }}
        return originalFetch.call(this, input, init);
    }};

    // XMLHttpRequest 同步改写
    var originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {{
        if (typeof url === 'string' && url.charAt(0) === '/' && url.charAt(1) !== '/') {{
            url = window.__HERMES_SIDECAR_BASE__ + url;
        }}
        return originalOpen.apply(this, arguments);
    }};

    // 3. 文件选择器拦截：monkey-patch HTMLInputElement.prototype.click
    //    当 input[type=file] 被点击时，改用 Tauri dialog API
    var __TAURI__ = window.__TAURI__;
    var originalClick = HTMLInputElement.prototype.click;
    HTMLInputElement.prototype.click = function() {{
        if (this.tagName === 'INPUT' && this.type === 'file' && __TAURI__ && __TAURI__.dialog) {{
            __handleFileInputClick(this);
            return;
        }}
        return originalClick.apply(this, arguments);
    }};

    function __handleFileInputClick(inputEl) {{
        if (!__TAURI__ || !__TAURI__.dialog || !__TAURI__.dialog.open) {{
            console.warn('[desktop-bridge] Tauri dialog API not available, falling back to native click');
            return originalClick.call(inputEl);
        }}
        // 调用 Tauri dialog.open 选择文件
        __TAURI__.dialog.open({{
            multiple: inputEl.hasAttribute('multiple'),
            filters: []
        }}).then(function(selected) {{
            if (!selected) return;  // 用户取消
            var paths = Array.isArray(selected) ? selected : [selected];
            __uploadFilesAndSetInput(inputEl, paths);
        }}).catch(function(err) {{
            console.error('[desktop-bridge] dialog.open failed:', err);
        }});
    }}

    async function __uploadFilesAndSetInput(inputEl, paths) {{
        try {{
            var files = [];
            for (var i = 0; i < paths.length; i++) {{
                var p = paths[i];
                // 通过 sidecar 的 /files/from-path 端点上传
                var body = new URLSearchParams();
                body.set('path', p);
                var resp = await fetch('/files/from-path', {{
                    method: 'POST',
                    body: body
                }});
                if (!resp.ok) {{
                    console.error('[desktop-bridge] upload failed for', p, resp.status);
                    continue;
                }}
                var result = await resp.json();
                // 构造 File 对象（从路径提取文件名）
                var filename = p.split(/[\\\\\/]/).pop();
                var file = new File([''], filename, {{ type: 'application/octet-stream' }});
                file.__file_id = result.file_id || result.id;
                file.__path = p;
                files.push(file);
            }}
            if (files.length > 0) {{
                // 构造 DataTransfer 合成 FileList
                var dt = new DataTransfer();
                files.forEach(function(f) {{ dt.items.add(f); }});
                inputEl.files = dt.files;
                // 触发 change 事件
                var event = new Event('change', {{ bubbles: true }});
                inputEl.dispatchEvent(event);
            }}
        }} catch (e) {{
            console.error('[desktop-bridge] upload error:', e);
        }}
    }}

    console.log('[desktop-bridge] injected, sidecar base:', window.__HERMES_SIDECAR_BASE__);
}})();
"#
    )
}

/// 自定义标题栏注入脚本。
///
/// 因为 `decorations: false` 移除了原生窗口边框，需要在前端注入一个
/// 固定定位的标题栏：左侧应用名（拖拽区，支持双击最大化/还原），
/// 右侧最小化 + 关闭按钮。样式使用主题 CSS 变量以匹配深色主题。
///
/// 布局补偿：body 增加 padding-top: 36px，`.app` 高度调整为 calc(100vh - 36px)。
const TITLEBAR_SCRIPT: &str = r#"(function() {
    function injectTitlebar() {
        if (document.getElementById('__hermes_titlebar__')) return;
        if (!document.body) {
            // body 尚未就绪，等待 DOMContentLoaded 后重试
            document.addEventListener('DOMContentLoaded', injectTitlebar);
            return;
        }

        // ---------- CSS ----------
        var style = document.createElement('style');
        style.id = '__hermes_titlebar_css__';
        style.textContent = `
            html, body { box-sizing: border-box !important; }
            body { padding-top: 36px !important; }
            .app { height: calc(100vh - 36px) !important; }

            #__hermes_titlebar__ {
                position: fixed;
                top: 0; left: 0; right: 0;
                height: 36px;
                background: var(--bg-secondary, #161b22);
                border-bottom: 1px solid var(--border, #21262d);
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 0 0 0 12px;
                z-index: 99999;
                user-select: none;
                -webkit-user-select: none;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                font-size: 12px;
                color: var(--text-muted, #5a6473);
            }
            .__htb_drag__ {
                flex: 1;
                height: 100%;
                display: flex;
                align-items: center;
                cursor: default;
            }
            .__htb_title__ {
                font-weight: 500;
                letter-spacing: 0.3px;
                pointer-events: none;
            }
            .__htb_buttons__ {
                display: flex;
                height: 100%;
            }
            .__htb_btn__ {
                width: 46px;
                height: 100%;
                border: none;
                background: transparent;
                color: var(--text-muted, #5a6473);
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: background 0.15s, color 0.15s;
            }
            .__htb_btn__:hover {
                background: var(--bg-hover, #2a313c);
                color: var(--text-primary, #e6edf3);
            }
            .__htb_btn_close__:hover {
                background: #e81123;
                color: #ffffff;
            }
            .__htb_btn__ svg {
                width: 10px;
                height: 10px;
            }
        `;
        document.head.appendChild(style);

        // ---------- DOM ----------
        var bar = document.createElement('div');
        bar.id = '__hermes_titlebar__';
        bar.innerHTML = `
            <div class="__htb_drag__">
                <span class="__htb_title__">Hermes Lite</span>
            </div>
            <div class="__htb_buttons__">
                <button class="__htb_btn__" id="__htb_min__" title="最小化" aria-label="最小化">
                    <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5">
                        <line x1="1" y1="5" x2="9" y2="5"/>
                    </svg>
                </button>
                <button class="__htb_btn__ __htb_btn_close__" id="__htb_close__" title="关闭" aria-label="关闭">
                    <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5">
                        <line x1="1.5" y1="1.5" x2="8.5" y2="8.5"/>
                        <line x1="8.5" y1="1.5" x2="1.5" y2="8.5"/>
                    </svg>
                </button>
            </div>
        `;
        document.body.appendChild(bar);

        // ---------- 事件 ----------
        var __TAURI__ = window.__TAURI__;
        var invoke = __TAURI__ && (__TAURI__.invoke || (__TAURI__.core && __TAURI__.core.invoke));

        // 拖拽移动窗口（仅左键）
        var dragEl = bar.querySelector('.__htb_drag__');
        if (__TAURI__ && __TAURI__.window) {
            var getWin = __TAURI__.window.getCurrentWindow || __TAURI__.window.getCurrent;
            if (getWin) {
                var w = getWin.call(__TAURI__.window);
                dragEl.addEventListener('mousedown', function(e) {
                    if (e.button === 0 && w.startDragging) w.startDragging();
                });
                // 双击最大化/还原
                dragEl.addEventListener('dblclick', function() {
                    if (!w.isMaximized) return;
                    w.isMaximized().then(function(max) {
                        if (max) { if (w.unmaximize) w.unmaximize(); }
                        else { if (w.maximize) w.maximize(); }
                    }).catch(function(){});
                });
            }
        }

        // 最小化按钮：调用 minimize_window 命令（最小化到任务栏）
        var minBtn = document.getElementById('__htb_min__');
        if (minBtn && invoke) {
            minBtn.addEventListener('click', function() {
                invoke('minimize_window').catch(function(e) {
                    console.error('[desktop-bridge] minimize_window failed:', e);
                });
            });
        }

        // 关闭按钮：调用 minimize_to_tray 命令（隐藏到托盘）
        var closeBtn = document.getElementById('__htb_close__');
        if (closeBtn && invoke) {
            closeBtn.addEventListener('click', function() {
                invoke('minimize_to_tray').catch(function(e) {
                    console.error('[desktop-bridge] minimize_to_tray failed:', e);
                });
            });
        }

        console.log('[desktop-bridge] titlebar injected');
    }
    injectTitlebar();
})();
"#;

pub fn setup_window(window: &WebviewWindow, port: u16) {
    let script = init_script(port);
    if let Err(e) = window.eval(&script) {
        log::error!("bridge: inject init_script failed: {}", e);
    } else {
        log::info!("bridge: init_script injected for port {}", port);
    }

    // 自定义标题栏：decorations:false 移除了原生窗口边框，需要前端注入
    // 标题栏提供拖拽区 + 最小化 + 关闭按钮
    if let Err(e) = window.eval(TITLEBAR_SCRIPT) {
        log::error!("bridge: inject titlebar script failed: {}", e);
    } else {
        log::info!("bridge: titlebar script injected");
    }
}

/// 页面导航/刷新后重新注入 bridge 脚本。
///
/// `setup_window` 仅在启动时调用一次，`window.location.reload()`
/// 会销毁所有注入的 DOM 与监听器。`tauri::Builder::on_page_load` 回调
/// 在每次页面加载完成时触发此函数，确保 titlebar 与 init_script 跨刷新持续存在。
pub fn reinject(webview: &Webview, port: u16) {
    let script = init_script(port);
    if let Err(e) = webview.eval(&script) {
        log::warn!("bridge: re-inject init_script failed: {}", e);
    } else {
        log::info!("bridge: init_script re-injected for port {}", port);
    }
    if let Err(e) = webview.eval(TITLEBAR_SCRIPT) {
        log::warn!("bridge: re-inject titlebar failed: {}", e);
    } else {
        log::info!("bridge: titlebar re-injected");
    }
}
