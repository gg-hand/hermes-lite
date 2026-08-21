# teage_liu2 core 缺口记录

> **定位**:记录 `teage_liu2/core/` 已发现但**尚未修复**的缺口（core 相关、实现层或协议落地偏差）。已修复项移入 [开发日志.md](./开发日志.md)。唯一契约源 = `teage_liu2/PROTOCOL/`(v1.0.0),以代码为准。

## 已修复（移至开发日志）

### [已修复] 终态钩子收到初始快照而非终态快照（2026-08-21 首个扩展接入实验发现并修复）

- **现象**:`after` / `on_error` 终态钩子收到的 `snapshot.round` 恒为 0、`messages`/`extra` 为对话开始前状态(观测枝干拿不到终态)。
- **根因**:`core/pipeline.py` 调用 `after_all(snapshot, ...)` 与 `on_error_all(snapshot, ...)` 传的是**构建时的初始快照**,而同一处 `_persist_session_extra`(会话态 extra 写回)已用 `final_snapshot`——三处对"最终快照"定义不一致(§5 L-10 一致性缺陷)。
- **修复**:pipeline.py 统一改为 `getattr(self._mode_instance, "final_snapshot", None) or snapshot` 传终态快照(after 与 on_error 均同源)。
- **验证**:行为套件 runner 17/17 + tests_core 85/85 回归(见开发日志)。

## 缺口清单

### [P2] 同语言 observe 扩展的 L3 观测投递未接线（2026-08-21 首个扩展接入实验发现）

- **现象**:同语言扩展声明 `observe` 能力后,只能收到 L2 摘要(钩子),**收不到 L3 观测事件**(tool_use/tool_result/step_end 原始事件)。
- **根因**:`core/supervisor.py:122-138` `_register_extension` 中 `l3_sink.subscribe(...)` 仅对 **transport: stdio 异语言扩展**调用;同语言扩展(普通工厂注册 + `host_port` 注入)从未注册 L3 投递目标。`core/event_stream.py` L3BatchSink 的 `subscribe()` 接口已存在,只是缺装配接线。
- **影响**:按设计文档 §11「observe capability 即订阅」,同语言观测枝干(审计/指标)收不到 L3 原始事件——依赖 L3 的枝干被迫走 L2 或 stdio 变通。
- **当前规避**:首个扩展 audit 刻意走 L2 通道(after_step/after 摘要),不依赖 L3。
- **候选修复方向**:app.py 装配时对声明 observe 的同语言扩展调用 `l3_sink.subscribe(name, deliver_fn)`,deliver_fn 经 `host_port` 投递(参考 supervisor 的 `_make_l3_deliver`);或 registry 注入时统一处理。修复后须补行为套件断言。

### [P3] routes.py /chat 非流式不拼接 done.response（2026-08-21 阶段 3 遗留,已知既有行为）

- **现象**:`server/routes.py` 的 `/chat` 非流式端点收集事件时,`done` 事件不拼接 `done.response`(intercepted 场景返回空 response)。
- **状态**:已知既有行为(阶段 3 验收时记录),不影响 SSE 流式路径;是否修复归外壳侧待定。

### [P3] SessionStore 无 TTL / 无清理策略（契约而非缺口）

- **现象**:`core/session.py` SessionStore 纯 dict,无 TTL/自动清理。
- **定位**:设计文档 §13 明确为**外壳责任**(core 不内建清理),`server` 侧应配置会话清理,非 core 缺口。长期运行的内存责任在外壳(§18.7 接入指南条款 5)。此处仅为记录,不作为修复项。
