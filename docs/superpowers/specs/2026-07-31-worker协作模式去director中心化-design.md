# Worker 协作模式去 Director 中心化 - 设计文档

> 日期：2026-07-31
> 状态：待审查
> 作者：协作设计 brainstorming
> 关联文档：`docs/plans/2026-07-23-agent自主协作架构重设计.md`

## 1. 背景与问题

### 1.1 现状观察

从 `data/blackboard/collaboration.md` 最新对话记录（seq 36-48）观察到的现象：

* **seq 36** director 广播：「你们分别写两份今日早报给我，相互探讨一个主题」

* **seq 41** teagent-lu 回复 director：「请 Director 确认：1. 是否有其他协作 agent 一同参与本任务？」

* **seq 43** teagent-liu-2 回复 director：「请 Director 或 teagent-lu 确认：是否同意以"AI与科技创新"作为协作主题？」

* **seq 47** teagent-lu 回复 director（reply\_to=46）：「teagent-liu-2，你觉得这个分配方式和规则可以吗？」

* **seq 48** teagent-liu-2 回复 director（reply\_to=46）：「请问 Director 和 teagent-lu，这个方案是否可行？」

### 1.2 设计问题

worker 把 director 广播当作"待审批提案"，倾向向 director 请示而非与对方 worker 协商推进。根因有 4：

| # | 根因                                                                                                          | 文件位置                                      |
| - | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| 1 | LLM 注入的 director 角色定位为"协调和引导协作"，暗示 worker 应请示                                                               | `director_injection.py` L76-82            |
| 2 | director 广播 prompt 用「请决定是否参与并回复」措辞，worker 自然理解为"回复 director"                                                | `worker_adapter.py` L1068                 |
| 3 | LLM 调用时仅给 director 广播内容，**未给对方 worker 的最近表态**，worker 看不到对方在说什么，只能向 director 请示                              | `worker_adapter.py` `_trigger_urgent_llm` |
| 4 | worker 回复时 `reply_to=director_seq`，对话以 director 为中心，对方 worker 看到这些回复也是"作为对 director 的回复"而非"对方 worker 在和我对话" | `worker_adapter.py` `_handle_request`     |

## 2. 设计目标与非目标

### 2.1 设计目标

* **worker 收到 director 广播后**：把广播内容作为"协作背景上下文"，直接与其他在线 worker 协商推进

* **worker 不再回复 director**：不向 director 请示方案、不请求确认

* **worker 能看到对方表态**：LLM 上下文包含协作伙伴列表 + 每个伙伴最近 N 条表态

* **对话路由去中心化**：worker 之间消息 reply\_to 指向对方 worker 消息，而非 director seq

### 2.2 非目标（明确不做）

* ❌ 不删除 director 进程代码（director\_engine / director\_cli / director\_manager 保留）

* ❌ 不删除 director 的基础设施职责（epoch / 心跳 / 互斥锁 / messages.pending.md flush）

* ❌ 不改前端 UI（保留现有"Director 广播"入口）

* ❌ 不引入新的协作会话状态机（YAGNI）

* ❌ 不改 director 发广播的能力（director 仍可发 type=request / directive 广播）

## 3. 改造范围（4 个改动点）

### 3.1 改动点 1：DirectorInjector 注入内容改写

**文件**：`teage_liu/multiagent/director_injection.py` L76-82

**当前**：

```python
block = (
    f"[Director 引导]\n"
    f"角色提示：当前协作中存在 Director 角色，其职责是协调和引导协作。\n"
    f"当前引导：{content}\n"
    f"引导类型：{rule_type}\n"
    f"来源：{issued_by} | 时间：{ts}"
)
```

**改后**：

```python
block = (
    f"[协作背景指导]\n"
    f"说明：Director 仅提供任务背景与指导，不审批方案。请直接与其他在线 worker 协商推进，不要回复或请示 Director。\n"
    f"背景内容：{content}\n"
    f"指导类型：{rule_type}\n"
    f"来源：{issued_by} | 时间：{ts}"
)
```

**关键差异**：

* 标题「Director 引导」→「协作背景指导」

* 明确告知"Director 不审批方案，请直接与其他 worker 协商，不要回复或请示"

### 3.2 改动点 2：worker 收到 director 广播时的 prompt 改写

**文件**：`teage_liu/multiagent/worker_adapter.py` L1060-1068

**当前**：

```python
if from_label == "director":
    # Director 广播：幂等检查 _responded_request_seqs，走紧急 LLM
    if isinstance(msg_seq, int) and msg_seq in self._responded_request_seqs:
        ...
        return
    prompt = f"Director 广播协作请求：{msg.get('content', '')}\n请决定是否参与并回复。"
```

**改后**：

```python
if from_label == "director":
    # Director 广播：作为协作背景上下文，不回复 director
    if isinstance(msg_seq, int) and msg_seq in self._responded_request_seqs:
        ...
        return
    # 拉取协作伙伴上下文（其他在线 worker 最近表态）
    partner_context = await self._build_collab_partner_context()
    prompt = (
        f"[协作背景] Director 提供任务背景：{msg.get('content', '')}\n"
        f"\n{partner_context}\n"
        f"\n请参考此背景，直接与其他在线 worker 协商推进任务。"
        f"不要回复 Director，不要请示 Director 审批。"
        f"回复时 to 字段设为对方 worker 的 agent_id（点对点）或 '*'（广播给所有 worker）。"
    )
```

**关键差异**：

* prompt 不再说"请决定是否参与并回复"

* 明确告诉"不要回复 Director，不要请示"

* 注入协作伙伴上下文（\_build\_collab\_partner\_context）

* 引导回复路由：to=对方 worker id 或 \*

### 3.3 改动点 3：新增协作伙伴上下文注入（核心新增）

**文件**：`teage_liu/multiagent/worker_adapter.py` 新增方法

````python
async def _build_collab_partner_context(self, max_partners: int = 5, max_msgs_per_partner: int = 3) -> str:
    """收集在线协作伙伴列表 + 每个伙伴最近 N 条表态，作为 LLM 上下文。

    Args:
        max_partners: 最多收集多少个伙伴（避免上下文爆炸）
        max_msgs_per_partner: 每个伙伴最近几条表态

    Returns:
        格式化的协作伙伴上下文字符串。如：
        ```
        在线协作伙伴：
        - teagent-liu-2 (capabilities: file_read, file_write, web_search)
          最近表态：
          [seq=47] 我来当出题者，teagent-liu-2 当猜题者...
          [seq=45] 我已准备就绪，可以参与协作...
        ```
    """
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.schema_validator import SchemaValidator
    from teage_liu.multiagent.blackboard import read_collab_messages

    # 1. 收集在线 agent（除自己外）
    registry = AgentRegistry(self._bb_root, SchemaValidator(enabled=False))
    agents = await registry.list_active_agents()
    partners = [a for a in agents if a.get("agent_id") != self._agent_id][:max_partners]

    if not partners:
        return "（当前无其他在线协作伙伴）"

    # 2. 收集每个伙伴最近 N 条表态
    all_msgs = await read_collab_messages(self._bb_root)
    # 仅取 from=伙伴 的消息，按 seq 倒序，取最近 N 条
    partner_msgs: dict[str, list[dict]] = {p["agent_id"]: [] for p in partners}
    for m in sorted(all_msgs, key=lambda x: x.get("seq", 0), reverse=True):
        sender = m.get("from", "")
        if sender in partner_msgs and len(partner_msgs[sender]) < max_msgs_per_partner:
            partner_msgs[sender].append(m)

    # 3. 格式化
    lines = ["在线协作伙伴："]
    for p in partners:
        aid = p["agent_id"]
        caps = ", ".join(p.get("capabilities", [])) or "(无)"
        lines.append(f"- {aid} (capabilities: {caps})")
        recent = partner_msgs.get(aid, [])
        if recent:
            lines.append("  最近表态：")
            for m in reversed(recent):  # 时间正序展示
                seq = m.get("seq", "?")
                content = (m.get("content", "") or "")[:200]
                lines.append(f"  [seq={seq}] {content}")
        else:
            lines.append("  最近表态：（暂无）")

    return "\n".join(lines)
````

**设计要点**：

* 仅收集 active 状态的 agent（除自己外）

* 每个伙伴最多 3 条最近表态，每条内容截断到 200 字（避免上下文爆炸）

* 时间正序展示（让人能读出对话流）

* 总伙伴数上限 5（避免上下文过长）

### 3.4 改动点 4：worker 回复路由调整

**文件**：`teage_liu/multiagent/worker_adapter.py` `_handle_request` 处理 from=director 路径

**当前**：worker 收到 director 广播后，回复时 `to="*"` + `reply_to=director_seq`

**改后**：worker 收到 director 广播后，回复时 **`to`** **字段由 LLM 根据 prompt 引导决定**（点对点 `to=对方 worker id` 或广播 `to="*"`）+ **代码层强制不设** **`reply_to`**（避免对话以 director 为中心）

**实现方式**：在 `_trigger_urgent_llm` 调用前，构造 context\_msg 的副本，将 reply\_to 字段移除：

```python
if from_label == "director":
    if isinstance(msg_seq, int) and msg_seq in self._responded_request_seqs:
        ...
        return
    partner_context = await self._build_collab_partner_context()
    prompt = (... 如 3.2 所示 ...)
    # 改动点 4：构造 context_msg 副本，移除 reply_to 字段，避免 worker 回复指向 director
    context_msg = dict(msg)
    context_msg.pop("reply_to", None)  # 不再以 director 消息为回复目标
    # 标记：本消息是对 director 广播的"参考响应"，但 reply 不指回 director
    context_msg["_collab_context_only"] = True
    await self._trigger_urgent_llm(prompt=prompt, context_msg=context_msg)
```

**注意**：`_trigger_urgent_llm` 内部构造协作回复消息时，需检查 `context_msg.get("_collab_context_only")` 标志，若为 True 则不在响应消息中设置 `reply_to`。

### 3.5 改动点 4 的实现细节（\_trigger\_urgent\_llm 协作）

需要查看 `_trigger_urgent_llm` 当前如何构造响应消息（搜索 `append_collab_message` / `append_message`）。预期改动：

```python
# 在 _trigger_urgent_llm 中构造响应消息时
response_msg = {
    "from": self._agent_id,
    "to": "*",  # 或由 LLM 决定 to=对方 worker id
    "type": "response",
    "content": llm_response,
    "timestamp": now_iso,
}
# 若 context_msg 是 director 广播（_collab_context_only=True），不设 reply_to
if not context_msg.get("_collab_context_only"):
    response_msg["reply_to"] = context_msg.get("seq")
```

## 4. 测试策略

### 4.1 单元测试（新增）

| 测试文件                                          | 测试用例                                                        | 验证点                                                   |
| --------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------- |
| `tests/multiagent/test_director_injection.py` | `test_injected_block_does_not_request_director_approval`    | 注入内容包含"不审批方案" / "不要回复或请示" 关键词                         |
| `tests/multiagent/test_worker_adapter.py`     | `test_director_broadcast_prompt_includes_partner_context`   | prompt 包含"协作伙伴" + "不要回复 Director"                     |
| `tests/multiagent/test_worker_adapter.py`     | `test_director_broadcast_response_has_no_reply_to_director` | worker 响应消息不含 reply\_to 字段或 reply\_to 不是 director seq |
| `tests/multiagent/test_worker_adapter.py`     | `test_build_collab_partner_context_excludes_self`           | 收集的伙伴列表不含本机 agent\_id                                 |
| `tests/multiagent/test_worker_adapter.py`     | `test_build_collab_partner_context_caps_per_partner`        | 每个伙伴最多 3 条表态                                          |
| `tests/multiagent/test_worker_adapter.py`     | `test_build_collab_partner_context_empty_when_no_partners`  | 无其他在线 worker 时返回提示字符串                                 |

### 4.2 回归测试（现有不应破坏）

* `tests/multiagent/test_director_injection.py` 现有测试需调整（断言关键词变化）

* `tests/multiagent/test_worker_adapter.py` 现有 director 广播相关测试需调整

* `tests/multiagent/test_worker_idempotency.py` 已处理请求跳过逻辑不变

* `tests/multiagent/test_collaboration_routes.py` 消息路由不破坏

### 4.3 集成验证（手动）

启动双 worker 实例，前端发 director 广播「你们两个玩猜数字游戏，角色自己分配」：

**通过标准**：

* worker 回复的 `reply_to` 字段为 null 或指向对方 worker 消息 seq（非 director seq）

* worker 回复的 `to` 字段为 `*` 或对方 worker id

* LLM 响应内容不再包含「请 Director 确认」「请 Director 审核」等请示措辞

* worker 直接与对方 worker 协商角色分配和规则

## 5. 回滚预案

所有改动用 `if config.get("worker_collab_decentralized", True):` 包住，便于回滚到原"director 中心化"模式：

```python
if self._config.get("worker_collab_decentralized", True):
    # 新逻辑：协作伙伴上下文 + 不回复 director
    partner_context = await self._build_collab_partner_context()
    prompt = (...)  # 新 prompt
    context_msg = dict(msg)
    context_msg.pop("reply_to", None)
    context_msg["_collab_context_only"] = True
else:
    # 旧逻辑：原 director 广播 prompt + reply_to=director_seq
    prompt = f"Director 广播协作请求：{msg.get('content', '')}\n请决定是否参与并回复。"
    context_msg = msg
```

DirectorInjector 的注入内容也用类似开关包住。

## 6. 风险与缓解

| 风险                                                           | 缓解                                                                                                  |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| LLM 仍倾向请示 director（prompt 引导不够强）                             | 调试时观察实际 LLM 输出，必要时加强 prompt 措辞（如"严禁回复 Director"）                                                    |
| 协作伙伴上下文过长导致 token 超限                                         | 已设上限：5 伙伴 × 3 条 × 200 字 = 最多 3000 字                                                                 |
| `_build_collab_partner_context` 性能开销（每次 LLM 调用都读 blackboard） | 可加缓存：基于 last\_collab\_seq 增量读取，但 v1 先做简单实现，性能问题后续优化                                                 |
| worker 之间死锁（双方都等对方表态）                                        | 不在本次改动范围（YAGNI），现有超时推进机制由 director 触发；本次改动让 worker 不响应 director 但仍接收 director 心跳检测，超时仍由 director 兜底 |
| 现有测试断言失败                                                     | 已在 4.2 列出需调整的测试文件                                                                                   |

## 7. 实现顺序建议

1. 先改 `director_injection.py` 注入内容（独立改动，无依赖）
2. 在 `worker_adapter.py` 新增 `_build_collab_partner_context` 方法（独立新增）
3. 修改 `worker_adapter.py` `_handle_request` 的 from=director 路径（依赖 1+2）
4. 调整 `_trigger_urgent_llm` 中响应消息构造（依赖 3）
5. 更新现有测试断言（依赖 1-4）
6. 新增单元测试（依赖 1-4）
7. 跑全量回归：`pytest tests/multiagent/ tests/api/test_multiagent_routes.py`
8. 手动集成验证（双 worker 实例 + 前端广播猜数字游戏）

## 8. 验收清单

* [ ] 改动点 1-4 全部实现且通过单元测试

* [ ] 全量回归 0 失败（原 468 passed + 新增 6 个测试 = 474 passed 期望）

* [ ] 手动集成验证：worker 回复不含「请 Director 确认」类措辞

* [ ] 手动集成验证：worker 回复 reply\_to 不是 director seq

* [ ] `if worker_collab_decentralized` 开关可回滚到旧模式

* [ ] 无新引入的未使用 import / 死代码

