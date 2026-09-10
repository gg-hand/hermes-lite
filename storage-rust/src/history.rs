//! sessions/messages 表:HistoryStore/MessageStore 语义(设计 §4.2)。
//! schema 与 core/history.py 兼容(新库从零开始,不迁移存量)。
use crate::dispatch::{take_opt_i64, take_opt_str, take_str};
use crate::docs::now_iso;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

pub fn init_tables(conn: &Connection) -> Result<(), String> {
    // 2026-09-10 定案(与 core/history.py 同步,P-4 语义条款⑨):DDL 不声明
    // messages.session_id 的外键约束 —— SQLite 外键默认 OFF,声明不执行属误导;
    // session 存在性由 ensure_session 调用契约保证。
    // 理由写在代码注释而非 SQL 文本内,避免注释字样击穿文本化 schema 检查。
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_call_id TEXT,
            token_count INTEGER DEFAULT 0,
            is_error INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            attachments TEXT,
            message_type TEXT,
            reasoning TEXT,
            content_blocks TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
        "#,
    )
    .map_err(|e| e.to_string())
}

pub fn ensure_session(conn: &Connection, p: &Value) -> Result<Value, String> {
    let session_id = take_str(p, "session_id")?;
    let now = now_iso();
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, created_at, updated_at) VALUES (?1, ?2, ?3)",
        params![session_id, now, now],
    )
    .map_err(|e| e.to_string())?;
    Ok(Value::Null)
}

/// 单条插入(校验 + INSERT + updated_at 同步),不含事务边界——
/// 供 log_message(独立事务)与 log_messages(P-7 批量同事务)复用。
fn insert_message(conn: &Connection, p: &Value) -> Result<(), String> {
    let session_id = take_str(p, "session_id")?;
    let role = take_str(p, "role")?;
    let content = take_str(p, "content")?;
    let tool_name = take_opt_str(p, "tool_name")?;
    let tool_call_id = take_opt_str(p, "tool_call_id")?;
    let token_count = take_opt_i64(p, "token_count")?.unwrap_or(0);
    let is_error = matches!(p.get("is_error"), Some(Value::Bool(true)));
    let reasoning = take_opt_str(p, "reasoning")?;
    let message_type = take_opt_str(p, "message_type")?;
    // 对齐 core:content_blocks 空数组按 NULL 落(旧行回退纯文本语义)
    let blocks = match p.get("content_blocks") {
        Some(Value::Array(a)) if !a.is_empty() => Some(Value::Array(a.clone()).to_string()),
        _ => None,
    };
    let now = now_iso();
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, tool_call_id, \
         token_count, is_error, created_at, reasoning, content_blocks, message_type) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        params![
            session_id, role, content, tool_name, tool_call_id,
            token_count, if is_error { 1 } else { 0 }, now,
            reasoning, blocks, message_type,
        ],
    )
    .map_err(|e| e.to_string())?;
    conn.execute(
        "UPDATE sessions SET updated_at = ?1 WHERE id = ?2",
        params![now, session_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

pub fn log_message(conn: &Connection, p: &Value) -> Result<Value, String> {
    insert_message(conn, p)?;
    Ok(Value::Null)
}

/// P-7 批量消息写:单事务原子(P-4 条款③范式)——任一元素非法/SQL 错
/// 则整体回滚返回 ok:false,不产生部分写入。
pub fn log_messages(conn: &Connection, p: &Value) -> Result<Value, String> {
    let msgs = match p.get("messages") {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return Err("参数 messages 缺失或为空数组/非数组".into()),
    };
    // dispatch 为单线程串行处理(P-4 条款⑥),unchecked_transaction 的借用
    // 放宽在并发化之前是安全的;若未来引入多线程须改回 transaction()。
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
    for m in msgs {
        insert_message(&tx, m)?;
    }
    tx.commit().map_err(|e| e.to_string())?;
    Ok(Value::Null)
}

/// 与 core SQL 同构:limit 取最近 N 条(子查询倒序取后正序返回),
/// before_id 游标向上翻页(id < before_id)。
pub fn get_session_messages(conn: &Connection, p: &Value) -> Result<Value, String> {
    let session_id = take_str(p, "session_id")?;
    let limit = take_opt_i64(p, "limit")?;
    let before_id = take_opt_i64(p, "before_id")?;
    let mut sql = String::from("SELECT * FROM (SELECT * FROM messages WHERE session_id = ?1");
    if before_id.is_some() {
        sql.push_str(" AND id < ?2");
    }
    sql.push_str(" ORDER BY id DESC");
    if limit.is_some() {
        sql.push_str(if before_id.is_some() { " LIMIT ?3" } else { " LIMIT ?2" });
    }
    sql.push_str(") ORDER BY id ASC");
    let mut pv: Vec<rusqlite::types::Value> = vec![session_id.into()];
    if let Some(b) = before_id {
        pv.push(b.into());
    }
    if let Some(l) = limit {
        pv.push(l.into());
    }
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(pv.iter()), row_to_json)
        .map_err(|e| e.to_string())?;
    let mut out: Vec<Value> = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| e.to_string())?);
    }
    Ok(Value::Array(out))
}

pub fn update_session_title(conn: &Connection, p: &Value) -> Result<Value, String> {
    let session_id = take_str(p, "session_id")?;
    let title = take_str(p, "title")?;
    conn.execute(
        "UPDATE sessions SET title = ?1 WHERE id = ?2",
        params![title, session_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(Value::Null)
}

pub fn get_session_title(conn: &Connection, p: &Value) -> Result<Value, String> {
    let session_id = take_str(p, "session_id")?;
    // .optional():会话不存在 → Ok(None) → r:null(对齐 core 语义)
    let title: Option<String> = conn
        .query_row(
            "SELECT title FROM sessions WHERE id = ?1",
            params![session_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    Ok(match title {
        Some(t) => json!(t),
        None => Value::Null,
    })
}

/// 子串匹配(P-4 条款⑤契约最低要求;FTS5 留作后续观察项):
/// LIKE 特殊字符 % _ \ 转义防通配符注入,按 created_at 倒序取 limit。
pub fn search_messages(conn: &Connection, p: &Value) -> Result<Value, String> {
    let keyword = take_str(p, "keyword")?;
    let session_id = take_opt_str(p, "session_id")?;
    let limit = take_opt_i64(p, "limit")?.unwrap_or(20);
    if limit < 1 {
        return Err(format!("limit 必须是正整数,实际 {limit}"));
    }
    let escaped = keyword
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_");
    let mut sql = String::from(
        "SELECT m.* FROM messages m WHERE m.content LIKE ?1 ESCAPE '\\'",
    );
    let mut pv: Vec<rusqlite::types::Value> = vec![format!("%{escaped}%").into()];
    if let Some(s) = &session_id {
        sql.push_str(" AND m.session_id = ?2");
        pv.push(s.clone().into());
    }
    sql.push_str(&format!(" ORDER BY m.created_at DESC LIMIT ?{}", pv.len() + 1));
    pv.push(limit.into());
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(pv.iter()), row_to_json)
        .map_err(|e| e.to_string())?;
    let mut out: Vec<Value> = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| e.to_string())?);
    }
    Ok(Value::Array(out))
}

fn row_to_json(r: &rusqlite::Row) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": r.get::<_, Option<i64>>("id")?,
        "session_id": r.get::<_, Option<String>>("session_id")?,
        "role": r.get::<_, Option<String>>("role")?,
        "content": r.get::<_, Option<String>>("content")?,
        "tool_name": r.get::<_, Option<String>>("tool_name")?,
        "tool_call_id": r.get::<_, Option<String>>("tool_call_id")?,
        "token_count": r.get::<_, Option<i64>>("token_count")?,
        "is_error": r.get::<_, Option<i64>>("is_error")?,
        "created_at": r.get::<_, Option<String>>("created_at")?,
        "attachments": r.get::<_, Option<String>>("attachments")?,
        "message_type": r.get::<_, Option<String>>("message_type")?,
        "reasoning": r.get::<_, Option<String>>("reasoning")?,
        "content_blocks": r.get::<_, Option<String>>("content_blocks")?,
    }))
}
