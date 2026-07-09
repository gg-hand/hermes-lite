//! credstore.rs — Windows Credential Manager 封装（via keyring crate）
//!
//! 提供 API 密钥的 CRUD：save / load / delete。
//! service 名固定为 `com.hermeslite.desktop.apikeys`，key 命名与 config.yaml 的 model 配置对应
//! （openai / anthropic / deepseek 等）。

use anyhow::Result;
use keyring::Entry;

const SERVICE_NAME: &str = "com.hermeslite.desktop.apikeys";

pub fn save_key(key_name: &str, value: &str) -> Result<()> {
    let entry = Entry::new(SERVICE_NAME, key_name)?;
    entry.set_password(value)?;
    log::info!("credstore: saved key '{}'", key_name);
    Ok(())
}

pub fn load_key(key_name: &str) -> Result<Option<String>> {
    let entry = Entry::new(SERVICE_NAME, key_name)?;
    match entry.get_password() {
        Ok(v) => {
            log::debug!("credstore: loaded key '{}'", key_name);
            Ok(Some(v))
        }
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(anyhow::anyhow!("load key '{}' failed: {}", key_name, e)),
    }
}

pub fn delete_key(key_name: &str) -> Result<()> {
    let entry = Entry::new(SERVICE_NAME, key_name)?;
    match entry.delete_credential() {
        Ok(()) => {
            log::info!("credstore: deleted key '{}'", key_name);
            Ok(())
        }
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(anyhow::anyhow!("delete key '{}' failed: {}", key_name, e)),
    }
}
