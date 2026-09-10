# config 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `config.schema.json` 是 config 域的语言无关规范。

## 1. 配置格式

- 配置文件 YAML（语言无关）；`${ENV_VAR}` 占位注入，敏感字段分离（.env 不入库）；
- 配置 schema（JSON Schema）: core 段 + 各扩展段；严格校验——未知键拒绝、类型/范围校验，失败 = 启动失败（可读错误列未知键）。

**行为条款 C-1（配置段归属）**: core 段由 core 校验，扩展段由扩展在 setup 自校验（收到自己的配置段）。
**行为条款 C-2（敏感字段）**: `${VAR}` 占位注入 + `.env` 分离 + 脱敏函数是既有配置功能资产（回指 §15-A8：core 敏感信息不泄漏——配置功能与安全边界区分，不重复声明）。

## 2. core 段结构

```yaml
core:
  mode: loop                    # bare / loop(未知值启动失败)
  max_loops: 50                 # 1-200
  system_prompt: ...            # 字符串(默认内置)
  hook_timeout: 5.0             # 0.1-60 秒
  history_window_messages: 100  # 1-10000
  injection_budget_chars:       # 分层预算(层名白名单)
    PREFIX: 8000
  max_snapshot_bytes: 2097152   # 2 MiB(1MiB-256MiB,§types T-8 资源上限)
  max_message_bytes: 524288     # 512 KiB(1KiB-16MiB)
  max_messages_per_conversation: 2000   # 单次对话消息条数上限(§types T-8)
  extensions_root: data2/extensions     # 扩展安装根(非空字符串,目录发现)
  branches:                     # 扩展声明, 顺序 = 注册顺序
    guardrails: { enabled: true }
```

> **补录说明（2026-09-10）**：`max_snapshot_bytes` / `max_message_bytes` / `max_messages_per_conversation`（§types T-8 资源上限）与 `extensions_root`（2026-09-08 统一扩展目录树）此前实现已支持但本表遗漏，现补录。按 §3 演进规则，新增 core 配置键属 minor 演进面：`extensions_root` 与 manifest 规范一并登记于 PENDING **P-1**（涉及域含 config）；三个资源上限键**尚无 PENDING 条目**（其语义由 `types.spec.md` §7 的 T-8 条款承载），待 P-1 收口评审时一并决定是否登记。

## 3. 版本与演进

- 新增 core 配置键 = minor 演进；删除/改名配置键 = major 演进。
- 扩展配置段由扩展自管 schema（不在 core 域 schema 内）。
