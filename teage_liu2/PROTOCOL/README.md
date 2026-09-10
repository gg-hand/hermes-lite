# PROTOCOL — teage_liu2 协议族（唯一契约源）

> **协议版本**: v1.0.0（semver；稳定面冻结，见 `VERSION`）
> **权威规格**: `docs/plans/2026-08-20-终极解耦架构-设计.md`（v1.14）
> **定位**: 本目录是 core 与扩展之间一切交互的**唯一契约源**——语言无关，任何宿主语言（Python/Rust/…）按此实现。
> `teage_liu2/docs/CORE.md` 与 `docs/SUBSYSTEM-SPI.md` 为实现视图，阶段 2 起逐步对齐本目录。

## 9 协议域

| 域 | 内容 | 关键文件 |
|---|---|---|
| `types/` | 值对象定义（Message/ContentBlock/ToolSchema/Usage/Injection/Snapshot）+ 命名约束 | `types.spec.md` / `types.schema.json` |
| `events/` | 事件流（8+ 事件 / done 9 键 / L1-L2-L3 三层） | `events.spec.md` / `events.schema.json` |
| `hooks/` | 11 钩子 + 6 Action + 应用时序（收口四连 / 覆盖规则 / SetStop 短路） | `hooks.spec.md` / `hooks.schema.json` |
| `lifecycle/` | 生命周期（setup/teardown/热重载/进程重建）+ 扩展声明 | `lifecycle.spec.md` / `lifecycle.schema.json` |
| `storage/` | 存储 SPI + 三层能力模型 + kind 前缀隔离 | `storage.spec.md` / `storage.schema.json` |
| `config/` | 配置协议（YAML + ${VAR} + schema 校验） | `config.spec.md` / `config.schema.json` |
| `transport/` | 传输协议（双绑定形态 + 消息全集 + 帧编码） | `transport.spec.md` / `transport.schema.json` |
| `errors/` | 错误协议（终止原因 + 错误码全集 + 责任矩阵） | `errors.spec.md` / `errors.schema.json` |
| `evolution/` | 演进机制（稳定面冻结 vs 演进面开放 + 三阶演进） | `evolution.spec.md` / `evolution.schema.json` |

## 演进追踪

候选/实验性协议元素（未入冻结面）统一记录在 [`PENDING.md`](./PENDING.md)：新扩展协议**先写 PENDING.md**，正式使用且迭代稳定后才更新进本目录契约（spec + schema + 行为套件用例 + VERSION），并在 PENDING.md"已并入记录"区登记。

## 行为套件（黄金用例集）

`behavior-suite/` 是协议级语言无关黄金用例集（JSON），每个宿主实现运行同一套用例并报告通过率——v1.0 冻结前提 + 新宿主验收门槛 + 协议-实现漂移审计工具。

**交付物边界（2026-09-10 明确）**：语言无关交付物 = `cases/*.json` + `suite.schema.json` + `matcher.schema.json`；`runner.py` 是**本实现的 Python 参考执行器**，允许依赖宿主实现（`teage_liu2/core/`）与其测试基础设施（`teage_liu2/tests_core/fake_llm.py`）——该依赖经用户 2026-09-10 授权（测试套件允许入库）显式化，非隐性耦合。

**断言能力纪律（2026-09-10 起）**：runner 对**未实现的断言键一律显式失败**（`_check_supported`），禁止静默忽略——此前 `final.persisted` / `invocations[].isolation` 曾被静默跳过，形成"假绿"。

## 核心约束（速查）

- `extension_name`: `^[a-z0-9_]+$`（禁点）
- `kind`: `^[a-z0-9_.]+$`
- `SetExtra` key: `^[a-z0-9_]+\.[a-z0-9_.]+$`
- done 事件统一 9 键；事件流必以 done/error 收尾
- core 对未知协议元素一律"透传/记录，绝不崩溃"
