//! main.rs — Hermes Lite 桌面端入口
//!
//! 组装所有模块：sidecar + bridge + commands + tray + shortcuts + single-instance。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod commands;
mod credstore;
mod installer;
mod port_selector;
mod sidecar;

use commands::AppState;
use std::path::PathBuf;
use std::time::Duration;
use tauri::Manager;
use tokio::sync::Mutex as AsyncMutex;

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format_timestamp_secs()
        .init();

    log::info!("hermes-lite-desktop starting up (v{})", env!("CARGO_PKG_VERSION"));

    let hermes_root = resolve_hermes_root();
    log::info!("hermes_root = {:?}", hermes_root);

    let data_dir = resolve_data_dir();
    log::info!("data_dir = {:?}", data_dir);

    #[cfg(debug_assertions)]
    let port: u16 = 18000;
    #[cfg(not(debug_assertions))]
    let port: u16 = port_selector::select_port(&data_dir).expect("port selection failed");
    log::info!("sidecar port = {}", port);

    let python_path = resolve_python_path(&hermes_root);
    log::info!("python_path = {}", python_path);

    bootstrap_config(&hermes_root);

    let sidecar = if is_port_serving(port) {
        log::info!(
            "sidecar: port {} already in use — assuming external sidecar, skipping spawn",
            port
        );
        None
    } else {
        match sidecar::SidecarHandle::spawn(
            port,
            &data_dir,
            &hermes_root,
            &python_path,
        ) {
            Ok(h) => Some(h),
            Err(e) => {
                log::warn!(
                    "sidecar spawn failed: {} — continuing without sidecar (pages may fail to load)",
                    e
                );
                None
            }
        }
    };

    let app_state = AppState {
        sidecar: AsyncMutex::new(sidecar),
        port,
        data_dir: data_dir.clone(),
        hermes_root: hermes_root.clone(),
        python_path: python_path.clone(),
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            log::info!("single-instance: second instance launched, focusing main window");
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
                let _ = w.unminimize();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::new().build())
        .plugin(tauri_plugin_store::Builder::new().build())
        .manage(app_state)
        .invoke_handler(tauri::generate_handler![
            commands::get_sidecar_port,
            commands::get_data_dir,
            commands::restart_sidecar,
            commands::open_external,
            commands::reveal_in_explorer,
            commands::save_api_key,
            commands::load_api_key,
            commands::delete_api_key,
            commands::get_app_version,
            commands::quit_app,
            commands::show_window,
            commands::minimize_to_tray,
            commands::minimize_window,
            commands::toggle_maximize,
        ])
        // 页面刷新/导航后注入的脚本会被销毁。每次 PageLoadEvent::Finished
        // 时重新注入 titlebar 与 init_script，确保 chat-settings.js 的
        // window.location.reload() 后标题栏仍在。同时当 sidecar 的 chat 页
        // 加载完成时显示主窗口（窗口启动时隐藏，避免介绍页闪现）。
        .on_page_load(move |webview, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                let url = payload.url();
                log::info!("on_page_load: page finished, url={}", url);
                // 只对 main 窗口注入 bridge（titlebar + fetch 改写）
                // settings 窗口有原生标题栏，不需要注入
                if webview.window().label() == "main" {
                    bridge::reinject(webview, port);
                }
                let needle = format!("127.0.0.1:{}", port);
                if url.as_str().contains(needle.as_str()) {
                    let _ = webview.window().show();
                    let _ = webview.window().set_focus();
                }
            }
        })
        .setup(move |app| {
            let main_window = app.get_webview_window("main").expect("main window not found");

            bridge::setup_window(&main_window, port);

            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                log::info!("setup: waiting for sidecar ready...");
                let state = app_handle.state::<AppState>();
                let mut guard = state.sidecar.lock().await;
                let mut sidecar_ready = true;
                if let Some(ref mut sc) = *guard {
                    match sc.wait_for_ready(port, Duration::from_secs(60)).await {
                        Ok(()) => log::info!("setup: sidecar ready, navigating to chat page"),
                        Err(e) => {
                            log::error!("setup: sidecar not ready: {}", e);
                            sidecar_ready = false;
                        }
                    }
                }
                drop(guard);

                if let Some(w) = app_handle.get_webview_window("main") {
                    if sidecar_ready {
                        // 导航到 sidecar：同源访问避免 CORS，HERMES_DESKTOP=1
                        // 在根路径直接返回 chat.html（跳过介绍页）。
                        // 窗口保持隐藏，由 on_page_load 在 chat.html 加载完成后显示。
                        let url = format!("http://127.0.0.1:{}/", port);
                        let js = format!("window.location.replace({:?});", url);
                        if let Err(e) = w.eval(&js) {
                            log::error!("setup: navigate to sidecar failed: {}", e);
                        }
                        // 兜底：3 秒后仍未显示则强制显示，防止 on_page_load 未触发的边界情况
                        let w_fallback = w.clone();
                        tauri::async_runtime::spawn(async move {
                            tokio::time::sleep(Duration::from_secs(3)).await;
                            let _ = w_fallback.show();
                            let _ = w_fallback.set_focus();
                        });
                    } else {
                        // sidecar 未就绪：仅显示主窗口，不弹窗。
                        // 桌面端进入即是聊天页，设置通过聊天页内的模态框完成。
                        let _ = w.show();
                        let _ = w.set_focus();
                        let w_fallback = w.clone();
                        tauri::async_runtime::spawn(async move {
                            tokio::time::sleep(Duration::from_secs(3)).await;
                            let _ = w_fallback.show();
                            let _ = w_fallback.set_focus();
                        });
                    }
                }
            });

            setup_tray(app)?;

            log::info!("setup: complete");
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                log::info!("window: close requested, minimizing to tray");
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn is_port_serving(port: u16) -> bool {
    use std::net::{SocketAddr, TcpStream};
    let addr: SocketAddr = format!("127.0.0.1:{}", port)
        .parse()
        .expect("valid socket addr");
    TcpStream::connect_timeout(&addr, Duration::from_secs(1)).is_ok()
}

fn resolve_data_dir() -> PathBuf {
    if let Ok(d) = std::env::var("HERMES_DATA_DIR") {
        let p = PathBuf::from(d);
        let _ = std::fs::create_dir_all(&p);
        return p;
    }
    let appdata = std::env::var("APPDATA")
        .or_else(|_| std::env::var("HOME"))
        .expect("neither APPDATA nor HOME set");
    let p = PathBuf::from(appdata).join("hermes-lite");
    let _ = std::fs::create_dir_all(&p);
    p
}

/// 运行时解析 hermes_root，避免编译时硬编码 CARGO_MANIFEST_DIR。
/// 优先级：HERMES_ROOT 环境变量 > debug 用 CARGO_MANIFEST_DIR > release 用 current_exe().parent()。
fn resolve_hermes_root() -> PathBuf {
    if let Ok(d) = std::env::var("HERMES_ROOT") {
        let p = PathBuf::from(d);
        if p.is_dir() {
            return p;
        }
    }
    #[cfg(debug_assertions)]
    {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        return manifest_dir
            .parent()
            .and_then(|p| p.parent())
            .map(|p| p.to_path_buf())
            .expect("cannot resolve hermes_root from CARGO_MANIFEST_DIR");
    }
    #[cfg(not(debug_assertions))]
    {
        std::env::current_exe()
            .expect("cannot get current_exe")
            .parent()
            .map(|p| p.to_path_buf())
            .expect("cannot resolve exe parent")
    }
}

/// 运行时解析 python 解释器路径。
/// 优先级：HERMES_PYTHON_PATH 环境变量 > debug 用系统 "python" > release 用嵌入式 python/python.exe。
fn resolve_python_path(hermes_root: &std::path::Path) -> String {
    if let Ok(p) = std::env::var("HERMES_PYTHON_PATH") {
        return p;
    }
    #[cfg(debug_assertions)]
    {
        "python".to_string()
    }
    #[cfg(not(debug_assertions))]
    {
        let embedded = hermes_root.join("python").join("python.exe");
        if embedded.exists() {
            embedded.to_string_lossy().to_string()
        } else {
            log::warn!(
                "embedded python not found at {:?}, falling back to system python",
                embedded
            );
            "python".to_string()
        }
    }
}

/// 首次启动时从 config.yaml.example 复制 config.yaml（若不存在）。
fn bootstrap_config(hermes_root: &std::path::Path) {
    let config_path = hermes_root.join("config.yaml");
    if config_path.exists() {
        return;
    }
    let example_path = hermes_root.join("config.yaml.example");
    if example_path.exists() {
        match std::fs::copy(&example_path, &config_path) {
            Ok(_) => log::info!("config: copied example to {:?}", config_path),
            Err(e) => log::warn!("config: failed to copy example: {}", e),
        }
    } else {
        log::warn!(
            "config: neither config.yaml nor config.yaml.example found in {:?}",
            hermes_root
        );
    }
}

fn setup_tray(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::TrayIconBuilder;

    let show_item = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

    let _tray = TrayIconBuilder::with_id("main-tray")
        .icon(app.default_window_icon().unwrap().clone())
        .tooltip("Hermes Lite")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                    let _ = w.unminimize();
                }
            }
            "quit" => {
                log::info!("tray: quit requested");
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;

    log::info!("tray: setup complete");
    Ok(())
}
