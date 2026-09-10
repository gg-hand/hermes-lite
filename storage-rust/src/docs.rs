//! doc_* 表:StorageProvider 语义(设计 §4.2)。
//! read 对不存在的 doc_id 返回 r:null(对齐 core 契约;query/delete 在表不存在时
//! 与 core 一致:SQLite 报错透传 ok:false)。
use crate::dispatch::{take_opt_i64, take_str};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{Map, Value};

const KIND_CHARS: &str = "abcdefghijklmnopqrstuvwxyz0123456789_.";

/// kind 白名单双重防线的第二道(宿主 _validate_kind 先行,此处兜底)。
pub fn validate_kind(kind: &str) -> Result<(), String> {
    if kind.is_empty() || !kind.chars().all(|c| KIND_CHARS.contains(c)) {
        return Err(format!("非法 kind: {kind:?}(仅允许小写字母/数字/点/下划线)"));
    }
    Ok(())
}

fn table_for(kind: &str) -> String {
    format!("doc_{}", kind.replace('.', "_"))
}

fn ensure_table(conn: &Connection, table: &str) -> Result<(), String> {
    conn.execute_batch(&format!(
        "CREATE TABLE IF NOT EXISTS {table} \
         (doc_id TEXT PRIMARY KEY, doc TEXT NOT NULL, created_at TEXT NOT NULL);"
    ))
    .map_err(|e| e.to_string())
}

pub fn now_iso() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
}

/// 单条/批量统一入口(宿主把单条包成 docs:[doc]):批量同事务原子提交,
/// 响应 r 恒为 doc_id 数组、顺序对应 docs(P-4 条款④)。
pub fn write(conn: &mut Connection, p: &Value) -> Result<Value, String> {
    let kind = take_str(p, "kind")?;
    validate_kind(&kind)?;
    let docs = p.get("docs").and_then(Value::as_array)
        .ok_or("参数 docs 缺失或非数组")?;
    if docs.is_empty() {
        return Err("批量写入 docs 不能为空列表".into());
    }
    let table = table_for(&kind);
    ensure_table(conn, &table)?;
    let now = now_iso();
    let tx = conn.transaction().map_err(|e| e.to_string())?;
    let mut ids: Vec<Value> = Vec::with_capacity(docs.len());
    for d in docs {
        let obj: &Map<String, Value> = d.as_object()
            .ok_or("docs 元素必须是 JSON 对象")?;
        let doc_id = uuid::Uuid::new_v4().simple().to_string();
        let text = Value::Object(obj.clone()).to_string();
        tx.execute(
            &format!("INSERT INTO {table} (doc_id, doc, created_at) VALUES (?1, ?2, ?3)"),
            params![doc_id, text, now],
        )
        .map_err(|e| e.to_string())?;
        ids.push(Value::String(doc_id));
    }
    tx.commit().map_err(|e| e.to_string())?;
    Ok(Value::Array(ids))
}

pub fn read(conn: &Connection, p: &Value) -> Result<Value, String> {
    let kind = take_str(p, "kind")?;
    validate_kind(&kind)?;
    let doc_id = take_str(p, "doc_id")?;
    let table = table_for(&kind);
    // .optional()(OptionalExtension):无行 → Ok(None),对齐 core「不存在返回 None」
    let text: Option<String> = conn
        .query_row(
            &format!("SELECT doc FROM {table} WHERE doc_id = ?1"),
            params![doc_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    match text {
        Some(t) => serde_json::from_str(&t).map_err(|e| e.to_string()),
        None => Ok(Value::Null),
    }
}

/// query:先 filters 过滤(顶层字段精确匹配)后 limit(P-4 语义澄清项),
/// ORDER BY rowid ASC = 写入序(P-4 条款②)。
pub fn query(conn: &Connection, p: &Value) -> Result<Value, String> {
    let kind = take_str(p, "kind")?;
    validate_kind(&kind)?;
    let limit = take_opt_i64(p, "limit")?;
    if let Some(n) = limit {
        if n < 1 {
            return Err(format!("limit 必须是正整数,实际 {n}"));
        }
    }
    let filters = p.get("filters").and_then(Value::as_object)
        .ok_or("参数 filters 缺失或非对象")?;
    let table = table_for(&kind);
    let mut sql = format!("SELECT doc FROM {table}");
    let mut binds: Vec<String> = Vec::new();
    for (k, v) in filters {
        let joiner = if binds.is_empty() { " WHERE" } else { " AND" };
        if v.is_null() {
            // 语义对齐 core(Python d.get(k) == None 命中缺失键):IS NULL 兼容两者
            sql.push_str(&format!("{joiner} json_extract(doc, ?) IS NULL"));
            binds.push(format!("$.{k}"));
        } else {
            sql.push_str(&format!("{joiner} CAST(json_extract(doc, ?) AS TEXT) = ?"));
            binds.push(format!("$.{k}"));
            binds.push(match v {
                Value::String(s) => s.clone(),
                Value::Bool(b) => if *b { "1".into() } else { "0".into() },
                other => other.to_string(),
            });
        }
    }
    match limit {
        Some(n) => sql.push_str(&format!(" ORDER BY rowid ASC LIMIT {n}")),
        None => sql.push_str(" ORDER BY rowid ASC"),
    }
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(binds.iter()), |r| r.get::<_, String>(0))
        .map_err(|e| e.to_string())?;
    let mut out: Vec<Value> = Vec::new();
    for row in rows {
        let text = row.map_err(|e| e.to_string())?;
        out.push(serde_json::from_str(&text).map_err(|e| e.to_string())?);
    }
    Ok(Value::Array(out))
}

pub fn delete(conn: &Connection, p: &Value) -> Result<Value, String> {
    let kind = take_str(p, "kind")?;
    validate_kind(&kind)?;
    let doc_id = take_str(p, "doc_id")?;
    let table = table_for(&kind);
    conn.execute(
        &format!("DELETE FROM {table} WHERE doc_id = ?1"),
        params![doc_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(Value::Null)
}
