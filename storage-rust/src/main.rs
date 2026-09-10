//! P-4 storage-stdio 存储后端(Rust 实现,设计 §4)。
//! 启动:teage-storage-rust --db <path>;stdin EOF 或 bye 后 flush 退出。
mod dispatch;
mod docs;
mod history;
mod proto;

use std::process::ExitCode;

fn main() -> ExitCode {
    let db_path = match parse_db_arg() {
        Some(p) => p,
        None => {
            eprintln!("启动失败: 缺少必填参数 --db <path>(SQLite 库文件路径)");
            return ExitCode::from(2);
        }
    };
    if let Err(e) = run(&db_path) {
        eprintln!("启动失败: {e}");
        return ExitCode::from(2);
    }
    ExitCode::SUCCESS
}

fn run(db_path: &str) -> Result<(), String> {
    let conn = open_db(db_path)?;
    history::init_tables(&conn)?;
    proto::serve(conn)
}

fn open_db(db_path: &str) -> Result<rusqlite::Connection, String> {
    // 对齐 core(实现 os.makedirs 父目录):库路径父目录不存在则创建
    if let Some(parent) = std::path::Path::new(db_path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("创建库目录失败 {parent:?}: {e}"))?;
        }
    }
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| format!("打开 SQLite 库失败 {db_path:?}: {e}"))?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
        .map_err(|e| format!("设置 PRAGMA 失败: {e}"))?;
    Ok(conn)
}

fn parse_db_arg() -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    let pos = args.iter().position(|a| a == "--db")?;
    let value = args.into_iter().nth(pos + 1)?;
    if value.is_empty() { None } else { Some(value) }
}
