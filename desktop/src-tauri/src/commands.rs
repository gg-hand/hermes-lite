//! commands.rs — Tauri IPC 命令
//!
//! 前端通过 `window.__TAURI__.invoke('cmd_name', { args })` 调用。
//! 所有命令在 main.rs 中通过 `tauri::generate_handler![...]` 注册。

use crate::credstore;
use crate::sidecar::SidecarHandle;
use std::path::PathBuf;
use tauri::{State, WebviewWindow};
use tokio::sync::Mutex as AsyncMutex;

pub struct AppState {
    pub sidecar: AsyncMutex<Option<SidecarHandle>>,
    pub port: u16,
    pub data_dir: PathBuf,
    pub hermes_root: PathBuf,
    pub python_path: String,
}

#[tauri::command]
pub fn get_sidecar_port(state: State<'_, AppState>) -> u16 {
    state.port
}

#[tauri::command]
pub fn get_data_dir(state: State<'_, AppState>) -> String {
    state.data_dir.to_string_lossy().to_string()
}

#[tauri::command]
pub async fn restart_sidecar(
    state: State<'_, AppState>,
) -> Result<(), String> {
    let mut guard = state.sidecar.lock().await;
    if let Some(mut old) = guard.take() {
        old.kill();
    }
    let mut new_handle = SidecarHandle::spawn(
        state.port,
        &state.data_dir,
        &state.hermes_root,
        &state.python_path,
    )
    .map_err(|e| format!("spawn failed: {}", e))?;
    new_handle
        .wait_for_ready(state.port, std::time::Duration::from_secs(30))
        .await
        .map_err(|e| format!("health check failed: {}", e))?;
    *guard = Some(new_handle);
    Ok(())
}

#[tauri::command]
pub async fn open_external(app: tauri::AppHandle, url: String) -> Result<(), String> {
    use tauri_plugin_shell::ShellExt;
    if url.is_empty() {
        return Err("url is empty".to_string());
    }
    app.shell().open(url, None).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn reveal_in_explorer(path: String) -> Result<(), String> {
    #[cfg(windows)]
    {
        std::process::Command::new("explorer")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("explorer failed: {}", e))?;
    }
    Ok(())
}

#[tauri::command]
pub fn save_api_key(key_name: String, value: String) -> Result<(), String> {
    credstore::save_key(&key_name, &value).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn load_api_key(key_name: String) -> Result<Option<String>, String> {
    credstore::load_key(&key_name).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn delete_api_key(key_name: String) -> Result<(), String> {
    credstore::delete_key(&key_name).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_app_version(app: tauri::AppHandle) -> String {
    app.package_info().version.to_string()
}

#[tauri::command]
pub fn quit_app(app: tauri::AppHandle) {
    log::info!("commands: quit_app requested");
    app.exit(0);
}

#[tauri::command]
pub fn show_window(window: WebviewWindow) -> Result<(), String> {
    window
        .show()
        .map_err(|e| format!("show failed: {}", e))?;
    window
        .set_focus()
        .map_err(|e| format!("set_focus failed: {}", e))?;
    Ok(())
}

#[tauri::command]
pub fn minimize_to_tray(window: WebviewWindow) -> Result<(), String> {
    window
        .hide()
        .map_err(|e| format!("hide failed: {}", e))?;
    Ok(())
}

#[tauri::command]
pub fn minimize_window(window: WebviewWindow) -> Result<(), String> {
    window
        .minimize()
        .map_err(|e| format!("minimize failed: {}", e))?;
    Ok(())
}
