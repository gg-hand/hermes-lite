# storage 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `storage.schema.json` 是 storage 域的语言无关规范。

## 1. StorageProvider SPI

```
StorageProvider:
  write(kind, doc | docs[]) -> doc_id | doc_id[]   # 支持批量(§18.4)
  read(kind, doc_id) -> doc | null
  query(kind, filters, limit?) -> doc[]             # limit 防全量加载(§18.4)
  delete(kind, doc_id)
  close()
kind 白名单 ^[a-z0-9_.]+$(防注入,非法名拒绝)
MessageStore: 消息级落盘(content_blocks JSON / token_count / reasoning / message_type)
```

任何语言实现满足此 SPI 即可作为存储后端。

**行为条款 S-1（落盘时机）**: 事件驱动——user 前置 → step_end 落 assistant → tool_result 聚合落 user（配对 tool_use_id）→ finally 兜底；注入永不落盘。

## 2. 扩展存储三层能力模型

1. **宿主通用存储**（经 transport `storage_*` 消息）: 结构化 doc，kind 前缀隔离；适用于低频读写；SPI 保持极简（write/read/query/delete）——core 只保持最基本的稳定高效的最小实现，复杂形态不膨胀进宿主 SPI。
2. **自持存储**（显式声明 `capabilities: [..., "self_hosted_storage"]`）: 扩展自管文件/向量库/任何外部存储，进程自有——向量相似度检索（记忆）、高频实时写+轮转+游标增量读（审计）、按天 upsert（指标）等超出通用 SPI 表达力的场景全部落此层。**沙箱边界（路径白名单/网络白名单）归扩展+部署层，非 core 范围（§15-B2）**。
3. **协议显式承认**: 自持存储是经 capability 声明的显式例外，是声明制开放面。

**行为条款 S-2（宿主存储的唯一通道）**: 宿主存储的唯一合法入口 = transport `storage_*` 消息（同语言实现亦走消息，冷路径低频，性能可接受）；扩展不得以对象引用访问宿主存储（§lifecycle L-8）。

## 3. kind 前缀隔离（读写都隔离）

**行为条款 S-3（前缀隔离强制）**: 扩展只能读写自己前缀 `{extension_name}.` 的 kind；跨前缀访问拒绝（schema 级校验）；跨扩展数据查询走 RFC（新协议面），当前禁止——SetExtra 只承载对话内同步传递，不承载跨扩展持久查询。此前缀隔离为 core 内核级安全 A3，在 transport `storage_*` 消息层强制。
**行为条款 S-4（extension_name 字符集）**: `^[a-z0-9_]+$`（禁点）——禁点消除 "my.tool.facts" 同时匹配扩展 "my" 与 "my.tool" 的解析歧义。前缀校验谓词 = `kind.startswith(f"{name}.")` 且 name 匹配 `^[a-z0-9_]+$`。

## 4. 会话态不在扩展存储范围

会话态经快照 extra 延续承载（§lifecycle L-10），不经 storage 消息；SessionStore 仅宿主容器，会话级生命周期归外壳管理。

## 5. 性能契约（§18.4）

- `storage_write` 支持批量（`docs[]`，一次消息携带多条）——审计类高频写的宿主侧通道优化；
- `query` 补 `limit` 参数（防全量加载）；
- 宿主通用存储定位 = **低频小量结构化数据**；高频写（每工具调用级）/向量检索/轮转游标 → 自持存储 capability——SPI 不为重负载膨胀；
- 审计分流: 审计扩展推荐同语言进程内绑定（写队列零序列化成本，经 StorageWriter 串行化，SQLite WAL 顺序写正是强项）；必须异语言时走自持存储；宿主 storage_write 批量接口作为两者的中间档。

## 6. 版本与演进

- SPI 新增方法 = minor 演进；变更既有签名 = major 演进。
