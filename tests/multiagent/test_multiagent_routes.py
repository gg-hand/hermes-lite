"""multiagent_routes 端点测试。"""
import pytest
from pathlib import Path


def _make_container(bb_root, enabled=True, role="worker"):
    """构造测试用 DI 容器 mock。"""
    class _Cfg:
        def __init__(self, d): self._d = d
        def get(self, k, default=None): return self._d.get(k, default)
    class _Container:
        def __init__(self):
            self.config = _Cfg({
                "multiagent": {
                    "enabled": enabled,
                    "role": role,
                    "blackboard_dir": str(bb_root),
                }
            })
        def get(self, k): return None
    return _Container()


@pytest.mark.asyncio
async def test_status_returns_epoch_history(bb_root, monkeypatch):
    """/status 响应应包含 epoch_history 字段，从 collab directive 消息提取。"""
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))
    from teage_liu.multiagent.blackboard import append_collab_message

    # 写入 3 条 directive 消息模拟 epoch 推进
    for i in range(1, 4):
        await append_collab_message(bb_root, {
            "type": "directive", "from": "director",
            "content": f"directive-{i}",
            "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
        })

    container = _make_container(bb_root)
    from teage_liu.api.multiagent_routes import create_multiagent_router
    router = create_multiagent_router(container)

    # 找到 get_status 路由并直接调用
    status_route = next(r for r in router.routes if getattr(r, "path", "") == "/api/multiagent/status")
    response = await status_route.endpoint()

    assert "epoch_history" in response
    history = response["epoch_history"]
    assert isinstance(history, list)
    assert len(history) <= 5  # 最多 5 个节点
    # 每条应有 epoch / ts / seq 字段
    if history:
        assert "epoch" in history[0]
        assert "ts" in history[0]
        assert "seq" in history[0]


@pytest.mark.asyncio
async def test_status_epoch_history_ordered_desc(bb_root, monkeypatch):
    """epoch_history 应按时间倒序（最新在前）。"""
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))
    from teage_liu.multiagent.blackboard import append_collab_message

    # 写入 3 条 directive 消息
    await append_collab_message(bb_root, {
        "type": "directive", "from": "director", "content": "d1",
        "timestamp": "2026-07-27T10:00:00+00:00",
    })
    await append_collab_message(bb_root, {
        "type": "directive", "from": "director", "content": "d2",
        "timestamp": "2026-07-27T12:00:00+00:00",
    })
    await append_collab_message(bb_root, {
        "type": "directive", "from": "director", "content": "d3",
        "timestamp": "2026-07-27T11:00:00+00:00",
    })

    container = _make_container(bb_root)
    from teage_liu.api.multiagent_routes import create_multiagent_router
    router = create_multiagent_router(container)
    status_route = next(r for r in router.routes if getattr(r, "path", "") == "/api/multiagent/status")
    response = await status_route.endpoint()

    history = response.get("epoch_history", [])
    assert len(history) == 3
    # 最新（12:00）应在前
    assert history[0]["ts"] == "2026-07-27T12:00:00+00:00"
    assert history[1]["ts"] == "2026-07-27T11:00:00+00:00"
    assert history[2]["ts"] == "2026-07-27T10:00:00+00:00"


@pytest.mark.asyncio
async def test_status_epoch_history_caps_at_5(bb_root, monkeypatch):
    """epoch_history 最多返回 5 条。"""
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))
    from teage_liu.multiagent.blackboard import append_collab_message

    # 写入 7 条 directive 消息
    for i in range(1, 8):
        await append_collab_message(bb_root, {
            "type": "directive", "from": "director", "content": f"d{i}",
            "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
        })

    container = _make_container(bb_root)
    from teage_liu.api.multiagent_routes import create_multiagent_router
    router = create_multiagent_router(container)
    status_route = next(r for r in router.routes if getattr(r, "path", "") == "/api/multiagent/status")
    response = await status_route.endpoint()

    history = response.get("epoch_history", [])
    assert len(history) == 5


@pytest.mark.asyncio
async def test_status_epoch_history_empty_when_no_directives(bb_root, monkeypatch):
    """无 directive 消息且 director_epoch=0 时返回空列表。"""
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))

    container = _make_container(bb_root)
    from teage_liu.api.multiagent_routes import create_multiagent_router
    router = create_multiagent_router(container)
    status_route = next(r for r in router.routes if getattr(r, "path", "") == "/api/multiagent/status")
    response = await status_route.endpoint()

    history = response.get("epoch_history", [])
    assert history == []
