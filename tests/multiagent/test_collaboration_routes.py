"""协作 API 测试（Task 3，含 A2/A3/A4 修复）"""
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from teage_liu.multiagent.message_signature import sign_message


def _register_agent_key(tmp_path: Path, agent_id: str = "agent_A"):
    """在 tmp_path/blackboard 下注册 agent 公钥，返回私钥。"""
    private_key = Ed25519PrivateKey.generate()
    keys_dir = tmp_path / "blackboard" / "agents" / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (keys_dir / f"{agent_id}.pem").write_bytes(pem)
    return private_key


@pytest.fixture
def bb_root(tmp_path):
    """临时黑板根目录"""
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


@pytest.fixture
def client(tmp_path, monkeypatch):
    """创建测试客户端，独立 app 只挂载 collab_router（避免依赖完整 lifespan）"""
    bb_root = tmp_path / "blackboard"
    bb_root.mkdir()
    (bb_root / "collabs").mkdir()
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))

    from fastapi import FastAPI
    from teage_liu.multiagent.collaboration_routes import create_collab_router

    app = FastAPI()
    app.include_router(create_collab_router())
    return TestClient(app)


def test_get_collab_messages_empty(client):
    """获取空协作消息列表"""
    resp = client.get("/api/multiagent/collab/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []
    assert data["total"] == 0


def test_append_collab_message(client):
    """写入协作消息（返回 seq + ok）"""
    resp = client.post("/api/multiagent/collab/append", json={
        "message": {
            "from": "agent_A",
            "type": "announce",
            "action": "online",
            "content": "我上线了",
        }
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["seq"] == 1
    assert data["ok"] is True


def test_append_collab_message_with_collab_id(client):
    """写入带 collab_id 的协作消息"""
    resp = client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_001",
        "message": {
            "from": "agent_A",
            "type": "request",
            "content": "需要协作",
            "collab_id": "collab_001",
        }
    })
    assert resp.status_code == 200
    assert resp.json()["seq"] == 1


def test_get_collab_messages_with_collab_id(client):
    """获取指定协作的消息"""
    client.post("/api/multiagent/collab/append", json={
        "message": {"from": "agent_A", "type": "announce", "action": "online", "content": "global"}
    })
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_001",
        "message": {"from": "agent_B", "type": "request", "content": "collab1"}
    })

    # 查询全局聚合视图：含全局消息 + active 协作消息（collab_001 为 initiated，
    # 由 append 自动建索引，纳入聚合），故 total=2
    resp = client.get("/api/multiagent/collab/messages")
    assert resp.json()["total"] == 2

    # 查询 collab_001
    resp = client.get("/api/multiagent/collab/messages?collab_id=collab_001")
    assert resp.json()["total"] == 1
    assert resp.json()["messages"][0]["content"] == "collab1"


def test_forward_a2a_message(client, tmp_path):
    """转发 A2A 消息（A3 修复：用 from alias；Task 2：强制签名校验）"""
    private_key = _register_agent_key(tmp_path, "agent_A")
    message = {
        "from": "agent_A",
        "to": "agent_B",
        "content": "A2A 消息",
        "via": "a2a",
        "message_id": "msg_001",
    }
    signature = sign_message(message, private_key)
    resp = client.post("/api/multiagent/collab/forward", json={
        **message,
        "from_": message["from"],
        "signature": signature,
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_forward_dedup(client, tmp_path):
    """相同 message_id 转发去重（A2 修复：返回 deduplicated 字段；Task 2：强制签名校验）"""
    private_key = _register_agent_key(tmp_path, "agent_A")
    message = {
        "from": "agent_A",
        "to": "agent_B",
        "content": "A2A 消息",
        "via": "a2a",
        "message_id": "msg_002",
    }
    signature = sign_message(message, private_key)
    client.post("/api/multiagent/collab/forward", json={
        **message,
        "from_": message["from"],
        "signature": signature,
    })
    resp = client.post("/api/multiagent/collab/forward", json={
        **message,
        "from_": message["from"],
        "signature": signature,
    })
    assert resp.status_code == 200
    assert resp.json()["deduplicated"] is True


def test_broadcast_from_director(client):
    """Director 发布广播发起新协作（start_collab=True，from=director, type=request）"""
    resp = client.post("/api/multiagent/collab/broadcast", json={
        "content": "帮我读直播间弹幕并讨论",
        "start_collab": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["started"] is True
    # 生命周期：start_collab 生成 collab_id，路由到 collabs/{collab_id}.md
    collab_id = data["collab_id"]

    messages = client.get(
        f"/api/multiagent/collab/messages?collab_id={collab_id}"
    ).json()["messages"]
    assert messages[0]["from"] == "director"
    assert messages[0]["type"] == "request"
    assert messages[0]["collab_id"] == collab_id


def test_announce_online(client):
    """agent 上线 announce"""
    resp = client.post("/api/multiagent/collab/announce", json={
        "agent_id": "agent_A",
        "action": "online",
        "capabilities": ["sentiment_analysis"],
        "endpoint": "http://localhost:8001",
        "agent_name": "情感分析助手",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_get_agents_list(client):
    """获取 agent 列表（A4 修复：合并 registry + announce）"""
    client.post("/api/multiagent/collab/announce", json={
        "agent_id": "agent_A",
        "action": "online",
        "capabilities": ["sentiment_analysis"],
        "agent_name": "情感分析助手",
    })
    client.post("/api/multiagent/collab/announce", json={
        "agent_id": "agent_B",
        "action": "online",
        "capabilities": ["text_summary"],
        "agent_name": "总结助手",
    })

    resp = client.get("/api/multiagent/collab/agents")
    assert resp.status_code == 200
    data = resp.json()
    agent_ids = [a["agent_id"] for a in data["agents"]]
    assert "agent_A" in agent_ids
    assert "agent_B" in agent_ids


def test_directive_endpoint(client):
    """Director 注入 directive 端点（Task 5 Step 6.5 用）"""
    resp = client.post("/api/multiagent/collab/directive", json={
        "content": "按顺序执行",
        "rule_type": "ordering",
        "target": "*",
        "priority": "high",
        "issued_by": "UserDirector",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    messages = client.get("/api/multiagent/collab/messages").json()["messages"]
    assert messages[0]["type"] == "directive"
    assert messages[0]["priority"] == "high"
    assert messages[0]["issued_by"] == "UserDirector"


# ========== Task 12: /collabs 端点 ==========


def test_list_collabs_empty(client):
    """空协作列表"""
    resp = client.get("/api/multiagent/collab/collabs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["collabs"] == []
    assert data["total"] == 0


def test_upsert_collab(client):
    """手动创建协作索引"""
    resp = client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_001",
        "title": "弹幕分析",
        "status": "active",
        "participants": ["agent_A", "agent_B"],
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 验证可读取
    resp = client.get("/api/multiagent/collab/collabs")
    data = resp.json()
    assert data["total"] == 1
    assert data["collabs"][0]["collab_id"] == "collab_001"
    assert data["collabs"][0]["title"] == "弹幕分析"


def test_upsert_collab_updates_existing(client):
    """更新已存在的协作索引（upsert）"""
    client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_001",
        "title": "t1", "status": "active", "participants": [],
    })
    client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_001",
        "title": "t2", "status": "completed", "participants": [],
    })
    resp = client.get("/api/multiagent/collab/collabs")
    data = resp.json()
    assert data["total"] == 1  # 不重复
    assert data["collabs"][0]["status"] == "completed"


def test_collab_index_auto_created_on_append(client):
    """写入带 collab_id 的 request 时自动创建索引"""
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_auto",
        "message": {
            "from": "agent_A",
            "type": "request",
            "content": "需要协作",
        },
    })
    resp = client.get("/api/multiagent/collab/collabs")
    data = resp.json()
    assert any(c["collab_id"] == "collab_auto" for c in data["collabs"])


# ---------------- Task 5: broadcast 去重 ----------------


def test_broadcast_dedup_on_duplicate_submission(client):
    """协作中 Director 重复广播相同内容只写一条（同 collab 内 message_id 去重）。"""
    # 1. 发起一个新协作
    resp_start = client.post("/api/multiagent/collab/broadcast", json={
        "content": "开始讨论", "start_collab": True,
    })
    assert resp_start.status_code == 200
    collab_id = resp_start.json()["collab_id"]

    # 2. 协作中 Director 追加信息（首次）
    resp1 = client.post("/api/multiagent/collab/broadcast", json={
        "content": "ciallo~", "collab_id": collab_id,
    })
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["ok"] is True
    assert data1["deduplicated"] is False, "首次追加不应去重"
    assert data1["started"] is False

    # 3. 协作中 Director 重复追加相同内容（同 collab → 同文件 → message_id 去重）
    resp2 = client.post("/api/multiagent/collab/broadcast", json={
        "content": "ciallo~", "collab_id": collab_id,
    })
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["ok"] is True
    assert data2["seq"] == data1["seq"], "去重时返回原 seq"
    assert data2["deduplicated"] is True, "同 collab 内重复内容应被去重"

    # 验证该 collab 下 "ciallo~" 只有一条
    resp = client.get(f"/api/multiagent/collab/messages?collab_id={collab_id}")
    messages = resp.json()["messages"]
    ciallo_msgs = [m for m in messages if m.get("content") == "ciallo~"]
    assert len(ciallo_msgs) == 1, f"应只有 1 条 ciallo 消息，实际: {len(ciallo_msgs)}"


def test_broadcast_different_content_not_dedup(client):
    """不同内容发起新协作不去重（各自独立 collab_id，seq=1）。"""
    resp1 = client.post("/api/multiagent/collab/broadcast", json={
        "content": "msg A", "start_collab": True,
    })
    resp2 = client.post("/api/multiagent/collab/broadcast", json={
        "content": "msg B", "start_collab": True,
    })
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["deduplicated"] is False, "不同内容不应去重"
    # 不同 collab_id → 不同文件 → 各自独立 seq 空间，均为 seq=1
    assert data2["seq"] == 1, "不同 collab_id 各自独立 seq，应为 1"
    assert resp1.json()["collab_id"] != data2["collab_id"], "不同内容应生成不同 collab_id"


def test_broadcast_rejects_free_floating(client):
    """游离广播（既不 start_collab 也不带 collab_id）应被拒绝。"""
    resp = client.post("/api/multiagent/collab/broadcast", json={
        "content": "无归属广播",
    })
    assert resp.status_code == 400
    assert "collab" in resp.json()["detail"].lower()


# ========== Task 4: /events 端点 ==========

def test_events_endpoint_returns_activity_events(client):
    """/events 端点应从协作消息提取活动事件。"""
    # 写入混合消息（/events 读全局 collaboration.md）
    client.post("/api/multiagent/collab/append", json={
        "message": {
            "type": "announce", "from": "agent-a", "action": "online",
            "agent_name": "AgentA", "capabilities": ["task_exec"],
            "timestamp": "2026-07-27T10:00:00+00:00",
        },
    })
    # 阶段 3.1：broadcast 现路由到 collabs/{collab_id}.md（隔离），/events 读全局，
    # 故用 /append 写一条 director request 到全局以验证事件提取
    client.post("/api/multiagent/collab/append", json={
        "message": {
            "type": "request", "from": "director", "content": "hello all",
            "to": "*", "timestamp": "2026-07-27T11:00:00+00:00",
        },
    })
    client.post("/api/multiagent/collab/append", json={
        "message": {
            "type": "relay", "from": "agent-a", "content": "noise",
            "timestamp": "2026-07-27T11:30:00+00:00",
        },
    })  # 应被过滤

    resp = client.get("/api/multiagent/collab/events")
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data
    # 仅 announce + broadcast，relay 被过滤
    assert len(data["events"]) == 2
    types = [e["event_type"] for e in data["events"]]
    assert "agent_online" in types
    assert "director_broadcast" in types


def test_events_endpoint_before_ts_pagination(client):
    """/events 端点支持 before_ts 游标分页。"""
    for i in range(1, 4):
        client.post("/api/multiagent/collab/append", json={
            "message": {
                "type": "announce", "from": f"agent-{i}", "action": "online",
                "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
            },
        })

    # 取 11:00 之前的事件（10:00 那条，seq=1）
    resp = client.get("/api/multiagent/collab/events?before_ts=2026-07-27T11:00:00%2B00:00")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["agent_id"] == "agent-1"


def test_events_endpoint_limit(client):
    """/events 端点支持 limit 参数。"""
    for i in range(1, 6):
        client.post("/api/multiagent/collab/append", json={
            "message": {
                "type": "announce", "from": f"agent-{i}", "action": "online",
                "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
            },
        })

    resp = client.get("/api/multiagent/collab/events?limit=3")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 3
    # 最新（14:00）应在最前
    assert data["events"][0]["agent_id"] == "agent-5"


# ========== Task 5: /messages 支持 before_seq ==========

def test_messages_endpoint_before_seq(client):
    """/messages 端点支持 before_seq 游标分页。"""
    for i in range(1, 5):
        client.post("/api/multiagent/collab/append", json={
            "message": {
                "type": "status", "from": "agent-x", "content": f"m{i}",
                "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
            },
        })

    resp = client.get("/api/multiagent/collab/messages?before_seq=3&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    seqs = [m["seq"] for m in data["messages"]]
    assert seqs == [1, 2]  # 严格小于 3


def test_messages_endpoint_limit(client):
    """/messages 端点 limit 参数返回最新 N 条。"""
    for i in range(1, 6):
        client.post("/api/multiagent/collab/append", json={
            "message": {
                "type": "status", "from": "agent-y", "content": f"m{i}",
                "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
            },
        })

    resp = client.get("/api/multiagent/collab/messages?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    seqs = [m["seq"] for m in data["messages"]]
    assert seqs == [4, 5]


# ========== include_archived：工作台全局视图合并 active+archived ==========

def test_messages_default_excludes_archived(client):
    """默认 /messages（无 collab_id）不返回 archived 协作的消息。"""
    # 1. active 协作 + 一条消息
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_active",
        "message": {"from": "agent_A", "type": "request", "content": "active msg",
                    "timestamp": "2026-07-27T10:00:00+00:00"},
    })
    # 2. archived 协作 + 一条消息，再归档
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_archived",
        "message": {"from": "agent_A", "type": "request", "content": "archived msg",
                    "timestamp": "2026-07-27T11:00:00+00:00"},
    })
    client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_archived", "title": "已归档",
        "status": "archived", "participants": ["agent_A"],
    })

    # 默认（include_archived 缺省 True）：应返回两条
    resp = client.get("/api/multiagent/collab/messages")
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.json()["messages"]]
    assert "active msg" in contents
    assert "archived msg" in contents


def test_messages_include_archived_true_returns_all(client):
    """include_archived=true 显式返回 active+archived 全部消息。"""
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_active",
        "message": {"from": "agent_A", "type": "request", "content": "active",
                    "timestamp": "2026-07-27T10:00:00+00:00"},
    })
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_archived",
        "message": {"from": "agent_A", "type": "request", "content": "archived",
                    "timestamp": "2026-07-27T11:00:00+00:00"},
    })
    client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_archived", "title": "已归档",
        "status": "archived", "participants": [],
    })

    resp = client.get("/api/multiagent/collab/messages?include_archived=true")
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.json()["messages"]]
    assert "active" in contents
    assert "archived" in contents


def test_messages_include_archived_false_excludes_archived(client):
    """include_archived=false 仅返回 active 协作消息，归档协作被过滤。"""
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_active",
        "message": {"from": "agent_A", "type": "request", "content": "active",
                    "timestamp": "2026-07-27T10:00:00+00:00"},
    })
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_archived",
        "message": {"from": "agent_A", "type": "request", "content": "archived",
                    "timestamp": "2026-07-27T11:00:00+00:00"},
    })
    client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_archived", "title": "已归档",
        "status": "archived", "participants": [],
    })

    resp = client.get("/api/multiagent/collab/messages?include_archived=false")
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.json()["messages"]]
    assert "active" in contents
    assert "archived" not in contents


def test_messages_include_archived_with_specific_collab_id(client):
    """指定 collab_id 时 include_archived 参数无效（单协作查询本就含归档）。"""
    client.post("/api/multiagent/collab/append", json={
        "collab_id": "collab_archived",
        "message": {"from": "agent_A", "type": "request", "content": "x"},
    })
    client.post("/api/multiagent/collab/collabs", json={
        "collab_id": "collab_archived", "title": "x",
        "status": "archived", "participants": [],
    })

    # 指定 collab_id 时，无论 include_archived 取何值都应返回该协作消息
    resp = client.get(
        "/api/multiagent/collab/messages?collab_id=collab_archived&include_archived=false"
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
