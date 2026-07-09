//! installer.rs — OCR 依赖下载占位（v0.2 实现）
//!
//! MVP 不实现实际下载逻辑，仅提供 stub 接口。
//! v0.2 将实现 PaddlePaddle/PaddleOCR（约 2GB）的可选下载与安装。

use anyhow::Result;

pub fn is_ocr_installed() -> bool {
    false
}

pub async fn download_ocr(_progress: impl Fn(u8)) -> Result<()> {
    Err(anyhow::anyhow!(
        "OCR download not implemented in v0.1, deferred to v0.2"
    ))
}
