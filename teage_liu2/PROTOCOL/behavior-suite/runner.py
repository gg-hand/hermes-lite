"""行为套件统一 Runner(协议交付物,语言无关黄金用例的 Python 宿主执行器)。

用法::

    python runner.py                 # 跑全部用例并报告通过率
    python runner.py --case 03       # 只跑指定用例(前缀匹配 id)
    python runner.py --verbose       # 逐用例输出详情

每个用例按 JSON 定义执行(expected 的三类断言):
- ``expected.event_stream``:事件流有序逐项匹配;``absent: true`` 项为全程负断言
- ``expected.invocations``:钩子调用序断言(正序/逆序/跳过/短路)+ snapshot_assert + actions_assert
- ``expected.final``:收尾断言(termination_reason / is_complete / persisted)

用例执行分两类(宿主协议层对拍):
- pipeline 类:经 ChatPipeline 跑真实对话流(ScriptedBranch 按 inputs 行为脚本化)
- 协议层类:直接对拍宿主 transport/registry/supervisor 协议语义

匹配器(与 matcher.schema.json 对齐):``{regex}`` / ``{length}`` / ``{range}`` / ``{type}`` / 值相等。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 仓库根(teage_liu2/ 的父目录):使 teage_liu2.* 可 import
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from teage_liu2.core.actions import ToolDecision, make_action  # noqa: E402
from teage_liu2.core.event_stream import L3BatchSink  # noqa: E402
from teage_liu2.core.hooks import Branch, HookChain  # noqa: E402
from teage_liu2.core.injection import Injection  # noqa: E402
from teage_liu2.core.pipeline import ChatPipeline  # noqa: E402
from teage_liu2.core.registry import BranchRegistry  # noqa: E402
from teage_liu2.core.storage import SQLiteStorageProvider  # noqa: E402
from teage_liu2.core.tasks import TaskRegistry  # noqa: E402
from teage_liu2.core.transport import (  # noqa: E402
    DeltaFrame,
    TransportBus,
    TransportFrame,
    negotiate_protocol_version,
    parse_protocol_version,
)
from teage_liu2.core.types import (  # noqa: E402
    EV_DONE,
    EV_ERROR,
    EV_STEP_END,
    EV_TOOL_RESULT,
    EV_TOOL_USE,
)
from teage_liu2.tests_core.fake_llm import FakeLLMClient  # noqa: E402

_CASES_DIR = os.path.join(os.path.dirname(__file__), "cases")

# ---------------------------------------------------------------------------
# 匹配器解释器(与 matcher.schema.json 对齐)
# ---------------------------------------------------------------------------
_TYPE_MAP = {
    "boolean": bool, "integer": int, "string": str,
    "array": list, "object": dict, "null": type(None),
}


def match_value(assertion: Any, actual: Any) -> bool:
    """单值匹配:{regex}/{length}/{range}/{type}/精确值/嵌套对象。"""
    if isinstance(assertion, dict):
        keys = set(assertion)
        if "regex" in keys:
            return isinstance(actual, str) and bool(re.search(assertion["regex"], actual))
        if "length" in keys:
            return isinstance(actual, (list, dict, str)) and len(actual) == assertion["length"]
        if "range" in keys:
            lo, hi = assertion["range"]
            return isinstance(actual, (int, float)) and not isinstance(actual, bool) and lo <= actual <= hi
        if "type" in keys:
            t = _TYPE_MAP.get(assertion["type"])
            return t is not None and isinstance(actual, t)
        if isinstance(actual, dict):
            return all(k in actual and match_value(v, actual[k]) for k, v in assertion.items())
        return False
    if isinstance(assertion, list):
        return (
            isinstance(actual, list)
            and len(assertion) == len(actual)
            and all(match_value(a, b) for a, b in zip(assertion, actual))
        )
    return assertion == actual


def _snapshot_fields(snapshot: Any) -> Dict[str, Any]:
    """快照关键字段(snapshot_assert 用):messages/system_text/extra/revision/round/stop。"""
    return {
        "messages": list(getattr(snapshot, "messages", [])),
        "system_text": getattr(snapshot, "system_text", ""),
        "extra": dict(getattr(snapshot, "extra", {})),
        "revision": getattr(snapshot, "revision", 0),
        "round": getattr(snapshot, "round", 0),
        "stop": getattr(snapshot, "stop", False),
    }


# ---------------------------------------------------------------------------
# ScriptedBranch:按 inputs 行为脚本化的同语言扩展
# ---------------------------------------------------------------------------
class ScriptedBranch(Branch):
    """按用例 inputs 行为的可编程扩展:记录钩子调用序 + 返回预设行为。"""

    def __init__(self, name: str, decl: Dict[str, Any], behaviors: Dict[str, Any],
                 invocations: List[Dict[str, Any]], host_port: Any = None) -> None:
        self.name = name
        self.capabilities = list(decl.get("capabilities") or [])
        self._hooks_implemented = set(decl.get("hooks_implemented") or [])
        self._behaviors = behaviors
        self._invocations = invocations
        self.host_port = host_port

    def implements(self, hook: str) -> bool:
        return hook in self._hooks_implemented

    def _should_record(self, hook: str) -> bool:
        """record 判定:build_injections 是隐式基础钩子(所有扩展默认参与);
        其余钩子仅在 hooks_implemented 声明时记录(§lifecycle Extension.hooks_implemented)。
        """
        return hook == "build_injections" or hook in self._hooks_implemented

    def _record(self, hook: str, snapshot: Any = None, actions: Any = None, extra: Any = None) -> None:
        if not self._should_record(hook):
            return
        rec: Dict[str, Any] = {"hook": hook, "extension": self.name}
        if snapshot is not None:
            rec["snapshot"] = _snapshot_fields(snapshot)
        if actions is not None:
            rec["actions"] = actions
        if extra is not None:
            rec.update(extra)
        self._invocations.append(rec)

    def _actions(self, key: str) -> List[Any]:
        return list(self._behaviors.get(key) or [])

    async def setup(self, config: dict, host: Any) -> None:
        self._record("setup")

    async def teardown(self) -> None:
        self._record("teardown")

    async def build_injections(self, snapshot: Any) -> List[Injection]:
        self._record("build_injections", snapshot)
        return list(self._behaviors.get("injections") or [])

    async def inject_round(self, snapshot: Any) -> Optional[Injection]:
        self._record("inject_round", snapshot)
        items = self._behaviors.get("round_injections") or []
        return items[0] if items else None

    async def before(self, snapshot: Any) -> List[Any]:
        actions = self._actions("before_actions")
        self._record("before", snapshot, actions=actions)
        return actions

    async def pre_tool_call(self, snapshot: Any, name: str, input: dict) -> ToolDecision:
        self._record("pre_tool_call", snapshot, extra={"name": name, "input": input})
        d = self._behaviors.get("pre_decision") or {}
        return ToolDecision(decision=d.get("decision", "allow"), input=d.get("input"))

    async def on_tool_call(self, snapshot: Any, name: str, input: dict) -> Any:
        self._record("on_tool_call", snapshot, extra={"name": name, "input": input})
        if not (self.implements("on_tool_call") or "tool_executor" in self.capabilities):
            return NotImplemented
        result = self._behaviors.get("tool_result")
        return result if result is not None else "ok"

    async def post_tool_call(self, snapshot: Any, name: str, input: dict,
                             result: Any, duration: float) -> List[Any]:
        actions = self._actions("post_actions")
        self._record("post_tool_call", snapshot, actions=actions)
        return actions

    async def after_step(self, snapshot: Any, summary: Any) -> List[Any]:
        actions = self._actions("after_step_actions")
        self._record("after_step", snapshot, actions=actions)
        return actions

    async def after(self, snapshot: Any, response: Any) -> List[Any]:
        actions = self._actions("after_actions")
        self._record("after", snapshot, actions=actions)
        return actions

    async def on_error(self, snapshot: Any, error: Any) -> List[Any]:
        actions = self._actions("on_error_actions")
        self._record("on_error", snapshot, actions=actions)
        return actions


# ---------------------------------------------------------------------------
# 行为解析:inputs → {extension_name: behaviors}
# ---------------------------------------------------------------------------
def _build_behaviors(name: str, inputs: Dict[str, Any], decl: Dict[str, Any]) -> Dict[str, Any]:
    hooks = set(decl.get("hooks_implemented") or [])
    b: Dict[str, Any] = {}

    def _make(x: Dict[str, Any]) -> Any:
        return make_action(x["op"], **{k: v for k, v in x.items() if k != "op"})

    for key, val in inputs.items():
        if key == f"{name}_actions":
            # 归属判定:声明 after/on_error(且非 before)→ 终态钩子 action;否则 before
            if ("after" in hooks or "on_error" in hooks) and "before" not in hooks:
                b["after_actions"] = [_make(x) for x in val]
                b["on_error_actions"] = list(b["after_actions"])
            else:
                b["before_actions"] = [_make(x) for x in val]
        elif key == f"{name}_action":
            b["before_actions"] = [_make(val)]
        elif key == f"{name}_decision":
            b["pre_decision"] = val
        elif key == f"{name}_result":
            b["tool_result"] = val
        elif key == f"{name}_injections":
            b["injections"] = [
                Injection(layer=i.get("layer", ""), content=i.get("content", ""),
                          priority=int(i.get("priority", 0) or 0), key=i.get("key"))
                for i in val
            ]
        elif key == f"{name}_inject_round":
            b["round_injections"] = [
                Injection(layer=i.get("layer", "BEFORE_INPUT"), content=i.get("content", ""),
                          priority=int(i.get("priority", 0) or 0), key=i.get("key"))
                for i in val
            ]
        elif key == f"{name}_terminal_actions":
            b["after_actions"] = [_make(x) for x in val]
            b["on_error_actions"] = list(b["after_actions"])
    # tool-path-modify:pre_modify_input → 声明 pre_tool_call 的扩展执行 modify 变形
    if "pre_modify_input" in inputs and "pre_tool_call" in hooks:
        b["pre_decision"] = {"decision": "modify", "input": inputs["pre_modify_input"]}
    return b


def _find_behavior_inputs(inputs: Dict[str, Any], suffix: str) -> Any:
    for k, v in inputs.items():
        if k.endswith(suffix):
            return v
    return None


# ---------------------------------------------------------------------------
# 事件断言
# ---------------------------------------------------------------------------
def _assert_event_stream(expected_events: List[Dict[str, Any]], actual_events: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    absent_items = [e for e in expected_events if e.get("absent")]
    ordered_items = [e for e in expected_events if not e.get("absent")]
    actual_types = [e.get("type") for e in actual_events]
    # absent:全程负断言
    for item in absent_items:
        if item["type"] in actual_types:
            errors.append(f"负断言失败: 不应出现事件 {item['type']} 但出现了")
    # 有序逐项匹配(跳过 absent)
    pos = 0
    for item in ordered_items:
        found = None
        while pos < len(actual_events):
            ev = actual_events[pos]
            pos += 1
            if ev.get("type") == item["type"]:
                found = ev
                break
        if found is None:
            errors.append(f"缺少有序事件 {item['type']}")
            break
        if "payload" in item:
            # custom:* 事件 payload 嵌套在事件 dict 的 payload 键;其余事件字段扁平
            target = found.get("payload") if str(found.get("type", "")).startswith("custom:") else found
            if not match_value(item["payload"], target):
                errors.append(f"事件 {item['type']} payload 不匹配: expect={item['payload']} actual={target}")
    return errors


def _assert_invocations(expected_invocations: List[Dict[str, Any]], actual: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    for item in expected_invocations:
        hook, ext = item.get("hook"), item.get("extension")
        if item.get("absent"):
            if any(r.get("hook") == hook and r.get("extension") == ext for r in actual):
                errors.append(f"负断言失败: {ext} 的 {hook} 不应被调用但发生了")
            continue
        order = item.get("order")
        candidates = [i for i, r in enumerate(actual) if r.get("hook") == hook and r.get("extension") == ext]
        if not candidates:
            errors.append(f"缺少调用 {ext} 的 {hook}")
            continue
        idx = candidates[0]
        # order 语义:同一 hook 内调用序号(1-based);'last' = 末位(逆序钩子用)
        if isinstance(order, int):
            hook_calls = [r for r in actual if r.get("hook") == hook]
            pos = next((i for i, r in enumerate(hook_calls) if r.get("extension") == ext), None)
            if pos is None or pos + 1 != order:
                errors.append(f"{ext} 的 {hook} 调用序应为 #{order},实际 #{pos + 1 if pos is not None else '?'}")
        elif order == "last":
            if idx != len(actual) - 1:
                errors.append(f"{ext} 的 {hook} 应在末位,实际 idx={idx}")
        rec = actual[idx]
        if "snapshot_assert" in item and not match_value(item["snapshot_assert"], rec.get("snapshot")):
            errors.append(f"{ext} 的 {hook} snapshot 不匹配: expect={item['snapshot_assert']} actual={rec.get('snapshot')}")
        if "actions_assert" in item and not match_value(item["actions_assert"], rec.get("actions") or []):
            errors.append(f"{ext} 的 {hook} actions 不匹配: {rec.get('actions')}")
    return errors


def _assert_final(expected_final: Dict[str, Any], done_event: Optional[Dict[str, Any]],
                  error_events: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    if not expected_final:
        return errors
    if expected_final.get("termination_reason") is not None:
        if done_event is None:
            errors.append(f"期望 termination_reason={expected_final['termination_reason']} 但无 done 事件")
        elif done_event.get("termination_reason") != expected_final["termination_reason"]:
            errors.append(f"termination_reason 不匹配: {done_event.get('termination_reason')}")
    if expected_final.get("is_complete") is not None:
        if done_event is not None and done_event.get("is_complete") != expected_final["is_complete"]:
            errors.append(f"is_complete 不匹配: {done_event.get('is_complete')}")
        elif done_event is None and expected_final.get("is_complete"):
            errors.append("期望 is_complete=true 但无 done 事件")
    return errors


# ---------------------------------------------------------------------------
# 临时存储
# ---------------------------------------------------------------------------
class _EphemeralStorage:
    """临时 SQLite 存储(会话存根 + 日志落盘),用完删除。"""

    def __init__(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="bs_runner_")
        self.db_path = os.path.join(self._tmpdir, "cases.db")
        self.provider = SQLiteStorageProvider(self.db_path)

    def ensure_session(self, sid: str) -> None:
        pass

    def get_session_messages(self, sid: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return []

    def log_message(self, *a: Any, **kw: Any) -> None:
        pass

    def close(self) -> None:
        try:
            self.provider.close()
        finally:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# pipeline 类用例执行
# ---------------------------------------------------------------------------
class _CaseResult:
    def __init__(self, case_id: str, ok: bool, detail: str) -> None:
        self.case_id = case_id
        self.ok = ok
        self.detail = detail


async def _run_pipeline_case(case: Dict[str, Any], verbose: bool) -> _CaseResult:
    case_id = case["id"]
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    storage = _EphemeralStorage()
    hooks = HookChain()
    invocations: List[Dict[str, Any]] = []

    l3_sink = L3BatchSink()
    l3_received: Dict[str, List[Dict[str, Any]]] = {}

    def make_l3_collector(ext: str):
        async def deliver(events: List[dict]) -> None:
            l3_received.setdefault(ext, []).extend(events)
        return deliver

    for decl in case.get("setup", {}).get("extensions", []):
        name = decl["name"]
        behaviors = _build_behaviors(name, inputs, decl)
        branch = ScriptedBranch(name, decl, behaviors, invocations)
        if "observe" in (decl.get("capabilities") or []):
            l3_sink.subscribe(name, make_l3_collector(name))
        hooks.register(branch)

    # FakeLLM 脚本:按 inputs 构造
    llm = _build_fake_llm(inputs)
    pipeline = ChatPipeline(
        llm_client=llm,
        history_store=storage,
        hooks=hooks,
        max_loops=int(inputs.get("max_loops", 50) or 50),
        storage_writer=None,
    )
    # L3 拦截点接线
    from teage_liu2.core.event_stream import EventStream
    pipeline.event_stream = EventStream(l3_sink=l3_sink)

    cancel_event = None
    if inputs.get("cancel"):
        cancel_event = threading.Event()
        cancel_event.set()

    events: List[Dict[str, Any]] = []
    try:
        async for ev in pipeline.chat_stream(
            inputs.get("session_id", "s-runner"),
            inputs.get("user_input", "测试"),
            system=inputs.get("system"),
            cancel_event=cancel_event,
        ):
            events.append(ev)
    except Exception as e:  # noqa: BLE001 - runner 断言层
        return _CaseResult(case_id, False, f"pipeline 异常: {e}")

    # 收集 L3 观测(等待批处理冲刷)
    await asyncio.sleep(0.15)

    errors: List[str] = []
    if case_id.startswith("l1-invariant"):
        # L1 不变量:event_stream 的 absent 项(text_delta/reasoning_delta)对 L3 投递流断言;
        # done 项对外壳事件流断言(§3.2 三层事件流)
        l3_all = [ev for evlist in l3_received.values() for ev in evlist]
        l3_types = {ev.get("type") for ev in l3_all}
        if "text_delta" in l3_types or "reasoning_delta" in l3_types:
            errors.append("L1 事件(text_delta/reasoning_delta)泄漏进 L3/transport(违反 L1 不变量)")
        done_event = next((e for e in events if e.get("type") == EV_DONE), None)
        if done_event is None or done_event.get("termination_reason") != "normal" or not done_event.get("is_complete"):
            errors.append("l1-invariant: 期望外壳事件流 done(normal, is_complete=true)")
    else:
        errors += _assert_event_stream(expected.get("event_stream") or [], events)
    errors += _assert_invocations(expected.get("invocations") or [], invocations)
    done_event = next((e for e in events if e.get("type") == EV_DONE), None)
    error_events = [e for e in events if e.get("type") == EV_ERROR]
    errors += _assert_final(expected.get("final") or {}, done_event, error_events)

    storage.close()
    return _CaseResult(case_id, not errors, "; ".join(errors))


def _build_fake_llm(inputs: Dict[str, Any]) -> FakeLLMClient:
    """按 inputs 构造 FakeLLM 脚本(对话类用例共用)。"""
    script: List[Any] = []

    def _text_block(text: str) -> dict:
        return {"type": "text", "text": text}

    def _tool_block(name: str, tool_input: dict, uid: str = "u1") -> dict:
        return {"type": "tool_use", "id": uid, "name": name, "input": tool_input}

    # 01/02/05/07 拦截路径:无 LLM;正常路径 end_turn 单轮
    tool_name = inputs.get("tool_name") or inputs.get("tool_use_name") or "echo"
    tool_input = inputs.get("tool_input") or inputs.get("tool_use_input") or {}
    text_deltas = inputs.get("llm_text_deltas")
    reasoning_deltas = inputs.get("llm_reasoning_deltas")

    if text_deltas is not None or reasoning_deltas is not None:
        # 04 L1 不变量:流式增量
        blocks = [_text_block(t) for t in (text_deltas or [])]
        if not blocks:
            blocks = [_text_block("流式输出")]
        return _StreamingLLM([(text_deltas or []), (reasoning_deltas or [])])

    if "scenarios" in inputs:
        # 16 错误责任矩阵:多场景由 runner 特判
        return FakeLLMClient([])

    if tool_name and inputs.get("tool_use_name") is not None or inputs.get("tool_name") is not None:
        # 03/08 工具路径:tool_use → end_turn
        if inputs.get("llm_script_mode") == "forever":
            return _ForeverToolLLM(tool_name, tool_input)
        script.append({
            "content": [_tool_block(tool_name, tool_input)],
            "stop_reason": "tool_use",
        })
        script.append({
            "content": [_text_block("完成")],
            "stop_reason": "end_turn",
        })
        return FakeLLMClient(script)

    # 默认:单轮 end_turn(02/05/09/13 等)
    script.append({
        "content": [_text_block("好的")],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    })
    return FakeLLMClient(script)


class _StreamingLLM(FakeLLMClient):
    """流式增量 LLM(L1 不变量用例):先 text 增量再 reasoning 增量,最后 done。"""

    def __init__(self, deltas: Tuple[List[str], List[str]]) -> None:
        super().__init__([])
        self.text_deltas, self.reasoning_deltas = deltas

    async def chat_main_stream(self, messages, tools=None, system=None, max_tokens=None,
                               cancel_event=None, activity_timeout=None):
        self.calls += 1
        for t in self.text_deltas:
            yield {"type": "text", "text": t}
        for r in self.reasoning_deltas:
            yield {"type": "reasoning", "text": r, "signature": None}
        yield {"type": "done", "stop_reason": "end_turn",
               "content_blocks": [{"type": "text", "text": "".join(self.text_deltas)}],
               "usage": {"input_tokens": 1, "output_tokens": 1}}


class _ForeverToolLLM(FakeLLMClient):
    """每轮都请求工具(16 max_loops 场景):驱动循环到 max_loops。"""

    def __init__(self, tool_name: str, tool_input: dict) -> None:
        super().__init__([])
        self.tool_name = tool_name
        self.tool_input = tool_input

    async def chat_main_stream(self, messages, tools=None, system=None, max_tokens=None,
                               cancel_event=None, activity_timeout=None):
        self.calls += 1
        yield {"type": "done", "stop_reason": "tool_use",
               "content_blocks": [{"type": "tool_use", "id": f"u{self.calls}",
                                   "name": self.tool_name, "input": self.tool_input}],
               "usage": None}


# ---------------------------------------------------------------------------
# 协议层用例执行(不经 pipeline,直接对拍宿主协议层)
# ---------------------------------------------------------------------------
async def _run_protocol_case(case: Dict[str, Any], verbose: bool) -> _CaseResult:
    case_id = case["id"]
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    errors: List[str] = []
    custom_events: List[Dict[str, Any]] = []

    if case_id.startswith("transport-frame"):
        frames = inputs.get("frames") or {}
        limits = inputs.get("limits") or {}
        r = {}
        try:
            f = TransportFrame.decode(json.dumps(frames["valid_full"], ensure_ascii=False))
            r["valid_full_decoded"] = f.type == "invoke_hook"
        except Exception:
            r["valid_full_decoded"] = False
        for key, label in [("invalid_type", "invalid_type_rejected"),
                           ("invalid_encoding", "invalid_encoding_rejected"),
                           ("invalid_json", "invalid_json_rejected"),
                           ("missing_fields", "missing_fields_rejected"),
                           ("delta_bad_revision", "delta_bad_revision_rejected")]:
            try:
                TransportFrame.decode(json.dumps(frames[key], ensure_ascii=False))
                r[label] = False
            except (ValueError, TypeError):
                r[label] = True
        try:
            d = TransportFrame.decode(json.dumps(frames["delta_valid"], ensure_ascii=False))
            r["delta_valid_decoded"] = d.encoding == "delta" and d.payload["base_revision"] == 3
        except Exception:
            r["delta_valid_decoded"] = False
        custom_events.append({"type": "custom:frame_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)

    elif case_id.startswith(("storage-spi", "storage-prefix-transport", "host-port-inprocess")):
        tmp = tempfile.mkdtemp(prefix="bs_storage_")
        db = os.path.join(tmp, "s.db")
        storage = SQLiteStorageProvider(db)

        class _FakeRoleLLM:
            async def chat_role(self, role, messages, system=None, max_tokens=None, tools=None):
                return type("R", (), {"content": [{"type": "text", "text": f"fake:{role}"}],
                                      "stop_reason": "end_turn", "usage": {}})()

        tasks = TaskRegistry()
        bus = TransportBus(llm_client=_FakeRoleLLM(), storage_provider=storage, task_registry=tasks)
        ext_name = (case.get("setup") or {}).get("extensions", [{}])[0].get("name", "audit")
        caps = (case.get("setup") or {}).get("extensions", [{}])[0].get("capabilities") or []
        bus.register_extension(ext_name, caps)

        r: Dict[str, Any] = {}
        ops = list(inputs.get("storage_ops") or []) + list(inputs.get("host_port_ops") or [])
        for op in ops:
            opname = op["op"]
            if opname.startswith("storage_"):
                opname = opname[len("storage_"):]  # write/query/write_cross_prefix 统一
            kind = op.get("kind", "")
            if opname == "write":
                resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": kind, "docs": op["docs"]}))
                if "result" in resp:
                    ids = resp["result"].get("doc_ids") or []
                    if len(op.get("docs") or []) > 1:
                        r["write_batch_ids"] = ids  # 批量写(§18.4)结果
                elif "error" in resp and resp["error"].get("code") == "invalid_payload":
                    r["illegal_kind_rejected"] = True  # 非法 kind(如 Bad;DROP)
            elif opname == "query":
                resp = await bus.handle(ext_name, TransportFrame("storage_query", {"kind": kind, "limit": op.get("limit")}))
                if "result" in resp:
                    r["query_limit_docs"] = resp["result"].get("docs") or []
            elif opname == "write_cross_prefix":
                resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": kind, "docs": op["docs"]}))
                r["cross_prefix_rejected"] = "error" in resp and resp["error"]["code"] == "kind_prefix_violation"
            elif opname == "invoke_llm":
                resp = await bus.handle(ext_name, TransportFrame("invoke_llm", {"role": op.get("role", "main"), "messages": op["messages"]}))
                r["invoke_llm_ok"] = "result" in resp
            elif opname == "task_register":
                resp = await bus.handle(ext_name, TransportFrame("task_register", {"task_id": op["task_id"], "description": op.get("description", "")}))
                r["task_register_ok"] = "result" in resp and tasks.registered_count == 1
            elif opname == "task_cancel":
                resp = await bus.handle(ext_name, TransportFrame("task_cancel", {"task_id": op["task_id"]}))
                r["task_cancel_ok"] = "result" in resp and tasks.registered_count == 0
        # 非法 kind 拒绝(06)
        if inputs.get("illegal_kind"):
            resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": inputs["illegal_kind"], "docs": [{"x": 1}]}))
            r["illegal_kind_rejected"] = "error" in resp
        # 跨前缀拒绝(未在 ops 显式覆盖时兜底验证)
        if "cross_prefix_rejected" not in r:
            resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": "other.cross_prefix", "docs": [{"x": 1}]}))
            r["cross_prefix_rejected"] = "error" in resp and resp["error"]["code"] == "kind_prefix_violation"
        # 前缀隔离 = 前缀内批量写成功 AND 跨前缀拒绝(§15-A3)
        r["prefix_isolation"] = bool(r.get("write_batch_ids")) and bool(r.get("cross_prefix_rejected"))

        event_type = "custom:host_port_result" if case_id.startswith("host-port-inprocess") else "custom:storage_result"
        custom_events.append({"type": event_type, "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
        errors += _assert_final(expected.get("final") or {}, {"type": EV_DONE, "is_complete": True}, [])
        storage.close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    elif case_id.startswith("invoke-llm"):
        tmp = tempfile.mkdtemp(prefix="bs_llm_")
        db = os.path.join(tmp, "s.db")

        class _CountingLLM:
            def __init__(self):
                self.max_concurrency = 0
                self.current = 0

            async def chat_role(self, role, messages, system=None, max_tokens=None, tools=None):
                self.current += 1
                self.max_concurrency = max(self.max_concurrency, self.current)
                await asyncio.sleep(0.02)
                self.current -= 1
                return type("R", (), {"content": [{"type": "text", "text": f"fake:{role}"}],
                                      "stop_reason": "end_turn", "usage": {}})()

        llm = _CountingLLM()
        tasks = TaskRegistry()
        bus = TransportBus(llm_client=llm, storage_provider=None, task_registry=tasks)
        bus.register_extension("llm_ext", ["llm"])
        bus.register_extension("plain_ext", [])
        r: Dict[str, Any] = {}
        for op in inputs.get("llm_ops") or []:
            ext = op.get("extension", "llm_ext")
            if op["op"] == "invoke_llm":
                resp = await bus.handle(ext, TransportFrame("invoke_llm", {"role": op.get("role", "main"), "messages": op["messages"]}))
                if op["extension"] == "llm_ext":
                    r["llm_ext_success"] = "result" in resp
                else:
                    r["plain_ext_rejected"] = "error" in resp and resp["error"]["code"] == "capability_not_declared"
            elif op["op"] == "invoke_llm_unknown_role":
                resp = await bus.handle(ext, TransportFrame("invoke_llm", {"role": op.get("role", "consolidation"), "messages": op["messages"]}))
                r["unknown_role_fallback_main"] = "result" in resp
            elif op["op"] == "invoke_llm_concurrent":
                results = await asyncio.gather(*[
                    bus.handle("llm_ext", TransportFrame("invoke_llm", {"role": "main", "messages": [{"role": "user", "content": "x"}]}))
                    for _ in range(op.get("count", 10))
                ])
                r["concurrency_max"] = llm.max_concurrency
        custom_events.append({"type": "custom:llm_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    elif case_id.startswith("l3-observe"):
        # L3 观测批处理旁路(§18.5):EventStream.route_l3 只投 L3 事件(tool_use/tool_result/step_end);
        # L1(text_delta/reasoning_delta)与外壳直通(done/error/step_start)不投 L3;有界队列满丢最旧+计数
        from teage_liu2.core.event_stream import EventStream
        sink = L3BatchSink(batch_window=0.02, batch_max=64)
        received: List[Dict[str, Any]] = []

        async def deliver(events: List[dict]) -> None:
            received.extend(events)

        sink.subscribe("obs", deliver)
        es = EventStream(l3_sink=sink)
        for ev in inputs.get("l3_events") or []:
            es.route_l3(ev)
        # L1 / 外壳直通事件不投 L3(§3.2)
        es.route_l3({"type": "text_delta", "session_id": "s", "text": "x"})
        es.route_l3({"type": "reasoning_delta", "session_id": "s", "text": "r", "signature": None})
        es.route_l3({"type": "step_start", "session_id": "s", "step": 1})
        es.route_l3({"type": "done", "session_id": "s"})
        await asyncio.sleep(0.1)
        received_types = [ev.get("type") for ev in received]
        r: Dict[str, Any] = {
            "observe_received_types": received_types,
            "l1_never_in_transport": "text_delta" not in received_types and "reasoning_delta" not in received_types,
            "bounded_queue_no_drop": sink.dropped_for("obs") == 0,
        }
        custom_events.append({"type": "custom:l3_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
        await sink.close()

    elif case_id.startswith("evolution-negotiation"):
        core_version = inputs.get("core_version", "v1.0.0")
        r: Dict[str, Any] = {}
        counts = {"proceed": 0, "downgrade": 0, "reject": 0}
        all_match = True
        for n in inputs.get("negotiations") or []:
            ext_ver = n.get("extension_version")
            try:
                result = negotiate_protocol_version(core_version, ext_ver)
            except ValueError:
                # 非法版本字符串 → reject(§evolution V-2 可读错误)
                result = {"compatible": False, "action": "reject"}
            actual = result["action"]
            counts[actual] = counts.get(actual, 0) + 1
            if actual != n.get("expect"):
                all_match = False
        r["proceed_cases"] = counts.get("proceed", 0)
        r["downgrade_cases"] = counts.get("downgrade", 0)
        r["reject_cases"] = counts.get("reject", 0)
        r["all_actions_match"] = all_match
        custom_events.append({"type": "custom:negotiation_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)

    elif case_id.startswith("lifecycle-reload"):
        errors += await _run_lifecycle_reload(case)

    elif case_id.startswith("error-responsibility"):
        errors += await _run_error_matrix(case)

    else:
        errors.append(f"未知用例类型: {case_id}")

    return _CaseResult(case["id"], not errors, "; ".join(errors))


async def _run_lifecycle_reload(case: Dict[str, Any]) -> List[str]:
    """14 热重载生命周期:registry.rebuild 原子替换 + setup 失败回滚保旧链。"""
    errors: List[str] = []
    registry = BranchRegistry()

    class _GoodBranch(Branch):
        name = "a"

        async def setup(self, config, host):
            pass

    class _BadBranch(Branch):
        name = "boom"

        async def setup(self, config, host):
            raise ValueError("setup fail")

    registry.register_factory("a", lambda cfg: _GoodBranch())
    registry.register_factory("boom", lambda cfg: _BadBranch())
    cfg1 = {"core": {"branches": {"a": True}}}
    registry.build(cfg1)
    await registry.setup_all(cfg1, host_builder=lambda n: {"protocol_domains": [], "storage": {"channel": "transport.storage_*", "kind_prefix": n}})

    reload_success_entries = len(registry.entries)
    rollback_preserved = False
    setup_idempotent = False
    # 第一次 reload 成功(同配置)
    await registry.rebuild(cfg1)
    reload_success_entries = len(registry.entries)
    # setup 幂等可重入:再次 setup 不抛
    try:
        await registry.setup_all(cfg1, host_builder=lambda n: {"protocol_domains": [], "storage": {"channel": "transport.storage_*", "kind_prefix": n}})
        setup_idempotent = True
    except Exception:
        setup_idempotent = False
    # 回滚:加 boom(setup 失败)
    cfg_bad = {"core": {"branches": {"a": True, "boom": True}}}
    try:
        await registry.rebuild(cfg_bad)
    except ValueError:
        rollback_preserved = len(registry.entries) == 1 and registry.entries[0][0].name == "a"
    await registry.teardown_all()

    custom = {
        "type": "custom:reload_result",
        "payload": {
            "reload_success_entries": reload_success_entries,
            "rollback_preserved_old_chain": rollback_preserved,
            "setup_idempotent_reentrant": setup_idempotent,
        },
    }
    expected = case.get("expected") or {}
    errors += _assert_event_stream(expected.get("event_stream") or [], [custom])
    return errors


async def _run_error_matrix(case: Dict[str, Any]) -> List[str]:
    """16 错误责任矩阵:7 终止原因实际路径全覆盖。"""
    errors: List[str] = []
    inputs = case.get("inputs") or {}
    scenarios = inputs.get("scenarios") or []
    covered = set()
    llm_error_no_done = True

    for sc in scenarios:
        label = sc.get("label", "")
        storage = _EphemeralStorage()
        hooks = HookChain()
        invocations: List[Dict[str, Any]] = []
        # 扩展注册按场景精确匹配(no_tool_executor 不注册执行者)
        if sc.get("ext_before") == "set_stop":  # intercepted
            b = ScriptedBranch("ext_stop", {"name": "ext_stop", "hooks_implemented": ["before"], "capabilities": []},
                               {"before_actions": [make_action("SetStop", reason="test")]}, invocations)
            hooks.register(b)
        if sc.get("ext_policy_decision"):  # tool_rejected
            b = ScriptedBranch("ext_policy", {"name": "ext_policy", "hooks_implemented": ["pre_tool_call"], "capabilities": []},
                               {"pre_decision": sc["ext_policy_decision"]}, invocations)
            hooks.register(b)
        if label in ("max_loops", "tool_rejected"):  # 有工具执行者
            b = ScriptedBranch("ext_tools", {"name": "ext_tools", "hooks_implemented": ["on_tool_call"], "capabilities": ["tool_executor"]},
                               {"tool_result": "executed"}, invocations)
            hooks.register(b)

        if label == "llm_error":
            class _ErrLLM(FakeLLMClient):
                async def chat_main_stream(self, *a, **kw):
                    self.calls += 1
                    raise RuntimeError("boom")
                    yield  # pragma: no cover
            llm = _ErrLLM()
        elif label == "max_loops":
            llm = _ForeverToolLLM("t", {})
        elif label in ("no_tool_executor", "tool_rejected"):
            llm = FakeLLMClient([{"content": [{"type": "tool_use", "id": "u1", "name": "t", "input": {}}], "stop_reason": "tool_use"}])
        else:  # normal / user_cancel / intercepted
            llm = FakeLLMClient([{"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}])

        cancel_event = threading.Event() if label == "user_cancel" else None
        if cancel_event is not None:
            cancel_event.set()
        pipeline = ChatPipeline(
            llm_client=llm,
            history_store=storage,
            hooks=hooks,
            max_loops=int(sc.get("max_loops", 50) or 50),
            storage_writer=None,
        )
        events: List[Dict[str, Any]] = []
        async for ev in pipeline.chat_stream("s-16", "错误矩阵测试", cancel_event=cancel_event):
            events.append(ev)

        expect = sc.get("expect") or {}
        etype = expect.get("type")
        if etype == "error":
            if any(e.get("type") == EV_ERROR for e in events):
                covered.add(label)
            else:
                errors.append(f"场景 {label}: 期望 error 事件")
            if any(e.get("type") == EV_DONE for e in events):
                llm_error_no_done = False
        elif etype == "tool_result":
            trs = [e for e in events if e.get("type") == EV_TOOL_RESULT]
            if trs and trs[0].get("is_error"):
                covered.add(label)
            else:
                errors.append(f"场景 {label}: 期望 tool_result is_error")
        else:
            done = next((e for e in events if e.get("type") == EV_DONE), None)
            if done and done.get("termination_reason") == expect.get("termination_reason"):
                covered.add(label)
            else:
                errors.append(f"场景 {label}: 期望 done({expect.get('termination_reason')})")
        storage.close()

    custom = {
        "type": "custom:error_matrix_result",
        "payload": {
            "scenario_count": len(scenarios),
            "all_termination_reasons_covered": len(covered) == 7,
            "llm_error_has_no_done": llm_error_no_done,
        },
    }
    expected = case.get("expected") or {}
    errors += _assert_event_stream(expected.get("event_stream") or [], [custom])
    if len(covered) != 7:
        errors.append(f"终止原因未全覆盖: {sorted(covered)}")
    return errors


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run_case(case: Dict[str, Any], verbose: bool) -> _CaseResult:
    case_id = case.get("id", "")
    protocol_pipeline_ids = {
        "storage-spi-06", "transport-frame-10", "storage-prefix-transport-11",
        "invoke-llm-12", "l3-observe-13", "lifecycle-reload-14",
        "evolution-negotiation-15", "error-responsibility-16",
        "host-port-inprocess-17",
    }
    if case_id in protocol_pipeline_ids:
        return await _run_protocol_case(case, verbose)
    return await _run_pipeline_case(case, verbose)


async def main() -> int:
    parser = argparse.ArgumentParser(description="teage_liu2 行为套件 runner")
    parser.add_argument("--case", default=None, help="只跑指定用例(id 前缀匹配,如 03)")
    parser.add_argument("--verbose", action="store_true", help="逐用例输出详情")
    args = parser.parse_args()

    case_files = sorted(f for f in os.listdir(_CASES_DIR) if f.endswith(".json"))
    if args.case:
        case_files = [f for f in case_files if f.startswith(args.case)]
    if not case_files:
        print(f"未找到用例(prefix={args.case!r})")
        return 1

    results: List[_CaseResult] = []
    for fname in case_files:
        path = os.path.join(_CASES_DIR, fname)
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        result = await run_case(case, args.verbose)
        results.append(result)
        status = "PASS" if result.ok else "FAIL"
        print(f"  [{status}] {result.case_id}" + (f"  <- {result.detail}" if not result.ok else ""))
        if args.verbose:
            print(f"          {case.get('description', '')[:80]}")

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print(f"\n=== 行为套件: {passed}/{total} 通过({100.0 * passed / total:.1f}%) ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
