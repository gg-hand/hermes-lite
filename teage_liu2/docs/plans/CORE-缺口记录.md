# teage_liu2 core 缺口记录

> **定位**:记录 `teage_liu2/core/` 已发现但**尚未修复**的缺口（core 相关、实现层或协议落地偏差）。已修复项移入 [开发日志.md](./开发日志.md)。唯一契约源 = `teage_liu2/PROTOCOL/`(v1.0.0),以代码为准。

## 已修复（正文保留供追溯；变更摘要已同步开发日志）

> 本节仅保留修复记录正文供回溯；每条对应摘要已写入 [开发日志.md](./开发日志.md)。

### [已修复] 终态钩子收到初始快照而非终态快照（2026-08-21 首个扩展接入实验发现并修复）

- **现象**:`after` / `on_error` 终态钩子收到的 `snapshot.round` 恒为 0、`messages`/`extra` 为对话开始前状态(观测枝干拿不到终态)。
- **根因**:`core/pipeline.py` 调用 `after_all(snapshot, ...)` 与 `on_error_all(snapshot, ...)` 传的是**构建时的初始快照**,而同一处 `_persist_session_extra`(会话态 extra 写回)已用 `final_snapshot`——三处对"最终快照"定义不一致(§5 L-10 一致性缺陷)。
- **修复**:pipeline.py 统一改为 `getattr(self._mode_instance, "final_snapshot", None) or snapshot` 传终态快照(after 与 on_error 均同源)。
- **验证**:行为套件 runner 17/17 + tests_core 85/85 回归(见开发日志)。

### [已修复] 同语言 observe 扩展的 L3 观测投递未接线（2026-08-21 修复）

- **现象**:同语言扩展声明 `observe` 能力后,只能收到 L2 摘要(钩子),**收不到 L3 观测事件**(tool_use/tool_result/step_end 原始事件)。
- **根因**:`core/supervisor.py` `_register_extension` 中 `l3_sink.subscribe(...)` 仅对 **transport: stdio 异语言扩展**调用;同语言扩展从未注册 L3 投递目标。
- **修复**:
  - `core/hooks.py` Branch 新增 `async def on_l3_events(events)` 默认空实现(同语言扩展 L3 观测通知入口,非钩子不经 HookChain);
  - `server/app.py` 新增 `subscribe_inprocess_l3(l3_sink, hooks)`:装配时对链上声明 observe 的**普通 Branch 实例**(非 RemoteBranchAdapter)调用 `l3_sink.subscribe(name, deliver_fn)`,deliver_fn 进程内异步调 `branch.on_l3_events(events)`(异常隔离,旁路不阻断主对话流);`create_app` 装配后调用,并挂 app.state 供热重载复用;
  - `server/routes.py` `/reload` 热重载成功后重新订阅(rebuild 产生新实例,覆盖同 name 订阅);
  - `branches/audit.py` 增加 `on_l3_events` 演示(批量落盘 `audit.l3_events`),`_write` 支持批量 docs。
- **验证**:行为套件 runner 17/17 + tests_core/tests_branches 89 passed + 端到端 d:/tmp 脚本(step_end/tool_use/tool_result 全部经 on_l3_events 到达落盘,4 条)+ create_app 冒烟(l3 observers 含 audit)。

### [已修复] /chat/stream 提前关闭生成器,after 终态钩子永不触发（2026-09-09 扩展端到端验证发现并修复,P2）

- **现象**:正常完成的对话从不写 `doc_audit_conversations`(表都未建),生产环境 `after` 钩子自上线以来从未真正执行。
- **根因**:`server/routes.py` `event_source()` 在 done/error 事件后 `return`,提前关闭 pipeline 生成器 —— `chat_stream` 中 `async for` 之后的 `after_all` 永远执行不到(协议契约:收尾事件之后还执行 after/on_error 终态钩子)。
- **修复**:删除提前 `return`(hooks._call 有超时+异常隔离,pipeline 钩子执行完自然结束,不存在 done 后再发 error 的风险)。
- **验证**:前端实测 + 库检查,`doc_audit_conversations` 首次建表,3 场正常对话均落盘(termination_reason=normal)。

### [已修复] SetStop 短路路径使用陈旧 final_snapshot（2026-09-09 发现并修复）

- **现象**:拦截对话进 `audit.errors` 时 `user_input` 是**上一场对话的输入**(实测:拦截记录显示前一轮的"这是一次正常对话"),`_persist_session_extra` 同样写回陈旧 extra。
- **根因**:模式实例(ReactLoop/BareMode)跨对话复用,`final_snapshot` 残留上一场对话终态;SetStop 短路与早期异常都发生在 `run_stream` 重置点之前。
- **修复**:`core/pipeline.py` 构建快照后立即 `self._mode_instance.final_snapshot = snapshot` 归位;run_stream 进入后按推进点正常覆盖。
- **验证**:复测拦截记录 `user_input` 正确、`round=0`(构建后快照)。

### [已修复] /chat 非流式不采用 done.response（2026-08-21 阶段 3 遗留,2026-09-09 修复）

- **现象**:`/chat` 非流式只拼全量 `text_parts`:①拦截路径无 text_delta → 返回空 response(友好文案只存在于 done.response);②loop 多轮全量拼接与流式"最后轮文本"口径不一致(P2-1 单一事实源锚定)。
- **修复**:`server/routes.py` done 事件到达时取 `ev["response"]`(pipeline 已兜底填充),为空才回退 `text_parts`。
- **验证**:/chat 拦截返回友好文案 + `termination_reason=intercepted`。

### [已修复] /reload 响应序列化 500(返回 Branch 实例而非名字)（2026-09-09 发现并修复）

- **现象**:`POST /reload` 恒返回 500,但 reload 本身成功(rebind 已执行,配置已生效) —— 异常发生在响应序列化阶段。
- **根因**:routes.py 返回 `[name for name, _ in registry.entries]`,而 `entries` 是 `(Branch, 配置段)` 元组表,第一个元素是 **Branch 实例**;FastAPI jsonable_encoder 钻进 Branch → host_port → transport_bus → storage_provider 触 `_thread.lock` 不可编码。
- **修复**:改为 `[branch.name for branch, _ in registry.entries]`。
- **验证**:reload 返回 `{"reloaded":true,"branches":["guardrails","audit"]}`,热重载后新链拦截 + audit 落盘正常。

### [已修复] 拦截提示文案不友好 / 审计不可区分拦截与失败（2026-09-09 顺带优化）

- **现象**:拦截时前端显示"对话已被枝干拦截"(生硬);`audit.errors` 中拦截记录 error 文案为兜底"对话未完成(中断/失败/拦截)",无法区分拦截与真失败。
- **修复**:`core/pipeline.py` 拦截分支 ①response 改为"您的消息包含不允许的内容，已被安全策略拦截。请调整表述后再试。"(core 通用文案,不点名枝干,守依赖铁律);②先设 `error_message = "对话被安全策略拦截"` 再进 finally 的 on_error_all。
- **验证**:行为套件 17/17;audit 语义可区分:拦截=round 0+拦截文案,中断=round≥1+兜底文案。

## 缺口清单

### [P3] SessionStore 无 TTL / 无清理策略（契约而非缺口）

- **现象**:`core/session.py` SessionStore 纯 dict,无 TTL/自动清理。
- **定位**:设计文档 §13 明确为**外壳责任**(core 不内建清理),`server` 侧应配置会话清理,非 core 缺口。长期运行的内存责任在外壳(§18.7 接入指南条款 5)。此处仅为记录,不作为修复项。
