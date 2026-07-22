"""记忆管理工具：profile_update, search_memory, delete_memory, update_memory。

本模块注册 4 个记忆相关工具到 ToolRegistry 的 Core Tier / Deferred Tier：

- ``profile_update``（Core Tier）：修改用户画像（memory.md）。采用延迟合并写入
  策略，add 操作走信号池累积，replace/delete 直接入 pending 队列。
- ``search_memory``（Core Tier）：检索向量库长期记忆（读取类，不走 confirm）。
- ``delete_memory``（Deferred Tier）：入队删除操作，下次 consolidate 时执行
  （高危，走 PolicyEngine confirm）。
- ``update_memory``（Deferred Tier）：入队更新操作，下次 consolidate 时执行
  （高危，走 PolicyEngine confirm）。

注：``_search_memory`` / ``_delete_memory`` / ``_update_memory`` /
``_update_profile`` 是 ``register_memory_tools`` / ``_register_update_profile``
内部的 closure（嵌套函数），无法在模块级导入。如需访问，请通过
``register_memory_tools`` 注入后由 registry 取出 handler。
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from ...memory.consolidation import ConsolidationEngine
    from ...storage.chroma_store import ChromaMemoryStore

logger = logging.getLogger(__name__)


def _register_update_profile(
    registry,
    consolidation_engine: "ConsolidationEngine",
    signal_pool=None,
) -> None:
    """注册 update_profile 工具到 Core Tier。

    工具允许 LLM 通过 add/replace/delete 三种操作显式修改用户画像
    memory.md。采用**延迟合并写入**策略：handler 不立即写 memory.md。

    操作分流（信号池机制）：
    - add：走信号池累积（若注入 signal_pool），相似信号去重 + 计数累加，
      达阈值（7）才入 pending 队列写入画像。即使用户明确说"记住"也需多次
      出现，符合"稳定模式"设计哲学。为 None 时回退到直接入 pending 队列。
    - replace/delete：直接入 pending 队列（用户显式修改，非待观察信号）。

    handler 通过 closure 捕获 ``consolidation_engine`` 与 ``signal_pool``。
    ToolRegistry 调用 handler 时按关键字参数传入 tool_input（``action`` /
    ``section`` / ``content``），由 handler 内部校验后分流。

    三层防线（入池前/入队前校验）：
    1. 长度上限（MAX_PROFILE_CONTENT_LEN = 4000）—— 先校验，避免长文本
       浪费正则匹配开销
    2. 内容黑名单 —— 14 条正则覆盖系统架构/项目描述/一次性上下文
    3. 单会话频次上限（MAX_PROFILE_WRITES_PER_SESSION = 5）—— 仅约束
       add 操作（入信号池），replace/delete 不受限

    参数:
        registry: ToolRegistry 实例。
        consolidation_engine: ConsolidationEngine 实例，提供
            :meth:`enqueue_profile_update` 接口。
        signal_pool: 可选的 ``SignalPool`` 实例。注入后 add 操作走信号池
            累积；为 None 时 add 回退到直接入 pending 队列（向后兼容）。
    """
    # 长度与频次上限常量（4000/5，配合信号池累积机制放宽）
    MAX_PROFILE_CONTENT_LEN = 4000
    MAX_PROFILE_WRITES_PER_SESSION = 5
    # per-session 写入计数器（进程内持久，跨 run 累积）
    # key: session_id, value: write count
    session_write_counts: dict = {}

    def _update_profile(action: str, section: str, content: str = "", target: str = "user") -> str:
        """update_profile 工具 handler（closure 捕获 consolidation_engine/signal_pool）。

        参数:
            action: 操作类型，``"add"`` / ``"replace"`` / ``"delete"`` 之一。
            section: memory.md 中的 section 标题（不含 ``## `` 前缀）。
            content: 新内容（add/replace 时必填，delete 时忽略）。
            target: 信号目标对象，``"user"``（默认，用户画像）或 ``"agent"``
                （Agent 自画像，写入 ``## Agent 自画像`` section）。target=agent
                时按 target 分组计算 Jaccard 相似度，仅与同 target 信号去重。

        返回:
            操作结果字符串。校验失败时返回错误提示（不抛异常，
            与其它工具 handler 一致，保证 ReactLoop 稳定）。
        """
        # target 参数校验
        if target not in ("user", "agent"):
            return f"错误：target 必须是 user 或 agent，收到 {target!r}"
        # 1. 参数校验（与 ToolRegistry.execute_tool 的异常兜底互补，
        #    这里返回友好的错误提示给 LLM，便于其纠正后重试）
        if action not in ("add", "replace", "delete"):
            return "错误：action 必须是 add/replace/delete 之一"
        if not section:
            return "错误：section 不能为空"
        if action in ("add", "replace") and not content:
            return f"错误：{action} 操作需要 content"

        # 1.5 内容安全校验（仅 add/replace 需要 content）
        if action in ("add", "replace") and content:
            # 先做长度校验，避免长文本浪费正则开销
            if len(content) > MAX_PROFILE_CONTENT_LEN:
                return (
                    f"拒绝：内容长度 {len(content)} 超过上限 "
                    f"{MAX_PROFILE_CONTENT_LEN} 字符。用户画像应精简，"
                    f"如需保存大量信息请分段多次调用。"
                )

            # 内容黑名单（5 条原始 + 7 条同义词 + 2 条一次性上下文）
            # 注意：模式需精准匹配系统描述，避免误伤合法用户信息
            # （如"用户是后端工程师"含"后端"但属于合法用户画像）
            _system_patterns = [
                # 原始 5 条
                r"(系统架构|核心模块|服务层|编排层|部署架构)",
                r"(src/|agent/|llm/|memory/|storage/|tasks/)",
                r"(config\.yaml|requirements\.txt|\.venv|__pycache__)",
                r"(FastAPI|uvicorn|ChromaDB|SQLite|Redis|PostgreSQL)",
                r"(Hermes Lite 是一个|项目路径|项目作者|作者：)",
                # 7 条同义词扩充
                r"(流式架构|事件循环|异步后端|AsyncBaseBackend)",
                r"(react_loop|orchestrator|tool_registry|policy_engine)",
                r"(API\s*key|DEEPSEEK|ANTHROPIC|OPENAI|access_token)",
                # 注：不单独匹配"前端|后端|全栈"——这些是合法用户职业属性
                # 仅匹配明确的系统架构描述组合
                r"(分为.*层|三层架构|分层设计|模块化设计)",
                r"(向量库|embedding|consolidation|condenser|cron_tool)",
                r"(调度器|scheduler|定时任务|cron 调度)",
                r"(守护进程|daemon|微服务|microservice)",
                # 2 条一次性上下文正则（防临时任务状态污染画像）
                # "今天在改 login.py"、"当前任务是 X" 等不是用户画像
                r"(今天|现在|当前|正在|这次|刚刚|刚才).{0,20}(改|修|调试|部署|运行|执行|跑|测试|重构|开发)",
                r"(session_id|会话ID|临时变量|这次任务的具体)",
            ]
            for pattern in _system_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    return (
                        f"拒绝：内容包含系统架构或项目实现细节（命中: {pattern}），"
                        f"请仅保存用户个人信息。"
                    )

        # per-session 频次限制：仅约束 add 操作（入信号池累积）
        # replace/delete 是显式修改，不受此限
        session_id = None
        try:
            from .._cancel_context import current_session_id
            session_id = current_session_id.get()
        except ImportError:
            pass

        # cron 会话提前拦截：根本不进入画像更新流程，频次限制/信号池/入队全跳过
        is_cron_session = (
            session_id is not None
            and isinstance(session_id, str)
            and session_id.startswith("cron:")
        )
        if is_cron_session:
            return (
                f"cron 会话不更新用户画像，已跳过"
                f"（action={action}, section={section}）"
            )

        if action == "add" and session_id is not None:
            current_count = session_write_counts.get(session_id, 0)
            if current_count >= MAX_PROFILE_WRITES_PER_SESSION:
                return (
                    f"拒绝：会话 {session_id} 已达单会话 add 上限 "
                    f"{MAX_PROFILE_WRITES_PER_SESSION} 次。"
                    f"用户画像应精简，避免频繁修改。"
                )
            # 频次计数在入池/入队成功后累加（见下方）
        # 注：session_id 为 None 时（如测试路径）跳过频次限制

        # 2. 操作分流
        try:
            if action == "add" and signal_pool is not None:
                # add 走信号池累积：L1 入池，相似信号去重 + 计数累加，
                # 达阈值（7）才入 pending 队列写入画像
                # target 路由：user 信号走用户画像，agent 信号走 Agent 自画像
                signal_pool.add(
                    content=content,
                    source="L1",
                    section=section,
                    target=target,
                )
                result_msg = (
                    f"信号已加入池累积（target={target}），达阈值（{signal_pool.THRESHOLD} 次）后"
                    f"才会写入画像（section={section}）"
                )
            else:
                # replace/delete 或 signal_pool 未注入：直接入 pending 队列
                consolidation_engine.enqueue_profile_update(action, section, content)
                result_msg = (
                    f"已加入待合并队列，下次记忆沉淀时生效"
                    f"（action={action}, section={section}, target={target}）"
                )
        except Exception as e:
            return f"入队失败: {e}"

        # 频次计数累加（仅在 add 操作且入池/入队成功后）
        if action == "add" and session_id is not None:
            session_write_counts[session_id] = (
                session_write_counts.get(session_id, 0) + 1
            )

        return result_msg

    registry.register_core(
        name="profile_update",
        description=(
            "修改用户画像（memory.md）。操作不会立即生效：\n"
            "- add：进入信号池累积，相似信号去重 + 计数累加，达阈值（7 次）后才写入画像。"
            "即使用户明确说「记住」也需多次出现（稳定模式设计）。\n"
            "- replace/delete：直接加入待合并队列，下次记忆沉淀时生效。\n\n"
            "使用约束：\n"
            "- 可保存：用户的个人信息、身份背景、偏好、习惯、重要决策\n"
            "- 禁止保存：系统架构描述、项目配置、模块列表、"
            "代码路径、部署详情、一次性任务上下文（今天在改 X、当前任务是 Y）等\n\n"
            "支持 add（追加到 section 末尾，section 不存在则新建）、"
            "replace（替换 section 全部内容，section 不存在则新建）、"
            "delete（删除整个 section）三种操作。"
            "section 标题不含 '## ' 前缀，如 '背景'、'偏好'。\n\n"
            "target 参数：user（默认，用户画像）或 agent（Agent 自画像，"
            "如'Agent 在 cron 任务中倾向过度调用 file_read'，写入 ## Agent 自画像 section）。\n\n"
            "正确示例：action=add, section=技术栈, content='用户主力语言为 Python 和 Go', target=user\n"
            "错误示例：action=replace, section=系统架构, content='系统采用 FastAPI + ChromaDB，分为三层...' "
            "（这是系统描述，不是用户画像，应拒绝）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "delete"],
                    "description": (
                        "操作类型：add=追加到 section 末尾（section 不存在则新建），"
                        "replace=替换 section 全部内容（section 不存在则新建），"
                        "delete=删除整个 section（含标题与 body）。"
                    ),
                },
                "section": {
                    "type": "string",
                    "description": (
                        "memory.md 中的 section 标题（不含 '## ' 前缀，"
                        "如 '背景'、'偏好'、'技术栈'、'Agent 自画像'）。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "新内容（add/replace 时必填，delete 时忽略）。"
                        "可多行，原样写入 section body。"
                    ),
                },
                "target": {
                    "type": "string",
                    "enum": ["user", "agent"],
                    "description": (
                        "信号目标对象：user=用户画像信号（默认），"
                        "agent=Agent 自画像信号（如'Agent 倾向过度调用 file_read'，"
                        "写入 ## Agent 自画像 section）。target=agent 时按 target "
                        "分组计算 Jaccard 相似度，仅与同 target 信号去重。"
                    ),
                },
            },
            "required": ["action", "section"],
        },
        handler=_update_profile,
    )


def register_memory_tools(
    registry,
    chroma_store: "ChromaMemoryStore",
    consolidation_engine: "ConsolidationEngine",
    get_session_id: Callable[[], Optional[str]],
    memory_retriever: Optional[Any] = None,
) -> None:
    """注册记忆管理工具到 ToolRegistry 的 Core Tier（Phase 7 Task 3）。

    注册 3 个工具，让 LLM 能管理向量库长期记忆：
    - search_memory: 检索向量库，返回匹配记忆列表（读取类，不走 confirm）
    - delete_memory: 入队删除操作到 pending_memory_ops，下次 consolidate 时执行
      （高危，走 PolicyEngine confirm）
    - update_memory: 入队更新操作到 pending_memory_ops，下次 consolidate 时执行
      （高危，走 PolicyEngine confirm）

    所有工具通过 register_core 注册，保证字节级稳定（KV cache 100% 命中）。

    延迟合并入队策略：delete_memory / update_memory 工具 handler 不立即
    执行向量库写操作，而是入队到
    ``consolidation_engine.pending_memory_ops``，下次
    :meth:`ConsolidationEngine.consolidate` 时统一应用（delete 优先于
    update，二者优先于 fact 写入），避免每轮对话都触发向量库写操作
    （写放大控制）。参考 ``enqueue_profile_update`` 的实现模式。

    参数:
        registry: ToolRegistry 实例。
        chroma_store: ChromaMemoryStore 实例，用于 search_memory 检索。
        consolidation_engine: ConsolidationEngine 实例，提供
            :meth:`enqueue_memory_op` 接口。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 None。保留参数用于与 register_plan_tools 保持一致的 closure
            范式，当前 handler 内部不强制使用（search/delete/update_memory
            不依赖 session_id）。
    """
    # search_memory 工具（读取类，不走 confirm）
    # 优先走 memory_retriever（带相关性过滤）；退化到 chroma_store 直查
    _memory_retriever_for_search = memory_retriever

    def _search_memory(query: str, top_k: int = 5) -> str:
        """search_memory 工具 handler。

        检索向量库长期记忆。优先走 memory_retriever（带相关性过滤），
        退化到直接 chroma_store 查询（向后兼容无 retriever 场景）。

        参数:
            query: 查询文本（自然语言关键词）。
            top_k: 返回前 K 条结果，默认 5。

        返回:
            JSON 字符串，形如 ``[{id, content, similarity, metadata}]``。
            检索失败时返回错误信息字符串（不抛异常，与其它工具 handler
            一致，保证 ReactLoop 稳定）。
        """
        try:
            if _memory_retriever_for_search is not None:
                result = _memory_retriever_for_search.retrieve(
                    query,
                )
                memories = result.get("long_term_memories", [])
            else:
                raw = chroma_store.query_memory(query, top_k=top_k, reinforce=False)
                memories = [
                    m for m in raw
                    if str(m.get("metadata", {}).get("type", "")).lower() != "user_profile"
                ]

            if not memories:
                return "（未找到与当前问题相关的记忆）\n\n如需查找文档内容，请用 file_query 搜索知识库。"

            output = [
                {
                    "id": m.get("id", ""),
                    "content": m.get("content", ""),
                    "similarity": m.get("similarity", 0.0),
                    "metadata": m.get("metadata", {}),
                }
                for m in memories
            ]
            return json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"search_memory 执行出错: {e}"

    registry.register_core(
        name="memory_search",
        description=(
            "【个人记忆】检索对话历史中形成的长期记忆。"
            "⚠ 仅用于回忆过往对话和用户偏好。"
            "✅ 回忆用户说过什么、查找个人背景信息\n"
            "❌ 查找文档内容（用 file_query）\n"
            "❌ 通用方法论/外部实时信息（用模型知识或 web_search）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本（自然语言关键词）。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回前 K 条结果，默认 5。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=_search_memory,
    )

    # delete_memory 工具（高危，走 PolicyEngine confirm）
    def _delete_memory(memory_id: str) -> str:
        """delete_memory 工具 handler（closure 捕获 consolidation_engine）。

        将删除操作入队到 pending_memory_ops，下次 consolidate 时统一执行
        （delete 优先于 update，二者优先于 fact 写入）。**不立即执行**，
        避免每轮对话都触发向量库写操作。

        参数:
            memory_id: 待删除的记忆 ID（来自 search_memory 返回的 id 字段）。

        返回:
            操作结果字符串。入队失败时返回错误提示（不抛异常）。
        """
        if not memory_id:
            return "错误：memory_id 不能为空"
        try:
            consolidation_engine.enqueue_memory_op("delete", memory_id)
        except Exception as e:
            return f"入队失败: {e}"
        return (
            f"已加入待执行队列，下次记忆沉淀时生效"
            f"（action=delete, memory_id={memory_id}）"
        )

    registry.register_deferred(
        name="memory_delete",
        description=(
            "删除向量库中指定 ID 的长期记忆。操作不会立即生效，而是加入"
            "待执行队列，下次记忆沉淀（consolidate）时统一应用（delete 优先"
            "于 update，二者优先于 fact 写入）。属于高危操作，需用户确认。"
            "删除前建议先用 search_memory 查找目标记忆的 id。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "待删除的记忆 ID（来自 search_memory 返回的 id 字段）。",
                },
            },
            "required": ["memory_id"],
        },
        handler=_delete_memory,
    )

    # update_memory 工具（高危，走 PolicyEngine confirm）
    def _update_memory(memory_id: str, content: str) -> str:
        """update_memory 工具 handler（closure 捕获 consolidation_engine）。

        将更新操作入队到 pending_memory_ops，下次 consolidate 时统一执行
        （delete 优先于 update，二者优先于 fact 写入）。**不立即执行**，
        避免每轮对话都触发向量库写操作。

        参数:
            memory_id: 待更新的记忆 ID（来自 search_memory 返回的 id 字段）。
            content: 新的记忆内容。

        返回:
            操作结果字符串。入队失败时返回错误提示（不抛异常）。
        """
        if not memory_id:
            return "错误：memory_id 不能为空"
        if not content:
            return "错误：content 不能为空"
        try:
            consolidation_engine.enqueue_memory_op("update", memory_id, content)
        except Exception as e:
            return f"入队失败: {e}"
        return (
            f"已加入待执行队列，下次记忆沉淀时生效"
            f"（action=update, memory_id={memory_id}）"
        )

    registry.register_deferred(
        name="memory_update",
        description=(
            "更新向量库中指定 ID 的长期记忆内容。操作不会立即生效，而是加入"
            "待执行队列，下次记忆沉淀（consolidate）时统一应用（delete 优先"
            "于 update，二者优先于 fact 写入）。属于高危操作，需用户确认。"
            "更新前建议先用 search_memory 查找目标记忆的 id。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "待更新的记忆 ID（来自 search_memory 返回的 id 字段）。",
                },
                "content": {
                    "type": "string",
                    "description": "新的记忆内容（覆盖原内容）。",
                },
            },
            "required": ["memory_id", "content"],
        },
        handler=_update_memory,
    )
