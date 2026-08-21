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
  branches:                     # 扩展声明, 顺序 = 注册顺序
    guardrails: { enabled: true }
```

## 3. 版本与演进

- 新增 core 配置键 = minor 演进；删除/改名配置键 = major 演进。
- 扩展配置段由扩展自管 schema（不在 core 域 schema 内）。
