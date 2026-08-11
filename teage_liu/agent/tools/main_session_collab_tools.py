"""主会话协作工具：list_collab_agents / request_collaboration。

主会话（用户聊天 session）的 LLM 通过这两个工具自主发起与其他 agent 的
协作（peer-collab 语义，一次性委托 + 阻塞等待精简结果）。

与 worker 协作工具（a2a_tools.send_remote_message / list_remote_agents）的
区别：
- 独立命名与描述（peer-collab 话术，不引入 collab_id/msg_type/round 等
  worker 术语，避免主会话 LLM 语言混淆）。
- 独立通道：消息带 ``channel: "main_session"``，collab_id 前缀 ``main_``，
  主会话协作**不走 worker 共享协作消息通道**（worker 协作逻辑过滤不处理）。
- A2A 端到端传输：``collab_message`` 方法广播转发，路由靠 ``to`` 字段 /
  ``collab_id`` 存在性，不做端点解析。
- 每端镜像：消息写本地 ``collabs/main_{cid}.md``，工作台 SSE 实时观察到
  真实 agent 间消息（非"进度+摘要"）。

注册条件（lifespan 装配）：
- ``multiagent.enabled`` 且 ``main_session_collab.enabled`` 且
  ``register_tools`` 且 ``a2a.enabled``（A2A 统一传输）。
- 单实例/无对端时功能静默：list_collab_agents 返回空目标，
  request_collaboration 对 self / 不可达目标返回干净错误/超时文本。

实现要点：
- 工具 handler 为同步签名（ToolRegistry.execute_tool 同步调用）；async 逻辑
  经 ``_run_async`` 桥接。request_collaboration 注册为 ``blocking=True``，
  由 sync/stream runner 用 asyncio.to_thread 放到工作线程执行——线程内无
  运行中事件循环，``_run_async`` 走 ``asyncio.run`` 分支，不冻结主循环。
- cancel_event 经 ``current_cancel_event`` contextvar 读取，轮询循环内检查。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from pathlib import Path
    from teage_liu.multiagent.a2a_client import A2AClient

logger = logging.getLogger(__name__)

# 复用 a2a_tools 的同步↔async 桥接（独立线程 + 新事件循环 / asyncio.run）
try:
    from teage_liu.agent.tools.a2a_tools import _run_async
except Exception:  # pragma: no cover - 兜底独立实现
    def _run_async(coro) -> Any:
        import asyncio
        import threading

        try:
            asyncio.get_running_loop()
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

            t = threading.Thread(target=_runner, name="main-collab-async-runner")
            t.start()
            t.join()
            if error_box:
                raise error_box[0]
            return result_box[0] if result_box else None
        except RuntimeError:
            import asyncio as _asyncio

            return _asyncio.run(coro)


def _current_cancel_event() -> Optional[Any]:
    """读取当前工具调用的取消事件（contextvar，由 ToolExecutor 设置）。"""
    try:
        from teage_liu.agent._cancel_context import current_cancel_event

        return current_cancel_event.get()
    except Exception:  # pragma: no cover
        return None


def _gen_message_id(local_agent_id: str, target: str, content: str) -> str:
    """生成跨端幂等 message_id（内容 hash + uuid 后缀）。"""
    content_hash = hashlib.md5(
        f"{local_agent_id}:{target}:{content}".encode("utf-8")
    ).hexdigest()[:16]
    return f"main_msg_{content_hash}_{uuid.uuid4().hex[:8]}"


def _truncate(content: str, max_chars: int) -> tuple[str, bool]:
    """截断内容到 max_chars，返回 (截断后文本, 是否截断)。"""
    content = (content or "").strip()
    if not content:
        return content, False
    if len(content) <= max_chars:
        return content, False
    return content[:max_chars] + f"...[截断，原文 {len(content)} 字符]", True


def register_main_session_collab_tools(
    registry,
    bb_root: "Path",
    local_agent_id: str,
    a2a_client: Optional["A2AClient"],
    msc_cfg: dict,
    std_client: Optional[Any] = None,
) -> None:
    """注册主会话协作工具到 ToolRegistry 的 Core Tier。

    Args:
        registry: ToolRegistry 实例。
        bb_root: 黑板根目录（collabs/ 镜像写入）。
        local_agent_id: 本实例 agent_id（主会话真实身份，from/to 字段用）。
        a2a_client: A2AClient 实例（None 时仅本地镜像，无跨端转发）。
        msc_cfg: ``multiagent.main_session_collab`` 配置段。
        std_client: StdA2AClient 实例（a2a.standard.mode=standard 时传入，
            工具走标准 message/send + tasks/get 通道）。
    """
    default_timeout = int(msc_cfg.get("default_timeout", 60))
    max_timeout = int(msc_cfg.get("max_timeout", 300))
    max_result_chars = int(msc_cfg.get("max_result_chars", 4000))

    # ------------------------------------------------------------------ #
    # list_collab_agents 工具
    # ------------------------------------------------------------------ #
    async def _list_collab_agents_async() -> str:
        """列出本地 + 远程所有可协作的平级 agent（排除 self，跨来源去重）。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        # 收集所有非 self 的 agent_id（本地 + 远程统一去重）
        seen_ids: set[str] = set()

        local_agents: list[dict] = []
        try:
            sv = SchemaValidator()
            reg = AgentRegistry(bb_root, sv)
            all_local = await reg.list_active_agents()
            for a in all_local:
                aid = a.get("agent_id")
                if aid and aid != local_agent_id and aid not in seen_ids:
                    seen_ids.add(aid)
                    local_agents.append(a)
        except Exception as e:
            logger.warning("list_collab_agents 本地查询失败: %s", e)

        remote_results: dict[str, Any] = {}
        if std_client is not None:
            # 标准模式：Agent Card 发现（取代 list_agents RPC）
            for ep_name in std_client.endpoints:
                try:
                    card = await std_client.fetch_agent_card(ep_name)
                    remote_results[ep_name] = {"agents": [{
                        "agent_id": card.get("name", ep_name),
                        "role": "worker",
                        "endpoint": card.get("url", ""),
                        "capabilities": [],
                        "via": "a2a-card",
                    }]}
                except Exception as e:
                    remote_results[ep_name] = {"error": str(e)}
        elif a2a_client is not None:
            try:
                remote_results = await a2a_client.call_all_endpoints("list_agents", {})
            except Exception as e:
                logger.warning("list_collab_agents 远程查询失败: %s", e)

        remote_view: dict[str, Any] = {}
        for ep_name, res in remote_results.items():
            if isinstance(res, Exception):
                remote_view[ep_name] = {"error": str(res)}
                continue
            agents = (res or {}).get("agents", [])
            filtered = []
            for a in agents:
                aid = a.get("agent_id")
                if aid and aid != local_agent_id and aid not in seen_ids:
                    seen_ids.add(aid)
                    filtered.append(a)
            remote_view[ep_name] = {"agents": filtered}

        return json.dumps(
            {
                "local": local_agents,
                "remote": remote_view,
                "self": local_agent_id,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _list_collab_agents() -> str:
        """列出本地 + 远程所有可协作的平级 agent（排除 self）。"""
        try:
            return _run_async(_list_collab_agents_async())
        except Exception as e:
            return f"list_collab_agents 执行出错: {e}"

    registry.register_core(
        name="list_collab_agents",
        description=(
            "【Agent 协作】发现当前可协作的平级 agent（本地 worker + 远程 A2A 端点）。"
            "返回 {local: [...], remote: {endpoint: {agents: [...]}}, self: <你的 agent_id>}。"
            "用于确认目标 agent_id 后调用 request_collaboration。\n"
            "✅ 主会话想找其他 agent 协作时先调用本工具\n"
            "❌ 主会话普通问答 / 查记忆（无需协作）"
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_list_collab_agents,
    )

    # ------------------------------------------------------------------ #
    # request_collaboration 工具
    # ------------------------------------------------------------------ #
    async def _request_collaboration_async(
        target_agent_id: str,
        task: str,
        timeout: Optional[int] = None,
        wait_for_response: bool = True,
    ) -> str:
        """向平级 agent 发起一次性协作请求，阻塞等待精简结果。

        单事件循环执行整条链路（append → A2A 广播 → 等待 → end），
        避免反复 ``_run_async`` 造成事件循环反复新建/关闭（曾导致
        "asyncio.run() cannot be called from a running event loop" 与超时）。
        """
        # 防御：worker 协作会话（system_prompt_override 路径）禁止调用
        try:
            from teage_liu.agent.tools.a2a_tools import _current_collab_id

            if _current_collab_id.get() is not None:
                return json.dumps({
                    "ok": False,
                    "error": "request_collaboration 仅主会话可用，协作会话请使用 worker 协作工具",
                }, ensure_ascii=False, indent=2)
        except Exception:
            pass

        if not target_agent_id:
            return "错误：target_agent_id 不能为空"
        if not task or not task.strip():
            return "错误：task 不能为空"
        if target_agent_id == local_agent_id:
            return json.dumps({
                "ok": False,
                "error": f"不能与自身协作（{target_agent_id} 就是本 agent），请选择 list_collab_agents 中的其他 agent",
            }, ensure_ascii=False, indent=2)

        # 超时钳制
        eff_timeout = int(timeout or default_timeout)
        if eff_timeout <= 0:
            eff_timeout = default_timeout
        eff_timeout = min(eff_timeout, max_timeout)

        # 标准 A2A 模式：message/send 建 Task → wait_for_terminal
        if std_client is not None:
            return await _request_collaboration_standard(
                target_agent_id=target_agent_id,
                task=task,
                eff_timeout=eff_timeout,
                wait_for_response=bool(wait_for_response),
                std_client=std_client,
                bb_root=bb_root,
                local_agent_id=local_agent_id,
                max_result_chars=max_result_chars,
            )

        from teage_liu.multiagent.blackboard import append_collab_message

        collab_id = f"main_{uuid.uuid4().hex[:12]}"
        message_id = _gen_message_id(local_agent_id, target_agent_id, task)
        params = {
            "channel": "main_session",
            "type": "request",
            "from": local_agent_id,
            "to": target_agent_id,
            "content": task,
            "collab_id": collab_id,
            "message_id": message_id,
            "priority": "high",
            "wait_for_response": bool(wait_for_response),
            "deadline": eff_timeout,
            "collab_round": 1,
        }

        # ① 本地镜像请求（自动建 collab index + 工作台 SSE 可见）
        local_seq: Optional[int] = None
        try:
            seq, _dedup = await append_collab_message(
                bb_root, params, collab_id=collab_id
            )
            local_seq = seq
            logger.info(
                "request_collaboration 发起: collab=%s from=%s to=%s timeout=%ds seq=%s",
                collab_id, local_agent_id, target_agent_id, eff_timeout, local_seq,
            )
        except Exception as e:
            logger.warning("request_collaboration 本地写入失败: %s", e)

        # ② A2A 广播转发（路由靠 to 字段，对端按 to==self 过滤）
        forwarded: list[dict] = []
        if a2a_client is not None:
            try:
                results = await a2a_client.call_all_endpoints("collab_message", params)
                for ep_name, res in results.items():
                    if isinstance(res, Exception):
                        forwarded.append({
                            "endpoint": ep_name, "ok": False, "error": str(res),
                        })
                    else:
                        forwarded.append({
                            "endpoint": ep_name, "ok": True, "result": res,
                        })
            except Exception as e:
                forwarded.append({"endpoint": "*", "ok": False, "error": str(e)})

        # ③ 阻塞等待对端响应（轮询本地镜像，同一事件循环内 await）
        response: Optional[dict] = None
        timed_out = False
        cancelled = False
        if wait_for_response and local_seq is not None:
            try:
                response = await _wait_for_response(
                    bb_root, collab_id, local_agent_id, local_seq, eff_timeout,
                )
                if response is None:
                    timed_out = True
                    logger.warning(
                        "request_collaboration 超时: collab=%s timeout=%ds", collab_id, eff_timeout,
                    )
                elif response.get("_cancelled"):
                    cancelled = True
                    response = None
                    logger.info("request_collaboration 已取消: collab=%s", collab_id)
                else:
                    logger.info(
                        "request_collaboration 收到响应: collab=%s from=%s seq=%s",
                        collab_id, response.get("from"), response.get("seq"),
                    )
            except Exception as e:
                logger.warning("request_collaboration 等待响应失败: %s", e)
                timed_out = True

        # ④ 完成后写 end 消息（归档标记，双方镜像）
        try:
            end_msg = {
                "channel": "main_session",
                "type": "end",
                "from": local_agent_id,
                "to": target_agent_id,
                "content": "协作完成",
                "collab_id": collab_id,
                "message_id": _gen_message_id(local_agent_id, target_agent_id, f"end:{collab_id}"),
                "collab_round": 1,
            }
            await append_collab_message(bb_root, end_msg, collab_id=collab_id)
            if a2a_client is not None:
                await a2a_client.call_all_endpoints("collab_message", end_msg)
        except Exception as e:
            logger.warning("request_collaboration end 消息写入失败: %s", e)

        # ⑤ 摘要截断返回
        summary: Optional[str] = None
        truncated = False
        if response is not None:
            summary, truncated = _truncate(
                response.get("content", ""), max_result_chars
            )

        result = {
            "ok": not timed_out and not cancelled,
            "collab_id": collab_id,
            "from": local_agent_id,
            "target_agent_id": target_agent_id,
            "wait_for_response": bool(wait_for_response),
            "timed_out": timed_out,
            "cancelled": cancelled,
            "forwarded": forwarded,
            "response": {
                "from": response.get("from") if response else None,
                "type": response.get("msg_type") or response.get("type") if response else None,
                "content": summary,
                "truncated": truncated,
            } if response else None,
        }
        if response is not None:
            result["summary"] = summary
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _request_collaboration(
        target_agent_id: str,
        task: str,
        timeout: Optional[int] = None,
        wait_for_response: bool = True,
    ) -> str:
        """向平级 agent 发起一次性协作请求，阻塞等待精简结果。"""
        try:
            return _run_async(_request_collaboration_async(
                target_agent_id, task, timeout, wait_for_response,
            ))
        except Exception as e:
            return f"request_collaboration 执行出错: {e}"

    registry.register_core(
        name="request_collaboration",
        description=(
            "【Agent 协作】向平级 agent 发起一次性协作请求，并阻塞等待其对任务的精简结果。"
            "会创建独立协作会话（collab_id=main_*），写入本地协作镜像并（若配置 A2A）"
            "转发到对端实例；wait_for_response=True 时阻塞至多 timeout 秒，"
            "返回对端响应摘要（截断到配置上限）。timeout 不能超过配置上限。\n"
            "✅ 主会话需要另一个 agent 的专业能力 / 独立处理一件事时\n"
            "❌ 自己能解决的事、或目标 agent 未出现在 list_collab_agents 中"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target_agent_id": {
                    "type": "string",
                    "description": "目标 agent_id（先调用 list_collab_agents 确认在线）。",
                },
                "task": {
                    "type": "string",
                    "description": "要委托的任务描述（自然语言，目标 agent 会据此处理）。",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"阻塞等待超时秒数，默认 {default_timeout}，上限 {max_timeout}。",
                },
                "wait_for_response": {
                    "type": "boolean",
                    "description": (
                        "是否阻塞等待响应。True（默认）时挂起至多 timeout 秒等待对端单次响应；"
                        "False 时立即返回（当前仍以阻塞语义为主，非阻塞回收为预留扩展）。"
                    ),
                    "default": True,
                },
            },
            "required": ["target_agent_id", "task"],
        },
        handler=_request_collaboration,
        blocking=True,
    )


async def _wait_for_response(
    bb_root_: "Path",
    collab_id_: str,
    self_agent_id: str,
    sent_seq: int,
    timeout_: int,
) -> Optional[dict]:
    """轮询本地镜像，等待对端 response/consensus/end 消息。

    匹配条件：seq > sent_seq 且 to == self_agent_id 且
    msg_type/type in (response, consensus, end)。

    Returns:
        命中的消息 dict；超时返回 None；被取消返回 {"_cancelled": True}。
    """
    from teage_liu.multiagent.blackboard import read_collab_messages

    cancel = _current_cancel_event()
    deadline = time.time() + timeout_
    while time.time() < deadline:
        if cancel is not None and cancel.is_set():
            return {"_cancelled": True}
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


async def _resolve_std_endpoint(std_client: Any, target_agent_id: str) -> Optional[str]:
    """标准模式：按 card.name 定位目标端点；无匹配时返回第一个端点。"""
    for ep_name in std_client.endpoints:
        try:
            card = await std_client.fetch_agent_card(ep_name)
        except Exception:
            continue
        if card.get("name") == target_agent_id:
            return ep_name
    return std_client.endpoints[0] if std_client.endpoints else None


async def _request_collaboration_standard(
    *,
    target_agent_id: str,
    task: str,
    eff_timeout: int,
    wait_for_response: bool,
    std_client: Any,
    bb_root: "Path",
    local_agent_id: str,
    max_result_chars: int,
) -> str:
    """标准 A2A 通道的协作请求：message/send 建 Task → wait_for_terminal → 摘要。"""
    from teage_liu.multiagent.a2a_std.client import StdA2AClientError
    from teage_liu.multiagent.a2a_std.models import Message, TextPart
    from teage_liu.multiagent.blackboard import append_collab_message, read_collab_messages

    collab_id = f"main_{uuid.uuid4().hex[:12]}"
    message_id = _gen_message_id(local_agent_id, target_agent_id, task)

    endpoint_name = await _resolve_std_endpoint(std_client, target_agent_id)
    if endpoint_name is None:
        return json.dumps({
            "ok": False, "error": f"未找到目标 agent {target_agent_id} 的端点",
        }, ensure_ascii=False, indent=2)

    # ① 本地镜像（工作台 SSE 可见）
    params = {
        "channel": "main_session",
        "type": "request",
        "from": local_agent_id,
        "to": target_agent_id,
        "content": task,
        "collab_id": collab_id,
        "message_id": message_id,
        "priority": "high",
        "wait_for_response": wait_for_response,
        "deadline": eff_timeout,
        "collab_round": 1,
    }
    try:
        await append_collab_message(bb_root, params, collab_id=collab_id)
    except Exception as e:
        logger.warning("request_collaboration(standard) 本地写入失败: %s", e)

    # ② 标准 message/send（对端建 Task 并写 collabs/{collab_id}.md，worker 拾取）
    msg = Message(
        role="user",
        message_id=message_id,
        parts=[TextPart(text=task)],
        metadata={
            "from": local_agent_id,
            "to": target_agent_id,
            "type": "request",
            "channel": "main_session",
        },
    )
    try:
        task_wire = await std_client.message_send(endpoint_name, msg, context_id=collab_id)
    except StdA2AClientError as e:
        return json.dumps({"ok": False, "error": f"message/send 失败: {e}"},
                           ensure_ascii=False, indent=2)
    task_id = task_wire.get("id", "")

    # ③ 等待终态（wait_for_terminal 轮询 tasks/get；超时回退本地镜像）
    response: Optional[dict] = None
    timed_out = False
    if wait_for_response and task_id:
        try:
            terminal = await std_client.wait_for_terminal(
                endpoint_name, task_id, context_id=collab_id, timeout=eff_timeout,
            )
            artifacts = terminal.get("artifacts") or []
            if artifacts:
                parts = (artifacts[-1].get("parts") or [])
                text = parts[0].get("text") if parts else None
                if text:
                    response = {"from": target_agent_id, "type": "response", "content": text}
        except StdA2AClientError as e:
            logger.warning("request_collaboration(standard) 等待超时: %s", e)
            timed_out = True
        if response is None and not timed_out:
            # 回退：本地镜像最新 response/consensus
            try:
                msgs = await read_collab_messages(bb_root, collab_id=collab_id)
                for m in reversed(msgs):
                    if m.get("type") in ("response", "consensus") and m.get("content"):
                        response = m
                        break
            except Exception:
                pass

    # ④ end 消息（归档标记）
    try:
        await append_collab_message(bb_root, {
            "channel": "main_session", "type": "end",
            "from": local_agent_id, "to": target_agent_id,
            "content": "协作完成", "collab_id": collab_id,
            "message_id": _gen_message_id(local_agent_id, target_agent_id, f"end:{collab_id}"),
            "collab_round": 1,
        }, collab_id=collab_id)
    except Exception as e:
        logger.warning("request_collaboration(standard) end 写入失败: %s", e)

    # ⑤ 摘要截断返回
    summary: Optional[str] = None
    truncated = False
    if response is not None:
        summary, truncated = _truncate(response.get("content", ""), max_result_chars)

    result = {
        "ok": not timed_out,
        "collab_id": collab_id,
        "from": local_agent_id,
        "target_agent_id": target_agent_id,
        "wait_for_response": wait_for_response,
        "timed_out": timed_out,
        "cancelled": False,
        "forwarded": [{"endpoint": endpoint_name, "ok": True, "via": "a2a-standard"}],
        "response": {
            "from": target_agent_id,
            "type": "response",
            "content": summary or "",
            "truncated": truncated,
        } if summary else None,
        "summary": summary,
        "via": "a2a-standard",
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = [
    "register_main_session_collab_tools",
    "_run_async",
    "_wait_for_response",
    "_request_collaboration_standard",
]
