"""主会话协作工具测试（list_collab_agents / request_collaboration）。

覆盖：
- 工具注册到 Core Tier + request_collaboration 标记 blocking=True
- request_collaboration 防自协作、字段断言（channel/main_ 前缀/from/to）
- wait_for_response=False 立即返回；无响应时干净超时
- list_collab_agents 排除 self
"""
from __future__ import annotations

import json

import pytest

from teage_liu.agent.tool_registry import ToolRegistry
from teage_liu.agent.tools.main_session_collab_tools import (
    register_main_session_collab_tools,
)


class FakeA2AClient:
    """最小 A2A 客户端替身（记录调用，返回空转发结果）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def call_all_endpoints(self, method: str, params: dict):
        self.calls.append(("*", method, params))
        return {"worker-2": {"ok": True, "seq": 1}}


def _make_registry(bb_root, local_agent_id="teagent-lu", msc_cfg=None):
    reg = ToolRegistry()
    register_main_session_collab_tools(
        reg,
        bb_root,
        local_agent_id,
        FakeA2AClient(),
        msc_cfg or {
            "enabled": True,
            "default_timeout": 3,
            "max_timeout": 10,
            "max_result_chars": 1000,
        },
    )
    return reg


def _list_main_collab_files(bb_root):
    collabs = bb_root / "collabs"
    if not collabs.exists():
        return []
    return sorted(p.name for p in collabs.glob("main_*.md"))


def test_registers_both_tools(bb_root):
    reg = _make_registry(bb_root)
    schema = reg.get_tools_schema()
    names = {t.get("name") for t in schema}
    assert "list_collab_agents" in names
    assert "request_collaboration" in names
    # request_collaboration 必须标记为阻塞型（to_thread 执行）
    assert reg.is_blocking_tool("request_collaboration") is True
    assert reg.is_blocking_tool("list_collab_agents") is False


def test_request_collaboration_rejects_self(bb_root):
    reg = _make_registry(bb_root)
    result = reg.execute_tool("request_collaboration", {
        "target_agent_id": "teagent-lu",  # 自身
        "task": "帮我做点什么",
        "wait_for_response": False,
    })
    data = json.loads(result)
    assert data["ok"] is False
    assert "自身" in data["error"] or "不能与自身协作" in data["error"]
    # 不写任何 main_ 文件
    assert _list_main_collab_files(bb_root) == []


def test_request_collaboration_writes_request_with_channel(bb_root):
    reg = _make_registry(bb_root)
    result = reg.execute_tool("request_collaboration", {
        "target_agent_id": "teagent-liu-2",
        "task": "总结一下项目结构",
        "wait_for_response": False,
    })
    data = json.loads(result)
    assert data["ok"] is True
    assert data["from"] == "teagent-lu"
    assert data["target_agent_id"] == "teagent-liu-2"
    assert str(data["collab_id"]).startswith("main_")

    # 本地镜像文件存在，且首条消息为 request + channel=main_session
    files = _list_main_collab_files(bb_root)
    assert len(files) == 1
    from teage_liu.multiagent.blackboard import read_collab_messages

    msgs = _run_sync(read_collab_messages(bb_root, collab_id=data["collab_id"]))
    assert len(msgs) >= 1
    req = msgs[0]
    assert req["channel"] == "main_session"
    assert req["type"] == "request"
    assert req["from"] == "teagent-lu"
    assert req["to"] == "teagent-liu-2"
    assert req["priority"] == "high"
    assert req.get("wait_for_response") is False
    # end 归档消息也会写入（wait_for_response=False 时仍写 end）
    assert any(m["type"] == "end" for m in msgs)


def test_request_collaboration_times_out_cleanly(bb_root):
    reg = _make_registry(bb_root)
    # 对端无响应，timeout=1s → 干净超时文本（非异常）
    result = reg.execute_tool("request_collaboration", {
        "target_agent_id": "teagent-liu-2",
        "task": "需要很久的任务",
        "timeout": 1,
        "wait_for_response": True,
    })
    data = json.loads(result)
    assert data["ok"] is False
    assert data["timed_out"] is True
    assert "summary" not in data or data["summary"] is None


def test_list_collab_agents_excludes_self(bb_root):
    # 注册 self 卡片（teagent-lu）+ 一个对端卡片（teagent-liu-2）
    agents_dir = bb_root / "agents"
    (agents_dir / "teagent-lu.md").write_text(
        "---\nagent_id: teagent-lu\nstatus: active\n---\n", encoding="utf-8"
    )
    (agents_dir / "teagent-liu-2.md").write_text(
        "---\nagent_id: teagent-liu-2\nstatus: active\n---\n", encoding="utf-8"
    )
    reg = _make_registry(bb_root)
    result = reg.execute_tool("list_collab_agents", {})
    data = json.loads(result)
    assert data["self"] == "teagent-lu"
    local_ids = [a.get("agent_id") for a in data["local"]]
    assert "teagent-lu" not in local_ids  # 排除 self
    assert "teagent-liu-2" in local_ids
    assert "worker-2" in data["remote"]  # 远程端点视图


def _run_sync(coro):
    import asyncio

    return asyncio.run(coro)
