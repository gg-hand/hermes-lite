# teage-storage-rust — Rust 存储后端扩展

P-4 storage-stdio 线协议的 Rust 实现（首个异语言存储后端），以
`kind: host-component` 扩展形态纳入统一扩展目录树（P-6）。
注意：扩展目录名/manifest name 为 `storage_rust`（manifest 名称白名单
`^[a-z0-9_]+$` 不含连字符）；crate/二进制名为 `teage-storage-rust`。

## 构建

```powershell
cargo build --release
```

## 安装（两条命令）

```powershell
New-Item -ItemType Directory -Force data2\extensions\storage_rust | Out-Null
Copy-Item storage-rust\target\release\teage-storage-rust.exe, storage-rust\manifest.yaml data2\extensions\storage_rust\
```

## 启用（config.yaml）

```yaml
core:
  extensions_root: data2/extensions
host_components:
  - slot: storage
    backend: stdio-proxy
    options:
      extension: storage_rust
      args: ["--db", "data2/storage_rust/sessions.db"]
      request_timeout_seconds: 10
```

`--db` 必填（缺参启动即失败）；库文件从零开始，不迁移存量。

## 协议

实现 `teage_liu2/PROTOCOL/PENDING.md` P-4（storage-stdio 线协议）：
JSON Lines over stdio，op 集 = write/read/query/delete + ensure_session/
log_message/get_session_messages/update_session_title/get_session_title/
search_messages；握手 `hello`（major 对齐 0.1.0）、关闭 `bye`。
单线程同步循环 + rusqlite WAL，schema 与 core 兼容（新库从零，不迁移存量）。
