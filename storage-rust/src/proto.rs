//! P-4 帧读写:JSON Lines over stdio,UTF-8 显式字节写出(Rust 无 Windows 编码坑)。
use serde_json::{json, Value};
use std::io::{BufRead, Write};

pub const PROTOCOL_NAME: &str = "storage-stdio";
pub const PROTOCOL_VERSION: &str = "0.1.0";

pub fn serve(conn: rusqlite::Connection) -> Result<(), String> {
    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    let mut backend = crate::dispatch::Backend { conn };
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => return Err(format!("读取 stdin 失败: {e}")),
        };
        if line.trim().is_empty() {
            continue;
        }
        let req: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                write_frame(&mut out, &json!({"id": Value::Null, "ok": false,
                    "e": format!("非 JSON 帧: {e}")}))?;
                continue;
            }
        };
        let id = req.get("id").cloned().unwrap_or(Value::Null);
        match req.get("op").and_then(Value::as_str) {
            Some("hello") => write_frame(&mut out, &json!({
                "id": id, "ok": true,
                "r": {"protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            }))?,
            Some("bye") => {
                write_frame(&mut out, &json!({"id": id, "ok": true, "r": Value::Null}))?;
                break;
            }
            Some(op) => {
                let resp = crate::dispatch::handle(&mut backend, id, op, req.get("p"));
                write_frame(&mut out, &resp)?;
            }
            None => write_frame(&mut out, &json!({"id": id, "ok": false,
                "e": "帧缺少 op 字段"}))?,
        }
    }
    Ok(())
}

fn write_frame(out: &mut impl Write, v: &Value) -> Result<(), String> {
    let mut buf = serde_json::to_vec(v).map_err(|e| e.to_string())?;
    buf.push(b'\n');
    out.write_all(&buf).and_then(|_| out.flush())
        .map_err(|e| format!("写 stdout 失败: {e}"))
}
