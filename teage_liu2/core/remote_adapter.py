"""RemoteBranchAdapter(§transport.5,阶段 3 落地):异语言扩展接入 core 的适配器。

- 对 core 是普通 :class:`~teage_liu2.core.hooks.Branch`:core 不感知对方语言
- 钩子调用经 ``invoke_hook`` 消息转发到扩展进程(参数 = Invocation{hook, snapshot, args},
  返回 ActionResult{actions[]});声明的 tool_executor 走 ``invoke_tool`` 轻量通道
  (仅 name+input,免快照序列化,§transport T-5)
- 快照序列化:Snapshot.to_dict()(full 编码;低频钩子全量传输,§18.2 分层实施)
- 扩展返回 action dict → 还原为 core 的 Action 值对象(make_action,§15-A1 校验)
- protocol_version 记录(协商收敛归阶段 4 evolution 域,§10.2)

不变量(§4.2):仅调用扩展声明的 hooks_implemented;未声明的钩子返回默认值
(不发起协议往返)。setup/teardown 与生命周期同步。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .actions import ToolDecision, make_action
from .hooks import Branch, CAP_TOOL_EXECUTOR
from .injection import Injection
from .types import Snapshot

logger = logging.getLogger(__name__)

#: hooks_implemented 合法钩子名(与 lifecycle/11 钩子对齐;未知声明仅告警,不崩溃)
_KNOWN_HOOKS = frozenset({
    "setup", "teardown",
    "build_injections", "inject_round", "before",
    "pre_tool_call", "on_tool_call", "post_tool_call", "after_step",
    "after", "on_error",
})

#: capabilities 合法枚举(与 hooks.CAP_* / lifecycle.schema.json 对齐)
_KNOWN_CAPABILITIES = frozenset({
    "observe", "tool_executor", "llm", "self_hosted_storage",
})


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    return value if isinstance(value, list) else []


class RemoteBranchAdapter(Branch):
    """远程扩展适配器:钩子调用经协议消息转发,对 core 透明。"""

    def __init__(
        self,
        name: str,
        declaration: Dict[str, Any],
        channel: Any,
    ) -> None:
        from .types import is_valid_extension_name

        if not is_valid_extension_name(name):
            raise ValueError(
                f"非法 extension_name: {name!r}(必须匹配 ^[a-z0-9_]+$)"
            )
        self.name = name
        self._decl = dict(declaration or {})
        self._channel = channel
        self.capabilities: List[str] = list(self._decl.get("capabilities") or [])
        unknown_caps = set(self.capabilities) - _KNOWN_CAPABILITIES
        if unknown_caps:
            logger.warning(
                "扩展 %s 声明未知 capability: %s(仅记录,不拒绝)",
                name, sorted(unknown_caps),
            )
        self._hooks_implemented: set = set(self._decl.get("hooks_implemented") or [])
        unknown_hooks = self._hooks_implemented - _KNOWN_HOOKS
        if unknown_hooks:
            logger.warning(
                "扩展 %s 声明未知钩子: %s(仅记录,core 不会调用)",
                name, sorted(unknown_hooks),
            )
        self.protocol_version: Optional[str] = self._decl.get("protocol_version")
        # 宿主能力端口(进程内通道,装配注入;扩展可经此访问 storage_*/invoke_llm)
        self.host_port: Any = None

    # ------------------------------------------------------------------
    # 内部:协议往返
    # ------------------------------------------------------------------
    def implements(self, hook: str) -> bool:
        return hook in self._hooks_implemented

    async def _invoke(
        self,
        hook: str,
        snapshot: Optional[Snapshot] = None,
        args: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发送 invoke_hook 请求,返回 result dict。

        响应含 error 时抛异常(HookChain 隔离,单扩展异常不中断对话)。
        """
        payload: Dict[str, Any] = {
            "hook": hook,
            "snapshot": snapshot.to_dict() if snapshot is not None else None,
            "args": args or {},
        }
        frame = await self._channel.request("invoke_hook", payload, timeout=timeout)
        response = frame.payload or {}
        if "error" in response:
            error = response["error"] or {}
            raise RuntimeError(
                f"扩展 {self.name} 的 {hook} 钩子失败: "
                f"{error.get('code', 'error')}: {error.get('message', '')}"
            )
        return response.get("result") if isinstance(response.get("result"), dict) else {}

    def _actions_from(self, result: Dict[str, Any]) -> List[Any]:
        """扩展返回 action dict[] → core Action 值对象(make_action,§15-A1 校验)。"""
        actions: List[Any] = []
        for item in _as_dict_list(result.get("actions")):
            if not isinstance(item, dict) or "op" not in item:
                logger.error("扩展 %s 返回非法 action(缺少 op): %r,跳过", self.name, item)
                continue
            try:
                actions.append(make_action(item["op"], **{k: v for k, v in item.items() if k != "op"}))
            except (ValueError, TypeError) as e:
                logger.error("扩展 %s 返回非法 action(%s): %r,跳过", self.name, e, item)
        return actions

    def _injections_from(self, result: Dict[str, Any]) -> List[Injection]:
        injections: List[Injection] = []
        for item in _as_dict_list(result.get("injections")):
            if not isinstance(item, dict):
                continue
            injections.append(
                Injection(
                    layer=item.get("layer", ""),
                    content=item.get("content", ""),
                    priority=int(item.get("priority", 0) or 0),
                    key=item.get("key"),
                )
            )
        return injections

    # ------------------------------------------------------------------
    # 生命周期(仅调用声明实现的;setup 失败 = 启动失败,不 try/except 吞错)
    # ------------------------------------------------------------------
    async def setup(self, config: dict, host: Any) -> None:
        if not self.implements("setup"):
            return
        await self._invoke("setup", args={"config": config, "host": host})

    async def teardown(self) -> None:
        if not self.implements("teardown"):
            return
        try:
            await self._invoke("teardown", timeout=10.0)
        except Exception as e:
            logger.warning("扩展 %s teardown 异常: %s", self.name, e)

    # ------------------------------------------------------------------
    # 注入声明
    # ------------------------------------------------------------------
    async def build_injections(self, snapshot: Snapshot) -> List[Injection]:
        if not self.implements("build_injections"):
            return []
        result = await self._invoke("build_injections", snapshot=snapshot)
        return self._injections_from(result)

    async def inject_round(self, snapshot: Snapshot) -> Optional[Injection]:
        if not self.implements("inject_round"):
            return None
        result = await self._invoke("inject_round", snapshot=snapshot)
        items = self._injections_from(result)
        return items[0] if items else None

    # ------------------------------------------------------------------
    # LLM 前后钩子
    # ------------------------------------------------------------------
    async def before(self, snapshot: Snapshot) -> List[Any]:
        if not self.implements("before"):
            return []
        result = await self._invoke("before", snapshot=snapshot)
        return self._actions_from(result)

    async def pre_tool_call(
        self, snapshot: Snapshot, name: str, input: dict
    ) -> ToolDecision:
        if not self.implements("pre_tool_call"):
            return ToolDecision(decision="allow")
        result = await self._invoke(
            "pre_tool_call", snapshot=snapshot, args={"name": name, "input": input}
        )
        decision = result.get("decision", "allow")
        return ToolDecision(decision=decision, input=result.get("input"))

    async def on_tool_call(
        self, snapshot: Snapshot, name: str, input: dict
    ) -> Any:
        """工具执行(§transport T-5):声明 tool_executor 者 core 优先走 invoke_tool。

        经 :meth:`invoke_tool` 轻量通道(免快照序列化);未声明则走 invoke_hook。
        """
        if CAP_TOOL_EXECUTOR in self.capabilities:
            return await self.invoke_tool(name, input)
        if not self.implements("on_tool_call"):
            return NotImplemented
        result = await self._invoke(
            "on_tool_call", snapshot=snapshot, args={"name": name, "input": input}
        )
        if result.get("not_implemented"):
            return NotImplemented
        return result.get("result")

    async def invoke_tool(self, name: str, input: dict) -> Any:
        """工具执行专用消息(§transport T-5):仅 name+input,免快照序列化。"""
        frame = await self._channel.request("invoke_tool", {"name": name, "input": input})
        response = frame.payload or {}
        if "error" in response:
            error = response["error"] or {}
            return Exception(
                f"扩展 {self.name} 执行工具 {name} 失败: "
                f"{error.get('code', 'error')}: {error.get('message', '')}"
            )
        result = response.get("result")
        if result is None:
            return NotImplemented
        if result.get("not_implemented"):
            return NotImplemented
        return result.get("result")

    async def post_tool_call(
        self,
        snapshot: Snapshot,
        name: str,
        input: dict,
        result: Any,
        duration: float,
    ) -> List[Any]:
        if not self.implements("post_tool_call"):
            return []
        result_payload = await self._invoke(
            "post_tool_call",
            snapshot=snapshot,
            args={"name": name, "input": input, "result": result, "duration": duration},
        )
        return self._actions_from(result_payload)

    async def after_step(
        self, snapshot: Snapshot, summary: Any
    ) -> List[Any]:
        if not self.implements("after_step"):
            return []
        result_payload = await self._invoke(
            "after_step",
            snapshot=snapshot,
            args={"summary": _summary_to_dict(summary)},
        )
        return self._actions_from(result_payload)

    async def after(self, snapshot: Snapshot, response: Any) -> List[Any]:
        if not self.implements("after"):
            return []
        result_payload = await self._invoke(
            "after", snapshot=snapshot, args={"response": _response_to_dict(response)}
        )
        return self._actions_from(result_payload)

    async def on_error(self, snapshot: Snapshot, error: Any) -> List[Any]:
        if not self.implements("on_error"):
            return []
        result_payload = await self._invoke(
            "on_error",
            snapshot=snapshot,
            args={"error": str(error) if error is not None else ""},
        )
        return self._actions_from(result_payload)


def _summary_to_dict(summary: Any) -> Dict[str, Any]:
    """StepSummary → 协议 dict(供扩展只读)。"""
    if summary is None:
        return {}
    return {
        "round": getattr(summary, "round", 0),
        "text": getattr(summary, "text", ""),
        "content_blocks": getattr(summary, "content_blocks", None) or [],
        "tool_uses": getattr(summary, "tool_uses", None) or [],
        "usage": getattr(summary, "usage", None),
        "duration": getattr(summary, "duration", 0.0),
    }


def _response_to_dict(response: Any) -> Dict[str, Any]:
    """AfterResponse → 协议 dict(供扩展只读)。"""
    if response is None:
        return {}
    return {
        "text": getattr(response, "text", ""),
        "content_blocks": getattr(response, "content_blocks", None) or [],
        "usage": getattr(response, "usage", None),
        "done_event": getattr(response, "done_event", None),
    }
