//! sidecar.rs — Python 子进程生命周期管理
//!
//! 职责：spawn `python -m src.server`、健康检查、优雅关闭。
//! 工作目录设为 hermes_root（让 src/ 包可被找到），数据目录通过 HERMES_DATA_DIR 环境变量传递。

use anyhow::{Context, Result};
use std::path::Path;
use std::process::{Child, Stdio};
use std::time::{Duration, Instant};

pub struct SidecarHandle {
    child: Option<Child>,
    pid: u32,
}

impl SidecarHandle {
    pub fn spawn(
        port: u16,
        data_dir: &Path,
        hermes_root: &Path,
        python_path: &str,
    ) -> Result<Self> {
        std::fs::create_dir_all(data_dir)
            .with_context(|| format!("create data_dir failed: {:?}", data_dir))?;

        let log_path = data_dir.join("sidecar.log");
        let log_file = std::fs::File::create(&log_path)
            .with_context(|| format!("create sidecar.log failed: {:?}", log_path))?;
        let stderr_file = log_file.try_clone()?;

        log::info!(
            "sidecar: spawning python={} port={} cwd={:?} data_dir={:?}",
            python_path,
            port,
            hermes_root,
            data_dir
        );

        let python_home = std::path::Path::new(python_path)
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| hermes_root.join("python"));

        let mut cmd = std::process::Command::new(python_path);
        cmd.arg("-m")
            .arg("src.server")
            .current_dir(hermes_root)
            .env("HERMES_DESKTOP", "1")
            .env("HERMES_DATA_DIR", data_dir)
            .env("HERMES_ROOT", hermes_root)
            .env("HERMES_PYTHON_PATH", python_path)
            .env("HERMES_PORT", port.to_string())
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONHOME", &python_home)
            .env("PYTHONPATH", python_home.join("Lib").join("site-packages"))
            .stdout(Stdio::from(log_file))
            .stderr(Stdio::from(stderr_file))
            .stdin(Stdio::null());

        // Windows: 抑制控制台窗口闪现（python.exe 默认会弹黑框）
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let child = cmd
            .spawn()
            .with_context(|| format!("spawn python failed: {}", python_path))?;
        let pid = child.id();
        log::info!("sidecar: spawned pid={} port={}", pid, port);

        Ok(Self {
            child: Some(child),
            pid,
        })
    }

    pub async fn wait_for_ready(&mut self, port: u16, timeout: Duration) -> Result<()> {
        let url = format!("http://127.0.0.1:{}/", port);
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()?;
        let start = Instant::now();

        log::info!("sidecar: waiting for {} (timeout {:?})", url, timeout);

        while start.elapsed() < timeout {
            if let Ok(resp) = client.get(&url).send().await {
                if resp.status().is_success() || resp.status().is_redirection() {
                    log::info!(
                        "sidecar: ready after {}ms",
                        start.elapsed().as_millis()
                    );
                    return Ok(());
                }
            }
            if let Some(child) = self.child.as_mut() {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        return Err(anyhow::anyhow!(
                            "sidecar process exited prematurely: {}",
                            status
                        ));
                    }
                    Ok(None) => {}
                    Err(_) => {}
                }
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        Err(anyhow::anyhow!(
            "sidecar health check timeout after {:?}",
            timeout
        ))
    }

    pub fn kill(&mut self) {
        if let Some(child) = self.child.as_mut() {
            let pid = self.pid;
            log::info!("sidecar: killing pid={}", pid);

            #[cfg(windows)]
            {
                let _ = std::process::Command::new("taskkill")
                    .args(["/PID", &pid.to_string(), "/T", "/F"])
                    .output();
            }
            #[cfg(not(windows))]
            {
                let _ = child.kill();
            }
            let _ = child.wait();
            self.child = None;
        }
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }
}

impl Drop for SidecarHandle {
    fn drop(&mut self) {
        self.kill();
    }
}
