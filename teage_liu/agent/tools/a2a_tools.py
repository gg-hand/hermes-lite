"""A2A 协作工具：list_remote_agents / send_remote_message。

让 agent（LLM）在协作会话中：
- list_remote_agents：合并本地 AgentRegistry + A2A 远程端点 list_agents 结果，
  给出全集群 agent 视图（agent_id / role / capabilities / endpoint）。
- send_remote_message(target_agent_id, content)：向其他 agent 发协作请求，
  触发目标 agent 的紧急 LLM 响应。支持 to="*" 广播或定向通信。

工具仅在 a2a.enabled=True 时由 lifespan 调用 register_a2a_tools 注册到
ToolRegistry 的 Core Tier。

实现要点：
- 工具 handler 签名为同步（ToolRegistry.execute_tool 通过 `tool.handler(**input)`
  同步调用），而 A2AClient / AgentRegistry 接口为 async。用 _run_async 在同步
  handler 内部桥接运行 coroutine——主事件循环正在跑时用独立线程 + 新 loop。
- send_remote_message 同时写入本地 collaboration.md（让本机 WorkerAdapter 在
  to 匹配时拾取）并转发到所有 remote_endpoints（对端 WorkerAdapter 拾取）。
  _handle_request 的 to/from 过滤确保只有目标 agent 触发 LLM。
- message_id 跨实例幂等去重：相同 message_id 重复写入返回 (existing_seq, True)。
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from pathlib import Path
    from teage_liu.multiagent.a2a_client import A2AClient

logger = logging.getLogger(__name__)

# 阶段 0.2：协作上下文 contextvar，供 worker_adapter._trigger_urgent_llm 设置，
# 让 send_remote_message 工具 handler 能感知当前协作会话 ID，并标记 LLM 是否
# 已通过工具发消息（fallback 检测：未调用工具时由系统代写 response）。
# 注：工具 handler 在主事件循环线程内同步执行（ToolRegistry.execute_tool 同步调用），
# 因此 contextvar 在主线程内读写可见，无需跨线程传递。
_current_collab_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "a2a_tools._current_collab_id", default=None
)
_collab_tool_called: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "a2a_tools._collab_tool_called", default=False
)
# 弹性协作：当前正在生成的协作轮次（由 _trigger_urgent_llm 设置）。
# send_remote_message 工具据此写入消息的 collab_round 字段，否则工具路径
# 发出的 response 缺 collab_round，对方 peer response 分支不触发 LLM（协作停滞）。
_current_collab_round: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "a2a_tools._current_collab_round", default=None
)
# 弹性协作：LLM 是否通过工具发送了 extend 消息。_trigger_urgent_llm 据此在
# LLM 返回后 bump 发送方本地的 _collab_max_rounds（接收方在收到 extend 时 bump 自己的）。
_collab_extended: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "a2a_tools._collab_extended", default=False
)
# P3-3 工具层闸门：标记本次 _trigger_urgent_llm 调用内是否已通过工具发出协作消息。
# send_remote_message handler 据此限制单次 LLM 响应只发 1 条协作消息，
# 阻断 LLM 在一次响应中连发多条导致的同回合风暴（line 1280 闸门在 LLM 触发前
# 检查，无法约束 LLM 单次响应内的多工具调用）。由 _trigger_urgent_llm 在调用前
# reset 为 False，每次 LLM 调用独立计数。
_collab_round_sent: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "a2a_tools._collab_round_sent", default=False
)


def _run_async(coro) -> Any:
    """在同步工具 handler 中运行 async coroutine。

    工具 handler 由 ToolRegistry.execute_tool 同步调用，但 A2AClient /
    AgentRegistry 均为 async 接口。本辅助函数处理两种场景：

    1. 无运行中的事件循环（如 sync CLI 上下文）：直接 asyncio.run(coro)
    2. 已在事件循环中（如 orchestrator 的 LLM 工具调用上下文，FastAPI
       handler 内）：不能 asyncio.run（会抛 "already running"），改为
       在独立线程中创建新事件循环运行 coro，避免阻塞主循环。

    Args:
        coro: 待运行的 coroutine 对象。

    Returns:
        coroutine 的返回值。

    Raises:
        原 coroutine 抛出的任何异常。
    """
    try:
        asyncio.get_running_loop()
        # 已在事件循环中——独立线程跑新 loop，避免 "can't run from running loop"
        result_box: list[Any] = []
        error_box: list[BaseException] = []

        def _runner() -> None:
            try:
                new_loop = asyncio.new_event_loop()
                try:
                    result_box.append(new_loop.run_until_complete(coro))
                finally:
                    new_loop.close()
            except BaseException as e:  # noqa: BLE001
                error_box.append(e)

        t = threading.Thread(target=_runner, name="a2a-tool-async-runner")
        t.start()
        t.join()
        if error_box:
            raise error_box[0]
        return result_box[0] if result_box else None
    except RuntimeError:
        # 无运行中的事件循环——直接 asyncio.run
        return asyncio.run(coro)


def register_a2a_tools(
    registry,
    bb_root: "Path",
    a2a_client: Optional["A2AClient"],
    local_agent_id: str,
    std_client: Optional[Any] = None,
) -> None:
    """注册 A2A 协作工具到 ToolRegistry 的 Core Tier。

    仅在 a2a.enabled=True 时由 lifespan 调用注册。注册两个工具：
    - list_remote_agents：列出本地 + 远程所有 active agent
    - send_remote_message：向目标 agent 发协作消息触发其 LLM 响应

    Args:
        registry: ToolRegistry 实例。
        bb_root: 本地 blackboard 根目录（用于读取本地 agents/ + 写入 collaboration.md）。
        a2a_client: A2AClient 实例（已配置 remote_endpoints）。None 时不注册。
        local_agent_id: 本 worker 的 agent_id（作为消息 from 字段）。
        std_client: StdA2AClient 实例（a2a.standard.mode=standard 时传入，
            send_remote_message 走标准 message/send 通道）。
    """
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.blackboard import append_collab_message
    from teage_liu.multiagent.schema_validator import SchemaValidator

    # ------------------------------------------------------------------ #
    # list_remote_agents 工具
    # ------------------------------------------------------------------ #
    def _list_remote_agents() -> str:
        """列出本地 + 远程所有 active agent。"""
        try:
            # 本地 agent
            local_agents: list[dict] = []
            try:
                sv = SchemaValidator()
                reg = AgentRegistry(bb_root, sv)
                local_agents = _run_async(reg.list_active_agents())
            except Exception as e:
                logger.warning("list_remote_agents 本地查询失败: %s", e)

            # 远程 agent
            remote_results: dict[str, Any] = {}
            if a2a_client is not None:
                try:
                    remote_results = _run_async(
                        a2a_client.call_all_endpoints("list_agents", {})
                    )
                except Exception as e:
                    logger.warning("list_remote_agents 远程调用失败: %s", e)

            # 合并视图：远程结果中 Exception 转为 {"error": ...}
            remote_view: dict[str, Any] = {}
            for ep_name, res in remote_results.items():
                if isinstance(res, Exception):
                    remote_view[ep_name] = {"error": str(res)}
                else:
                    remote_view[ep_name] = res

            output = {
                "local": local_agents,
                "remote": remote_view,
            }
            return json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"list_remote_agents 执行出错: {e}"

    registry.register_core(
        name="list_remote_agents",
        description=(
            "【A2A 协作】列出本地与所有远程端点的 active agent。"
            "返回 {local: [...], remote: {endpoint: {agents: [...]}}} 结构。"
            "用于发现可协作的对端 agent（含 agent_id / role / capabilities / endpoint）。"
            "✅ 查找可协作伙伴、确认目标 agent_id 用于 send_remote_message\n"
            "❌ 查询本地已上传文件（请用 file_list_uploads）"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_list_remote_agents,
    )

    # ------------------------------------------------------------------ #
    # send_remote_message 工具
    # ------------------------------------------------------------------ #
    def _send_remote_message(
        target_agent_id: str,
        content: str,
        collab_id: Optional[str] = None,
        msg_type: str = "request",
        wait_for_response: bool = False,
        timeout: int = 60,
    ) -> str:
        """向目标 agent 发协作消息（触发其 LLM 响应）。"""
        try:
            if not target_agent_id:
                return "错误：target_agent_id 不能为空"
            if not content:
                return "错误：content 不能为空"

            # 阶段 0.2：contextvar 兜底——LLM 未显式传 collab_id 时，
            # 沿用 worker_adapter._trigger_urgent_llm 设置的当前协作上下文。
            ctx_collab_id = _current_collab_id.get()
            if collab_id is None and ctx_collab_id is not None:
                collab_id = ctx_collab_id

            # 生成跨实例幂等 message_id（内容 hash + uuid 后缀，相同内容重复提交不重复写入）
            content_hash = hashlib.md5(
                f"{local_agent_id}:{target_agent_id}:{content}".encode("utf-8")
            ).hexdigest()[:16]
            message_id = f"agent_msg_{content_hash}_{uuid.uuid4().hex[:8]}"

            params = {
                "from": local_agent_id,
                "to": target_agent_id,
                "content": content,
                "message_id": message_id,
                # msg_type 同时写入 type 字段以复用 _handle_collab_message 路由
                # （request→_handle_request / response→peer response 分支 /
                #  consensus / end 为新语义，路由无匹配分支则仅落盘供工作台展示）
                "type": msg_type,
                "msg_type": msg_type,
            }
            if collab_id is not None:
                params["collab_id"] = collab_id
            # 弹性协作：写入当前轮次，让对方 peer response 分支能据此触发 LLM
            # （工具路径曾因缺 collab_round 导致协作停滞）。extend 消息也带轮次，
            # 接收方据此 +1 继续协作。
            ctx_collab_round = _current_collab_round.get()
            if ctx_collab_round is not None:
                params["collab_round"] = ctx_collab_round

            # P3-3 工具层同回合闸门：在一次 _trigger_urgent_llm 调用内
            # （ctx_collab_round 非 None 表示处于协作 LLM 上下文）仅允许发送 1 条
            # 协作消息。LLM 单次响应中第 2 次及以后调用 send_remote_message 直接拦截，
            # 避免同回合连发风暴。worker_adapter line 1280 的闸门在 LLM 触发前检查，
            # 无法约束 LLM 单次响应内的多工具调用，故在此补工具层闸门。
            if ctx_collab_round is not None and _collab_round_sent.get():
                logger.info(
                    "send_remote_message 同回合闸门拦截：agent=%s collab=%s round=%s "
                    "本次 LLM 响应已发过消息，阻止重复发送",
                    local_agent_id, collab_id, ctx_collab_round,
                )
                return json.dumps({
                    "ok": False,
                    "blocked": "same_round_gate",
                    "reason": (
                        "本回合已发送过协作消息（每回合仅允许发送 1 条），"
                        "本次发送已被阻止。请勿重复发送，等待对端回复后再继续。"
                    ),
                    "collab_id": collab_id,
                    "collab_round": ctx_collab_round,
                }, ensure_ascii=False, indent=2)

            # 标准 A2A 模式：message/send 直达目标端点
            if std_client is not None:
                return _run_async(_send_remote_message_standard(
                    target_agent_id=target_agent_id,
                    content=content,
                    collab_id=collab_id,
                    message_id=message_id,
                    params=params,
                    std_client=std_client,
                    bb_root=bb_root,
                    local_agent_id=local_agent_id,
                    wait_for_response=wait_for_response,
                    timeout=timeout,
                ))

            # 本地写入（若 to 匹配本机，本机 WorkerAdapter 会拾取触发 LLM）
            local_seq: Optional[int] = None
            local_dedup = False
            try:
                seq, dedup = _run_async(
                    append_collab_message(bb_root, params, collab_id=collab_id)
                )
                local_seq = seq
                local_dedup = dedup
            except Exception as e:
                logger.warning("send_remote_message 本地写入失败: %s", e)

            # 远程转发（best-effort，失败不影响本地写入结果）
            forwarded: list[dict] = []
            if a2a_client is not None:
                try:
                    results = _run_async(
                        a2a_client.call_all_endpoints("agent_message", params)
                    )
                    for ep_name, res in results.items():
                        if isinstance(res, Exception):
                            forwarded.append({
                                "endpoint": ep_name,
                                "ok": False,
                                "error": str(res),
                            })
                        else:
                            forwarded.append({
                                "endpoint": ep_name,
                                "ok": True,
                                "result": res,
                            })
                except Exception as e:
                    forwarded.append({"endpoint": "*", "ok": False, "error": str(e)})

            # 阶段 0.2：标记本次 LLM 调用已通过工具发消息，
            # 供 worker_adapter._trigger_urgent_llm 检测是否走 fallback 代写路径。
            _collab_tool_called.set(True)
            # P3-3：标记本次 LLM 响应已发过协作消息，后续同响应内的 send_remote_message
            # 调用将被上面的同回合闸门拦截。仅在协作 LLM 上下文（ctx_collab_round 非 None）
            # 内标记，避免影响非协作场景的工具调用。
            if ctx_collab_round is not None:
                _collab_round_sent.set(True)
            # 弹性协作：msg_type=extend 时标记，供 _trigger_urgent_llm 在 LLM 返回后
            # bump 发送方本地的 _collab_max_rounds（接收方收到 extend 时 bump 自己的）。
            if msg_type == "extend":
                _collab_extended.set(True)

            # 阶段 0.2 / 2.1：subagent 阻塞模式——wait_for_response=True 时
            # 轮询同 collab_id 下 to=自己 的 response，超时返回 timeout 错误。
            # 注：轮询在 _run_async 的独立线程 + 新事件循环中执行，主循环在此期间
            # 被阻塞（t.join），适用于跨进程 subagent 调用（对端 worker 独立进程运行）。
            waited_response: Optional[dict] = None
            timed_out = False
            if wait_for_response and collab_id is not None and local_seq is not None:
                try:
                    waited_response = _run_async(_wait_for_response(
                        bb_root, collab_id, local_agent_id, local_seq, timeout,
                    ))
                    if waited_response is None:
                        timed_out = True
                except Exception as e:
                    logger.warning("send_remote_message 等待响应失败: %s", e)
                    timed_out = True

            result = {
                "ok": True,
                "from": local_agent_id,
                "to": target_agent_id,
                "message_id": message_id,
                "collab_id": collab_id,
                "msg_type": msg_type,
                "local_seq": local_seq,
                "local_deduplicated": local_dedup,
                "forwarded": forwarded,
            }
            if wait_for_response:
                result["wait_for_response"] = True
                result["timed_out"] = timed_out
                result["response"] = waited_response
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"send_remote_message 执行出错: {e}"

    async def _wait_for_response(
        bb_root_: "Path",
        collab_id_: str,
        self_agent_id: str,
        sent_seq: int,
        timeout_: int,
    ) -> Optional[dict]:
        """轮询同 collab_id 下 to=自己 且 seq > sent_seq 的 response/consensus/end 消息。

        Args:
            bb_root_: 黑板根目录。
            collab_id_: 协作会话 ID（限定读取 collabs/{collab_id_}.md）。
            self_agent_id: 本 agent_id，用于匹配 to 字段。
            sent_seq: 已发出消息的 seq，仅匹配 seq 严格大于此值的消息（避免拾取自己刚发的）。
            timeout_: 超时秒数。

        Returns:
            匹配到的消息 dict；超时返回 None。
        """
        from teage_liu.multiagent.blackboard import read_collab_messages

        deadline = time.time() + timeout_
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                msgs = await read_collab_messages(bb_root_, collab_id=collab_id_)
            except Exception as e:
                logger.warning("_wait_for_response 读取失败: %s", e)
                continue
            for m in msgs:
                seq = m.get("seq")
                to = m.get("to", "")
                mtype = m.get("msg_type") or m.get("type", "")
                if (isinstance(seq, int) and seq > sent_seq
                        and to == self_agent_id
                        and mtype in ("response", "consensus", "end")):
                    return m
        return None

    registry.register_core(
        name="send_remote_message",
        description=(
            "【A2A 协作】向目标 agent 发协作消息，触发其 LLM 响应。"
            "target_agent_id=\"*\" 表示广播所有 agent，或指定具体 agent_id 定向通信。"
            "消息会写入本地 collaboration.md 并转发到所有远程端点，由目标 worker 的 "
            "WorkerAdapter 轮询拾取后触发紧急 LLM 响应。\n"
            "✅ 协作请求、任务委托、信息询问\n"
            "❌ 给用户回复（直接输出即可，无需调用工具）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target_agent_id": {
                    "type": "string",
                    "description": "目标 agent_id，或 \"*\" 表示广播所有 agent。",
                },
                "content": {
                    "type": "string",
                    "description": "协作消息内容（自然语言，目标 agent 的 LLM 会据此响应）。",
                },
                "collab_id": {
                    "type": "string",
                    "description": (
                        "协作会话 ID。发起协作时由系统生成，后续消息沿用同一 collab_id。"
                        "未传时自动沿用当前协作上下文（由系统注入）。"
                    ),
                },
                "msg_type": {
                    "type": "string",
                    "description": (
                        "消息类型：request（请求）/ response（响应）/ "
                        "consensus（达成共识，终止协作）/ end（结束协作）。"
                        "默认 request。"
                    ),
                    "default": "request",
                },
                "wait_for_response": {
                    "type": "boolean",
                    "description": (
                        "是否阻塞等待对方响应（subagent 模式）。True 时挂起当前流程，"
                        "轮询同 collab_id 下 to=自己 的 response，收到后返回。"
                        "被调用方回复时务必设为 False，避免双向阻塞。默认 False（讨论模式）。"
                    ),
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "wait_for_response=True 时的等待超时秒数，默认 60。",
                    "default": 60,
                },
            },
            "required": ["target_agent_id", "content"],
        },
        handler=_send_remote_message,
    )


async def _send_remote_message_standard(
    *,
    target_agent_id: str,
    content: str,
    collab_id: str,
    message_id: str,
    params: dict,
    std_client: Any,
    bb_root: "Path",
    local_agent_id: str,
    wait_for_response: bool,
    timeout: int,
) -> str:
    """标准 A2A 通道的 worker 协作消息：message/send 直达目标端点。"""
    from teage_liu.multiagent.a2a_std.client import StdA2AClientError
    from teage_liu.multiagent.a2a_std.models import Message, TextPart
    from teage_liu.multiagent.blackboard import append_collab_message
    from teage_liu.agent.tools.main_session_collab_tools import _resolve_std_endpoint

    endpoint_name = await _resolve_std_endpoint(std_client, target_agent_id)
    if endpoint_name is None:
        return json.dumps({
            "ok": False, "error": f"未找到目标 agent {target_agent_id} 的端点",
        }, ensure_ascii=False, indent=2)

    # 本地镜像
    try:
        await append_collab_message(bb_root, params, collab_id=collab_id)
    except Exception as e:
        logger.warning("send_remote_message(standard) 本地写入失败: %s", e)

    # message/send（携带 worker 协作元数据，对端 WorkerAdapter 拾取）
    msg = Message(
        role="user",
        message_id=message_id,
        parts=[TextPart(text=content)],
        metadata={
            "from": local_agent_id,
            "to": target_agent_id,
            "type": params.get("type", "request"),
            "channel": params.get("channel", "a2a_std"),
        },
    )
    try:
        task_wire = await std_client.message_send(endpoint_name, msg, context_id=collab_id)
    except StdA2AClientError as e:
        return json.dumps({"ok": False, "error": f"message/send 失败: {e}"},
                           ensure_ascii=False, indent=2)

    forwarded = [{"endpoint": endpoint_name, "ok": True, "via": "a2a-standard",
                  "task_id": task_wire.get("id", "")}]

    # 可选阻塞等待响应
    response: Optional[dict] = None
    timed_out = False
    if wait_for_response:
        task_id = task_wire.get("id", "")
        if task_id:
            try:
                terminal = await std_client.wait_for_terminal(
                    endpoint_name, task_id, context_id=collab_id, timeout=timeout,
                )
                artifacts = terminal.get("artifacts") or []
                if artifacts:
                    parts = artifacts[-1].get("parts") or []
                    text = parts[0].get("text") if parts else None
                    if text:
                        response = {"from": target_agent_id, "content": text}
            except StdA2AClientError:
                timed_out = True

    return json.dumps({
        "ok": not timed_out,
        "collab_id": collab_id,
        "from": local_agent_id,
        "target_agent_id": target_agent_id,
        "forwarded": forwarded,
        "response": response,
        "via": "a2a-standard",
    }, ensure_ascii=False, indent=2)


__all__ = [
    "register_a2a_tools",
    "_current_collab_id",
    "_collab_tool_called",
    "_current_collab_round",
    "_collab_extended",
    "_collab_round_sent",
]
