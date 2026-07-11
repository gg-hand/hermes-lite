//! port_selector.rs — 随机端口选择 + 持久化
//!
//! 范围 18000-18999。读 `data_dir/sidecar_port` 文件，已设置且可用则直接用；
//! 否则随机抽取，被占则 +1 探测，最多试 1000 次。最终写回 sidecar_port 文件。
//! 端口通过 HERMES_PORT 环境变量传递给 sidecar，此处持久化仅为复用偏好。
//!
//! dev 模式（cfg(debug_assertions)）由 main.rs 直接固定 18000，不调用本模块。

use anyhow::Result;
use rand::Rng;
use std::net::TcpListener;
use std::path::Path;

const PORT_MIN: u16 = 18000;
const PORT_MAX: u16 = 18999;
const MAX_PROBES: u32 = 1000;

pub fn select_port(data_dir: &Path) -> Result<u16> {
    let port_file = data_dir.join("sidecar_port");

    let existing = read_port_from_file(&port_file)?;
    if let Some(port) = existing {
        if is_port_available(port) {
            log::info!("port_selector: reuse persisted port {}", port);
            return Ok(port);
        }
        log::warn!("port_selector: persisted port {} occupied, re-selecting", port);
    }

    let mut rng = rand::thread_rng();
    let mut port: u16 = rng.gen_range(PORT_MIN..=PORT_MAX);
    for _ in 0..MAX_PROBES {
        if is_port_available(port) {
            write_port_to_file(&port_file, port)?;
            log::info!("port_selector: selected & persisted port {}", port);
            return Ok(port);
        }
        port = if port >= PORT_MAX { PORT_MIN } else { port + 1 };
    }
    Err(anyhow::anyhow!(
        "no available port in {}-{} after {} probes",
        PORT_MIN,
        PORT_MAX,
        MAX_PROBES
    ))
}

fn is_port_available(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

fn read_port_from_file(path: &Path) -> Result<Option<u16>> {
    if !path.exists() {
        return Ok(None);
    }
    let content = std::fs::read_to_string(path)?;
    let port = content.trim().parse::<u16>().ok();
    Ok(port)
}

fn write_port_to_file(path: &Path, port: u16) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, port.to_string())?;
    Ok(())
}
