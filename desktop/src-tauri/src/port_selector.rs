//! port_selector.rs — 随机端口选择 + 持久化
//!
//! 范围 18000-18999。读 `data_dir/config.yaml` 的 `server.port`，已设置且可用则直接用；
//! 否则随机抽取，被占则 +1 探测，最多试 1000 次。最终写回 config.yaml。
//!
//! dev 模式（cfg(debug_assertions)）由 main.rs 直接固定 18000，不调用本模块。

use anyhow::Result;
use rand::Rng;
use serde_json::Value;
use std::net::TcpListener;
use std::path::Path;

const PORT_MIN: u16 = 18000;
const PORT_MAX: u16 = 18999;
const MAX_PROBES: u32 = 1000;

pub fn select_port(data_dir: &Path) -> Result<u16> {
    let config_path = data_dir.join("config.yaml");

    let existing = read_port_from_config(&config_path)?;
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
            write_port_to_config(&config_path, port)?;
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

fn read_port_from_config(config_path: &Path) -> Result<Option<u16>> {
    if !config_path.exists() {
        return Ok(None);
    }
    let content = std::fs::read_to_string(config_path)?;
    let v: Value = serde_yaml::from_str(&content)?;
    Ok(v.get("server")
        .and_then(|s| s.get("port"))
        .and_then(|p| p.as_u64())
        .map(|p| p as u16))
}

fn write_port_to_config(config_path: &Path, port: u16) -> Result<()> {
    let content = if config_path.exists() {
        std::fs::read_to_string(config_path)?
    } else {
        String::new()
    };

    let mut root: Value = if content.trim().is_empty() {
        Value::Object(serde_json::Map::new())
    } else {
        serde_yaml::from_str(&content)?
    };

    let server = root
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("config.yaml root is not a mapping"))?
        .entry("server".to_string())
        .or_insert_with(|| Value::Object(serde_json::Map::new()));
    let server_obj = server
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("config.yaml 'server' is not a mapping"))?;
    server_obj.insert("port".to_string(), Value::Number(port.into()));

    let new_content = serde_yaml::to_string(&root)?;
    let tmp = config_path.with_extension("yaml.tmp");
    std::fs::write(&tmp, new_content)?;
    std::fs::rename(&tmp, config_path)?;
    Ok(())
}
