//! op 分发 + 参数校验(设计 §4.2):任何参数/SQL 错误 → ok:false,进程不崩。
use serde_json::{json, Value};
use std::panic::{catch_unwind, AssertUnwindSafe};

pub struct Backend {
    pub conn: rusqlite::Connection,
}

pub fn handle(backend: &mut Backend, id: Value, op: &str, p: Option<&Value>) -> Value {
    let p = p.cloned().unwrap_or_else(|| json!({}));
    let outcome = catch_unwind(AssertUnwindSafe(|| run(backend, op, &p)));
    match outcome {
        Ok(Ok(r)) => json!({"id": id, "ok": true, "r": r}),
        Ok(Err(e)) => json!({"id": id, "ok": false, "e": e}),
        Err(_) => json!({"id": id, "ok": false, "e": format!("op {op:?} 处理发生 panic")}),
    }
}

fn run(b: &mut Backend, op: &str, p: &Value) -> Result<Value, String> {
    match op {
        "write" => crate::docs::write(&mut b.conn, p),
        "read" => crate::docs::read(&b.conn, p),
        "query" => crate::docs::query(&b.conn, p),
        "delete" => crate::docs::delete(&b.conn, p),
        "ensure_session" => crate::history::ensure_session(&b.conn, p),
        "log_message" => crate::history::log_message(&b.conn, p),
        "log_messages" => crate::history::log_messages(&b.conn, p),
        "get_session_messages" => crate::history::get_session_messages(&b.conn, p),
        "update_session_title" => crate::history::update_session_title(&b.conn, p),
        "get_session_title" => crate::history::get_session_title(&b.conn, p),
        "search_messages" => crate::history::search_messages(&b.conn, p),
        other => Err(format!("未知 op: {other:?}")),
    }
}

/// 取必填字符串参数(空串允许,空 kind 由 validate_kind 拦)。
pub fn take_str(p: &Value, key: &str) -> Result<String, String> {
    match p.get(key) {
        Some(Value::String(s)) => Ok(s.clone()),
        _ => Err(format!("参数 {key} 缺失或非字符串")),
    }
}

pub fn take_opt_str(p: &Value, key: &str) -> Result<Option<String>, String> {
    match p.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(format!("参数 {key} 必须是字符串或 null")),
    }
}

pub fn take_opt_i64(p: &Value, key: &str) -> Result<Option<i64>, String> {
    match p.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) => n.as_i64().map(Some)
            .ok_or_else(|| format!("参数 {key} 必须是整数")),
        Some(_) => Err(format!("参数 {key} 必须是整数或 null")),
    }
}
