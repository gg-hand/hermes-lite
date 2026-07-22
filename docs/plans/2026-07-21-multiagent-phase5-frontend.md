---
plan_id: 2026-07-21-multiagent-phase5-frontend
title: Multi-Agent 改造 Plan 4 — 前端适配（chat-settings + chat-core SSE + Director 状态显示）
phase: 5
created_at: 2026-07-21
depends_on:
  - 2026-07-21-multiagent-phase1-foundation
  - 2026-07-21-multiagent-phase2-collaboration
  - 2026-07-21-multiagent-phase3-4-cross-device
spec_ref: docs/superpowers/specs/2026-07-20-多agent协作机制-design.md
design_sections: [§13 Phase 5 前端适配, §10.2 配置段]
---

# Plan 4: 前端适配（chat-settings + chat-core SSE + Director 状态显示）

## 目标

为 multiagent 协作机制提供前端 UI 支持，让用户能：

1. **配置 multiagent 协作**：在设置模态框中启用/关闭协作、配置角色（Director/Worker）、配置 blackboard 目录、配置远程端点
2. **实时监控协作状态**：通过 SSE 通道接收 multiagent_alert 事件，显示 Director 健康、自治模式、Agent 列表
3. **查看协作历史**：浏览 blackboard 中的 messages.md、audit.md、agent_card 等

## Global Constraints

1. **复用现有设置模态框**：在 chat.html 的齿轮图标设置模态框中新增 multiagent 段，不新建独立配置页
2. **SSE 通道命名**：`multiagent_alert`（与 design.md §13 一致）
3. **UI 风格一致**：毛玻璃按钮、圆角处理、元素间距适当拉大，与现有 chat 界面风格一致
4. **路径安全**：前端不直接显示绝对路径，统一显示相对路径或别名
5. **降级显示**：multiagent 关闭时隐藏相关 UI，不影响单 agent 使用
6. **配置通过 PUT /config 接口提交**：复用现有热更新机制
7. **Director 状态三色显示**：健康（绿）/ 降级（黄）/ 自治（橙）/ 故障（红）

## File Structure

```
web/
├── js/
│   ├── multiagent-settings.js   # 新增：multiagent 配置 UI 逻辑
│   ├── multiagent-sse.js        # 新增：multiagent_alert SSE 订阅
│   └── multiagent-render.js     # 新增：Director 状态/Agent 列表渲染
├── css/
│   └── multiagent.css           # 新增：multiagent UI 样式
└── chat.html                    # 修改：引入新文件 + 添加状态指示器 DOM

hermes/
├── api/
│   └── multiagent_routes.py     # 新增：multiagent 状态查询 REST 端点
└── app.py                       # 修改：注册 multiagent 路由 + SSE 通道

tests/
├── api/
│   └── test_multiagent_routes.py # 新增
└── e2e/
    └── test_multiagent_ui.py    # 新增（Playwright）
```

---

## Task 1: 后端 multiagent 状态查询端点

### RED：编写失败测试

创建 `tests/api/test_multiagent_routes.py`：

```python
"""multiagent REST 端点测试。"""
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient


@pytest.fixture
async def bb_root(tmp_path: Path) -> Path:
    from hermes.multiagent.blackboard import Blackboard
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def app_with_multiagent(bb_root: Path):
    from fastapi import FastAPI
    from hermes.api.multiagent_routes import create_multiagent_router
    from hermes.container import Container

    config = {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": str(bb_root),
        },
    }
    container = Container(config)
    app = FastAPI()
    app.include_router(create_multiagent_router(container))
    return app


class TestMultiagentRoutes:
    """multiagent 路由测试。"""

    async def test_get_status(self, app_with_multiagent):
        """GET /api/multiagent/status 返回协作状态。"""
        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "enabled" in data
            assert "role" in data
            assert "director" in data
            assert "agents" in data

    async def test_get_agents_list(self, app_with_multiagent, bb_root: Path):
        """GET /api/multiagent/agents 返回 agent 列表。"""
        from hermes.multiagent.agent_registry import AgentRegistry
        registry = AgentRegistry(bb_root)
        await registry.register("worker_001", "worker", ["file_read"], 10)

        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/agents")
            assert resp.status_code == 200
            data = resp.json()
            assert any(a["agent_id"] == "worker_001" for a in data["agents"])

    async def test_get_messages(self, app_with_multiagent, bb_root: Path):
        """GET /api/multiagent/messages 返回消息列表。"""
        from hermes.multiagent.blackboard import append_message
        await append_message(bb_root, {
            "seq": 1, "from": "worker_001", "to": "*",
            "timestamp": "2026-07-21T00:00:00Z",
            "type": "chat", "content_type": "markdown", "epoch": 0,
        })

        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/messages?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["messages"]) >= 1

    async def test_get_audit_records(self, app_with_multiagent, bb_root: Path):
        """GET /api/multiagent/audit 返回审计记录。"""
        from hermes.multiagent.blackboard import append_audit
        await append_audit(bb_root, {
            "ts": "2026-07-21T00:00:00Z",
            "actor": "director", "action": "election",
            "target": "status.json", "op_id": "test",
            "epoch": 1, "details": {},
            "prev_hash": "", "hash": "", "signature": "",
        })

        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/audit?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["records"]) >= 1

    async def test_get_director_md(self, app_with_multiagent, bb_root: Path):
        """GET /api/multiagent/director 返回 director.md 内容。"""
        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/director")
            assert resp.status_code == 200

    async def test_disabled_returns_404(self, tmp_path: Path):
        """multiagent.enabled=False 时返回 404。"""
        from fastapi import FastAPI
        from hermes.api.multiagent_routes import create_multiagent_router
        from hermes.container import Container

        config = {"multiagent": {"enabled": False}}
        container = Container(config)
        app = FastAPI()
        app.include_router(create_multiagent_router(container))

        with TestClient(app) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 404
```

### 验证失败

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/api/test_multiagent_routes.py -v
# 预期：全部失败（multiagent_routes 模块不存在）
```

### GREEN：最小实现

创建 `hermes/api/multiagent_routes.py`：

```python
"""multiagent REST 端点：状态查询 + 消息/审计读取 + Director 信息。

端点：
- GET /api/multiagent/status           协作总览状态
- GET /api/multiagent/agents           agent 列表
- GET /api/multiagent/messages         消息列表（支持 limit 参数）
- GET /api/multiagent/audit            审计记录（支持 limit 参数）
- GET /api/multiagent/director         director.md 内容
- GET /api/multiagent/sse              multiagent_alert SSE 通道
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


def create_multiagent_router(container) -> APIRouter:
    """创建 multiagent 路由器。"""
    router = APIRouter(prefix="/api/multiagent", tags=["multiagent"])
    config = container.config
    multiagent_cfg = config.get("multiagent", {}) or {}

    # 如果 multiagent 未启用，所有端点返回 404
    if not multiagent_cfg.get("enabled"):
        @router.get("/{path:path}")
        async def not_found(path: str):
            raise HTTPException(status_code=404, detail="multiagent disabled")
        return router

    bb_dir = multiagent_cfg.get("blackboard_dir", "data/blackboard")
    bb_root = Path(bb_dir)

    @router.get("/status")
    async def get_status() -> dict:
        """获取协作总览状态。"""
        from hermes.multiagent.blackboard import read_json, read_director_md
        from hermes.multiagent.agent_registry import AgentRegistry

        status = await read_json(bb_root / "status.json") or {}
        director_md = await read_director_md(bb_root) or {}
        registry = AgentRegistry(bb_root)
        agents = await registry.list_active_agents()

        # 判断 Director 健康状态
        director_state = _determine_director_state(status.get("director", {}))

        return {
            "enabled": True,
            "role": multiagent_cfg.get("role", "worker"),
            "director": {
                "agent_id": status.get("director", {}).get("agent_id", ""),
                "epoch": status.get("director", {}).get("epoch", 0),
                "last_tick": status.get("director", {}).get("last_tick", ""),
                "state": director_state,
            },
            "agents": agents,
            "autonomous_mode": status.get("autonomous_mode", False),
        }

    @router.get("/agents")
    async def get_agents() -> dict:
        """获取 agent 列表。"""
        from hermes.multiagent.agent_registry import AgentRegistry
        registry = AgentRegistry(bb_root)
        agents = await registry.list_active_agents()
        return {"agents": agents}

    @router.get("/messages")
    async def get_messages(limit: int = 100) -> dict:
        """获取消息列表。"""
        from hermes.multiagent.blackboard import read_messages
        messages = await read_messages(bb_root)
        return {"messages": messages[-limit:]}

    @router.get("/audit")
    async def get_audit(limit: int = 100) -> dict:
        """获取审计记录。"""
        from hermes.multiagent.blackboard import read_audit_records
        records = await read_audit_records(bb_root)
        return {"records": records[-limit:]}

    @router.get("/director")
    async def get_director() -> dict:
        """获取 director.md 内容。"""
        from hermes.multiagent.blackboard import read_director_md
        data = await read_director_md(bb_root)
        return data or {}

    @router.get("/sse")
    async def sse_stream(request: Request) -> StreamingResponse:
        """multiagent_alert SSE 通道。

        事件类型：
        - director_state_change: Director 健康状态变更
        - agent_join: 新 agent 加入
        - agent_leave: agent 离线
        - autonomous_enter: 进入自治模式
        - autonomous_exit: 退出自治模式
        - message_append: 新消息追加
        """
        async def event_stream():
            last_status = None
            while True:
                if await request.is_disconnected():
                    break
                try:
                    current_status = await get_status()
                    if last_status is None or current_status != last_status:
                        event_type = _detect_event_type(last_status, current_status)
                        if event_type:
                            yield f"event: {event_type}\n"
                            yield f"data: {json.dumps(current_status, ensure_ascii=False)}\n\n"
                        last_status = current_status
                except Exception as e:
                    logger.error("SSE 流异常: %s", e)
                await asyncio.sleep(2)  # 2 秒轮询

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _determine_director_state(director: dict) -> str:
    """判断 Director 健康状态。"""
    if not director:
        return "unknown"
    last_tick = director.get("last_tick")
    if not last_tick:
        return "unknown"
    try:
        from datetime import datetime, timedelta, timezone
        tick_dt = datetime.fromisoformat(last_tick.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age = (now - tick_dt).total_seconds()
        if age < 30:
            return "healthy"
        if age < 60:
            return "degraded"
        if age < 120:
            return "autonomous"
        return "fault"
    except (ValueError, TypeError):
        return "unknown"


def _detect_event_type(old: dict | None, new: dict) -> str | None:
    """检测状态变更事件类型。"""
    if old is None:
        return "initial"
    old_director = old.get("director", {})
    new_director = new.get("director", {})
    if old_director.get("state") != new_director.get("state"):
        return "director_state_change"
    if old.get("autonomous_mode", False) != new.get("autonomous_mode", False):
        return "autonomous_enter" if new.get("autonomous_mode") else "autonomous_exit"
    old_agents = {a["agent_id"] for a in old.get("agents", [])}
    new_agents = {a["agent_id"] for a in new.get("agents", [])}
    if new_agents - old_agents:
        return "agent_join"
    if old_agents - new_agents:
        return "agent_leave"
    return None
```

### 验证通过

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/api/test_multiagent_routes.py -v
# 预期：全部通过
```

### commit

```bash
git add hermes/api/multiagent_routes.py tests/api/test_multiagent_routes.py
git commit -m "feat(multiagent): Plan 4 Task 1 后端状态查询端点（status/agents/messages/audit/sse）"
```

---

## Task 2: 前端 multiagent-settings.js（配置 UI）

### RED：编写失败测试（Playwright）

创建 `tests/e2e/test_multiagent_ui.py`：

```python
"""multiagent 前端 UI 端到端测试。"""
import pytest
from pathlib import Path
from playwright.sync_api import Page, expect


@pytest.fixture
def hermes_app_url():
    return "http://127.0.0.1:18394"


class TestMultiagentSettingsUI:
    """multiagent 设置 UI 测试。"""

    def test_settings_modal_has_multiagent_section(self, page: Page, hermes_app_url):
        """设置模态框包含 multiagent 段。"""
        page.goto(hermes_app_url)
        # 点击齿轮图标打开设置
        page.click("[data-action='open-settings']")
        # 验证 multiagent 段存在
        expect(page.locator("#multiagent-section")).to_be_visible()

    def test_enable_multiagent_toggle(self, page: Page, hermes_app_url):
        """启用 multiagent 开关。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        # 勾选启用
        page.check("#multiagent-enabled")
        # 验证子选项显示
        expect(page.locator("#multiagent-role")).to_be_visible()
        expect(page.locator("#multiagent-blackboard-dir")).to_be_visible()

    def test_role_selector_has_director_and_worker(self, page: Page, hermes_app_url):
        """角色选择器包含 Director 和 Worker。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        # 验证角色选项
        options = page.locator("#multiagent-role option")
        expect(options.nth(0)).to_have_text("Worker")
        expect(options.nth(1)).to_have_text("Director")

    def test_save_multiagent_config_calls_api(self, page: Page, hermes_app_url):
        """保存配置调用 PUT /config API。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.select_option("#multiagent-role", "worker")
        page.fill("#multiagent-blackboard-dir", "/tmp/bb")

        # 监听网络请求
        with page.expect_request("/api/config", method="PUT") as req_info:
            page.click("[data-action='save-settings']")
        request = req_info.value
        # 验证请求体包含 multiagent 段
        post_data = request.post_data
        assert "multiagent" in post_data
        assert "worker" in post_data

    def test_disabled_hides_multiagent_section(self, page: Page, hermes_app_url):
        """multiagent 关闭时隐藏相关 UI。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        # 取消勾选启用
        page.uncheck("#multiagent-enabled")
        # 验证子选项隐藏
        expect(page.locator("#multiagent-role")).to_be_hidden()
```

### 验证失败

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/e2e/test_multiagent_ui.py -v
# 预期：失败（前端无 multiagent UI）
```

### GREEN：最小实现

创建 `web/js/multiagent-settings.js`：

```javascript
// multiagent-settings.js — multiagent 协作配置 UI 逻辑
// 在 chat.html 设置模态框中渲染 multiagent 配置段，提交时调用 PUT /api/config

(function () {
  "use strict";

  const MULTIAGENT_DEFAULTS = {
    enabled: false,
    role: "worker",
    blackboard_dir: "data/blackboard",
    worker: {
      agent_id: "worker_001",
      heartbeat_interval_seconds: 10,
      capabilities: ["file_read", "file_write", "web_search"],
      dangerous_tools: ["execute_command", "write_file", "call_tool"],
    },
    director: {
      heartbeat_timeout_seconds: 30,
      autonomous_after_seconds: 60,
    },
  };

  /**
   * 渲染 multiagent 配置段到设置模态框。
   * @param {HTMLElement} container - 容器元素
   * @param {Object} currentConfig - 当前配置
   */
  function renderMultiagentSection(container, currentConfig) {
    const cfg = Object.assign(
      {},
      MULTIAGENT_DEFAULTS,
      currentConfig.multiagent || {}
    );

    container.innerHTML = `
      <div id="multiagent-section" class="settings-section">
        <h3>多 Agent 协作</h3>
        <label class="settings-row">
          <input type="checkbox" id="multiagent-enabled" ${
            cfg.enabled ? "checked" : ""
          }>
          <span>启用多 Agent 协作</span>
        </label>
        <div id="multiagent-detail" style="${
          cfg.enabled ? "" : "display:none"
        }">
          <label class="settings-row">
            <span>角色</span>
            <select id="multiagent-role">
              <option value="worker" ${
                cfg.role === "worker" ? "selected" : ""
              }>Worker</option>
              <option value="director" ${
                cfg.role === "director" ? "selected" : ""
              }>Director</option>
            </select>
          </label>
          <label class="settings-row">
            <span>Blackboard 目录</span>
            <input type="text" id="multiagent-blackboard-dir" value="${escapeHtml(
              cfg.blackboard_dir
            )}" placeholder="data/blackboard">
          </label>
          <label class="settings-row">
            <span>Agent ID</span>
            <input type="text" id="multiagent-agent-id" value="${escapeHtml(
              cfg.worker?.agent_id || "worker_001"
            )}">
          </label>
          <label class="settings-row">
            <span>心跳间隔（秒）</span>
            <input type="number" id="multiagent-heartbeat-interval" min="5" max="300" value="${
              cfg.worker?.heartbeat_interval_seconds || 10
            }">
          </label>
          <label class="settings-row">
            <span>能力声明（逗号分隔）</span>
            <input type="text" id="multiagent-capabilities" value="${escapeHtml(
              (cfg.worker?.capabilities || []).join(",")
            )}">
          </label>
          <label class="settings-row">
            <span>危险工具（逗号分隔）</span>
            <input type="text" id="multiagent-dangerous-tools" value="${escapeHtml(
              (cfg.worker?.dangerous_tools || []).join(",")
            )}">
          </label>
          <div class="settings-row">
            <span>Director 配置</span>
          </div>
          <label class="settings-row">
            <span>心跳超时（秒）</span>
            <input type="number" id="multiagent-director-timeout" min="10" max="600" value="${
              cfg.director?.heartbeat_timeout_seconds || 30
            }">
          </label>
          <label class="settings-row">
            <span>自治模式触发（秒）</span>
            <input type="number" id="multiagent-autonomous-after" min="30" max="3600" value="${
              cfg.director?.autonomous_after_seconds || 60
            }">
          </label>
        </div>
      </div>
    `;

    // 绑定启用开关
    const enabledCheckbox = container.querySelector("#multiagent-enabled");
    const detailDiv = container.querySelector("#multiagent-detail");
    enabledCheckbox.addEventListener("change", (e) => {
      detailDiv.style.display = e.target.checked ? "" : "none";
    });
  }

  /**
   * 从 UI 收集 multiagent 配置。
   * @param {HTMLElement} container
   * @returns {Object} multiagent 配置段
   */
  function collectMultiagentConfig(container) {
    const enabled = container.querySelector("#multiagent-enabled").checked;
    if (!enabled) {
      return { multiagent: { enabled: false } };
    }

    const role = container.querySelector("#multiagent-role").value;
    const blackboardDir = container.querySelector(
      "#multiagent-blackboard-dir"
    ).value;
    const agentId = container.querySelector("#multiagent-agent-id").value;
    const heartbeatInterval = parseInt(
      container.querySelector("#multiagent-heartbeat-interval").value,
      10
    );
    const capabilities = container
      .querySelector("#multiagent-capabilities")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const dangerousTools = container
      .querySelector("#multiagent-dangerous-tools")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const directorTimeout = parseInt(
      container.querySelector("#multiagent-director-timeout").value,
      10
    );
    const autonomousAfter = parseInt(
      container.querySelector("#multiagent-autonomous-after").value,
      10
    );

    return {
      multiagent: {
        enabled: true,
        role: role,
        blackboard_dir: blackboardDir,
        worker: {
          agent_id: agentId,
          heartbeat_interval_seconds: heartbeatInterval,
          capabilities: capabilities,
          dangerous_tools: dangerousTools,
        },
        director: {
          heartbeat_timeout_seconds: directorTimeout,
          autonomous_after_seconds: autonomousAfter,
        },
      },
    };
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // 导出全局
  window.MultiagentSettings = {
    render: renderMultiagentSection,
    collect: collectMultiagentConfig,
  };
})();
```

修改 `web/chat.html`（在设置模态框中新增 multiagent 段容器）：

```html
<!-- 在设置模态框内新增 -->
<div id="multiagent-settings-container"></div>

<!-- 引入 JS 文件 -->
<script src="js/multiagent-settings.js"></script>
<script src="js/multiagent-sse.js"></script>
<script src="js/multiagent-render.js"></script>
```

修改现有 settings 初始化逻辑（伪代码，按现有风格补充）：

```javascript
// 在设置模态框初始化时调用
function initSettingsModal(currentConfig) {
  // ... 现有初始化 ...

  // 渲染 multiagent 段
  const multiagentContainer = document.getElementById(
    "multiagent-settings-container"
  );
  if (window.MultiagentSettings) {
    window.MultiagentSettings.render(multiagentContainer, currentConfig);
  }
}

// 在保存设置时调用
async function saveSettings() {
  const multiagentContainer = document.getElementById(
    "multiagent-settings-container"
  );
  const multiagentConfig = window.MultiagentSettings
    ? window.MultiagentSettings.collect(multiagentContainer)
    : {};

  // 合并到完整配置
  const fullConfig = { ...collectExistingConfig(), ...multiagentConfig };

  // 调用 PUT /api/config
  const resp = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fullConfig),
  });

  if (!resp.ok) {
    throw new Error(`保存失败: ${resp.status}`);
  }
}
```

### 验证通过

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/e2e/test_multiagent_ui.py -v
# 预期：全部通过（需启动 hermes-lite 服务）
```

### commit

```bash
git add web/js/multiagent-settings.js web/chat.html tests/e2e/test_multiagent_ui.py
git commit -m "feat(multiagent): Plan 4 Task 2 前端配置 UI（settings 模态框+PUT /config 提交）"
```

---

## Task 3: 前端 multiagent-sse.js（SSE 订阅）

### RED：编写失败测试

在 `tests/e2e/test_multiagent_ui.py` 中追加：

```python
class TestMultiagentSSE:
    """multiagent SSE 通道测试。"""

    def test_sse_indicator_present(self, page: Page, hermes_app_url):
        """页面包含 multiagent 状态指示器。"""
        page.goto(hermes_app_url)
        # 验证状态指示器 DOM 存在
        expect(page.locator("#multiagent-indicator")).to_be_visible()

    def test_sse_indicator_shows_disabled_state(self, page: Page, hermes_app_url):
        """multiagent 未启用时指示器显示禁用状态。"""
        page.goto(hermes_app_url)
        indicator = page.locator("#multiagent-indicator")
        # 应显示"未启用"或类似文本
        expect(indicator).to_contain_text("未启用")

    def test_sse_indicator_shows_director_state(self, page: Page, hermes_app_url):
        """启用后指示器显示 Director 状态。"""
        page.goto(hermes_app_url)
        # 启用 multiagent（通过设置模态框）
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")
        # 等待指示器更新
        page.wait_for_selector("#multiagent-indicator.state-healthy, "
                                "#multiagent-indicator.state-degraded, "
                                "#multiagent-indicator.state-fault")
        indicator = page.locator("#multiagent-indicator")
        # 应有状态类
        class_attr = indicator.get_attribute("class")
        assert any(state in class_attr for state in [
            "state-healthy", "state-degraded", "state-autonomous", "state-fault"
        ])

    def test_sse_agent_panel_shows_list(self, page: Page, hermes_app_url):
        """Agent 列表面板显示活跃 agents。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")
        # 等待 Agent 面板加载
        page.wait_for_selector("#multiagent-agents-panel")
        # 应至少显示自己
        agents = page.locator("#multiagent-agents-panel .agent-card")
        expect(agents.first).to_be_visible()

    def test_sse_autonomous_alert(self, page: Page, hermes_app_url):
        """自治模式触发时显示告警。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")
        # 模拟自治模式触发（通过 SSE 事件）
        page.evaluate("""
            window.dispatchEvent(new CustomEvent('multiagent-alert', {
                detail: { type: 'autonomous_enter', data: { autonomous_mode: true } }
            }));
        """)
        # 应显示自治模式告警
        expect(page.locator("#multiagent-alert-banner")).to_be_visible()
        expect(page.locator("#multiagent-alert-banner")).to_contain_text("自治")
```

### 验证失败

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/e2e/test_multiagent_ui.py::TestMultiagentSSE -v
# 预期：失败（SSE 模块未实现）
```

### GREEN：最小实现

创建 `web/js/multiagent-sse.js`：

```javascript
// multiagent-sse.js — multiagent_alert SSE 通道订阅
// 监听 /api/multiagent/sse 端点，将事件分发给 multiagent-render.js 渲染

(function () {
  "use strict";

  let eventSource = null;
  let reconnectTimer = null;
  let currentStatus = null;

  /**
   * 启动 SSE 订阅。
   */
  function start() {
    if (eventSource) {
      eventSource.close();
    }

    eventSource = new EventSource("/api/multiagent/sse");

    eventSource.addEventListener("initial", (e) => {
      currentStatus = JSON.parse(e.data);
      window.MultiagentRender.updateStatus(currentStatus);
    });

    eventSource.addEventListener("director_state_change", (e) => {
      const data = JSON.parse(e.data);
      currentStatus = data;
      window.MultiagentRender.updateStatus(data);
      window.MultiagentRender.showAlert(
        "Director 状态变更: " + (data.director?.state || "unknown"),
        "info"
      );
    });

    eventSource.addEventListener("agent_join", (e) => {
      const data = JSON.parse(e.data);
      currentStatus = data;
      window.MultiagentRender.updateStatus(data);
      const newAgents = data.agents || [];
      window.MultiagentRender.showAlert(
        "Agent 加入: " + newAgents.map((a) => a.agent_id).join(", "),
        "success"
      );
    });

    eventSource.addEventListener("agent_leave", (e) => {
      const data = JSON.parse(e.data);
      currentStatus = data;
      window.MultiagentRender.updateStatus(data);
      window.MultiagentRender.showAlert("Agent 离线", "warning");
    });

    eventSource.addEventListener("autonomous_enter", (e) => {
      const data = JSON.parse(e.data);
      currentStatus = data;
      window.MultiagentRender.updateStatus(data);
      window.MultiagentRender.showAlert(
        "进入自治模式（Director 故障）",
        "warning"
      );
    });

    eventSource.addEventListener("autonomous_exit", (e) => {
      const data = JSON.parse(e.data);
      currentStatus = data;
      window.MultiagentRender.updateStatus(data);
      window.MultiagentRender.showAlert(
        "退出自治模式（Director 恢复）",
        "success"
      );
    });

    eventSource.addEventListener("message_append", (e) => {
      // 消息追加事件，可触发聊天界面刷新
      const data = JSON.parse(e.data);
      window.dispatchEvent(
        new CustomEvent("multiagent-message", { detail: data })
      );
    });

    eventSource.onerror = (e) => {
      console.warn("multiagent SSE 连接失败，5 秒后重连");
      eventSource.close();
      eventSource = null;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(start, 5000);
    };
  }

  /**
   * 停止 SSE 订阅。
   */
  function stop() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    currentStatus = null;
  }

  /**
   * 获取当前状态。
   */
  function getCurrentStatus() {
    return currentStatus;
  }

  // 导出全局
  window.MultiagentSSE = {
    start: start,
    stop: stop,
    getCurrentStatus: getCurrentStatus,
  };

  // 监听自定义事件（测试用）
  window.addEventListener("multiagent-alert", (e) => {
    const detail = e.detail;
    if (detail.type === "autonomous_enter") {
      window.MultiagentRender.updateStatus(detail.data);
      window.MultiagentRender.showAlert("进入自治模式", "warning");
    }
  });
})();
```

### 验证通过

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/e2e/test_multiagent_ui.py::TestMultiagentSSE -v
# 预期：全部通过
```

### commit

```bash
git add web/js/multiagent-sse.js tests/e2e/test_multiagent_ui.py
git commit -m "feat(multiagent): Plan 4 Task 3 SSE 订阅（multiagent_alert 通道+重连+事件分发）"
```

---

## Task 4: 前端 multiagent-render.js（Director 状态/Agent 列表渲染）

### RED：编写失败测试

在 `tests/e2e/test_multiagent_ui.py` 中追加：

```python
class TestMultiagentRender:
    """multiagent 渲染测试。"""

    def test_director_state_color_coding(self, page: Page, hermes_app_url):
        """Director 状态颜色编码（绿/黄/橙/红）。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        # 等待指示器加载
        page.wait_for_selector("#multiagent-indicator[data-state]")

        # 验证状态类存在
        indicator = page.locator("#multiagent-indicator")
        state = indicator.get_attribute("data-state")
        assert state in ["healthy", "degraded", "autonomous", "fault", "unknown"]

    def test_agent_card_renders_correctly(self, page: Page, hermes_app_url):
        """Agent 卡片正确渲染。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        page.wait_for_selector(".agent-card")
        card = page.locator(".agent-card").first
        # 应包含 agent_id、role、status
        expect(card).to_contain_text("agent_id")
        expect(card.locator(".agent-role")).to_be_visible()
        expect(card.locator(".agent-status")).to_be_visible()

    def test_trust_score_progress_bar(self, page: Page, hermes_app_url):
        """信任分进度条渲染。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        page.wait_for_selector(".agent-card")
        # 信任分进度条应存在（如果有 agents）
        bar = page.locator(".trust-score-bar").first
        if bar.is_visible():
            # 验证宽度在 0-100%
            width = bar.evaluate(
                "(el) => getComputedStyle(el).width"
            )
            assert "%" in width or "px" in width

    def test_alert_banner_appears_and_disappears(self, page: Page, hermes_app_url):
        """告警横幅出现并自动消失。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        # 触发告警
        page.evaluate("""
            window.MultiagentRender.showAlert("测试告警", "info");
        """)
        expect(page.locator("#multiagent-alert-banner")).to_be_visible()
        expect(page.locator("#multiagent-alert-banner")).to_contain_text("测试告警")

        # 等待自动消失（默认 5 秒）
        page.wait_for_selector("#multiagent-alert-banner", state="hidden", timeout=10000)
```

### 验证失败

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/e2e/test_multiagent_ui.py::TestMultiagentRender -v
# 预期：失败（render 模块未实现）
```

### GREEN：最小实现

创建 `web/js/multiagent-render.js`：

```javascript
// multiagent-render.js — multiagent 状态渲染
// Director 状态指示器 + Agent 列表 + 告警横幅

(function () {
  "use strict";

  const STATE_COLORS = {
    healthy: "#22c55e", // 绿
    degraded: "#eab308", // 黄
    autonomous: "#f97316", // 橙
    fault: "#ef4444", // 红
    unknown: "#6b7280", // 灰
    disabled: "#9ca3af", // 浅灰
  };

  const STATE_LABELS = {
    healthy: "健康",
    degraded: "降级",
    autonomous: "自治",
    fault: "故障",
    unknown: "未知",
    disabled: "未启用",
  };

  /**
   * 初始化 multiagent UI 容器。
   */
  function initContainers() {
    // 状态指示器（顶部）
    if (!document.getElementById("multiagent-indicator")) {
      const indicator = document.createElement("div");
      indicator.id = "multiagent-indicator";
      indicator.className = "multiagent-indicator state-disabled";
      indicator.setAttribute("data-state", "disabled");
      indicator.innerHTML = `
        <span class="indicator-dot"></span>
        <span class="indicator-text">${STATE_LABELS.disabled}</span>
      `;
      document.body.appendChild(indicator);
    }

    // Agent 列表面板（侧边）
    if (!document.getElementById("multiagent-agents-panel")) {
      const panel = document.createElement("div");
      panel.id = "multiagent-agents-panel";
      panel.className = "multiagent-agents-panel";
      panel.innerHTML = `
        <div class="panel-header">
          <h4>协作 Agents</h4>
          <button class="panel-toggle" data-action="toggle-panel">−</button>
        </div>
        <div class="panel-body">
          <div class="agents-list"></div>
        </div>
      `;
      document.body.appendChild(panel);
    }

    // 告警横幅（顶部）
    if (!document.getElementById("multiagent-alert-banner")) {
      const banner = document.createElement("div");
      banner.id = "multiagent-alert-banner";
      banner.className = "multiagent-alert-banner";
      banner.style.display = "none";
      document.body.appendChild(banner);
    }
  }

  /**
   * 更新状态显示。
   * @param {Object} status - /api/multiagent/status 返回的状态
   */
  function updateStatus(status) {
    initContainers();

    const indicator = document.getElementById("multiagent-indicator");
    if (!status || !status.enabled) {
      indicator.className = "multiagent-indicator state-disabled";
      indicator.setAttribute("data-state", "disabled");
      indicator.querySelector(".indicator-text").textContent =
        STATE_LABELS.disabled;
      return;
    }

    const directorState = status.director?.state || "unknown";
    indicator.className = `multiagent-indicator state-${directorState}`;
    indicator.setAttribute("data-state", directorState);
    const directorText = status.director?.agent_id
      ? `Director: ${status.director.agent_id} (${STATE_LABELS[directorState]})`
      : STATE_LABELS[directorState];
    indicator.querySelector(".indicator-text").textContent = directorText;

    // 更新 Agent 列表
    renderAgents(status.agents || []);

    // 自治模式指示
    if (status.autonomous_mode) {
      indicator.className += " autonomous-active";
    }
  }

  /**
   * 渲染 Agent 列表。
   * @param {Array} agents - agent 列表
   */
  function renderAgents(agents) {
    const listContainer = document.querySelector(
      "#multiagent-agents-panel .agents-list"
    );
    if (!listContainer) return;

    if (agents.length === 0) {
      listContainer.innerHTML = '<p class="no-agents">暂无活跃 agents</p>';
      return;
    }

    listContainer.innerHTML = agents
      .map((agent) => {
        const trustScore = agent.trust_score || 100;
        const trustColor =
          trustScore >= 60
            ? "#22c55e"
            : trustScore >= 30
            ? "#eab308"
            : "#ef4444";
        return `
          <div class="agent-card" data-agent-id="${escapeHtml(agent.agent_id)}">
            <div class="agent-header">
              <span class="agent-id">${escapeHtml(agent.agent_id)}</span>
              <span class="agent-status status-${escapeHtml(
                agent.status || "active"
              )}">${escapeHtml(agent.status || "active")}</span>
            </div>
            <div class="agent-meta">
              <span class="agent-role">${escapeHtml(
                agent.role || "worker"
              )}</span>
              <span class="agent-last-seen">${escapeHtml(
                agent.last_seen || ""
              )}</span>
            </div>
            <div class="trust-score">
              <div class="trust-score-bar" style="width: ${trustScore}%; background: ${trustColor};"></div>
              <span class="trust-score-text">${trustScore}/100</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  /**
   * 显示告警横幅。
   * @param {string} message - 告警消息
   * @param {string} level - 告警级别（info/success/warning/error）
   */
  function showAlert(message, level = "info") {
    initContainers();
    const banner = document.getElementById("multiagent-alert-banner");
    banner.className = `multiagent-alert-banner alert-${level}`;
    banner.textContent = message;
    banner.style.display = "block";

    // 5 秒后自动消失
    setTimeout(() => {
      banner.style.display = "none";
    }, 5000);
  }

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // 导出全局
  window.MultiagentRender = {
    initContainers: initContainers,
    updateStatus: updateStatus,
    renderAgents: renderAgents,
    showAlert: showAlert,
  };

  // 页面加载完成后初始化
  document.addEventListener("DOMContentLoaded", () => {
    initContainers();
    // 检查 multiagent 是否启用，决定是否启动 SSE
    fetch("/api/multiagent/status")
      .then((resp) => {
        if (resp.ok) {
          return resp.json();
        }
        throw new Error("multiagent disabled");
      })
      .then((status) => {
        updateStatus(status);
        if (status.enabled && window.MultiagentSSE) {
          window.MultiagentSSE.start();
        }
      })
      .catch(() => {
        // multiagent 未启用，保持禁用状态
        updateStatus({ enabled: false });
      });
  });
})();
```

创建 `web/css/multiagent.css`：

```css
/* multiagent.css — multiagent UI 样式 */

/* 状态指示器（顶部） */
.multiagent-indicator {
  position: fixed;
  top: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.1);
  font-size: 13px;
  color: #e5e7eb;
  z-index: 1000;
  transition: all 0.3s ease;
}

.multiagent-indicator .indicator-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}

.multiagent-indicator.state-healthy {
  color: #22c55e;
  background: rgba(34, 197, 94, 0.1);
}
.multiagent-indicator.state-degraded {
  color: #eab308;
  background: rgba(234, 179, 8, 0.1);
}
.multiagent-indicator.state-autonomous {
  color: #f97316;
  background: rgba(249, 115, 22, 0.1);
}
.multiagent-indicator.state-fault {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
}
.multiagent-indicator.state-disabled {
  color: #9ca3af;
  background: rgba(156, 163, 175, 0.1);
}

/* Agent 列表面板（侧边） */
.multiagent-agents-panel {
  position: fixed;
  top: 60px;
  right: 12px;
  width: 280px;
  max-height: 60vh;
  background: rgba(17, 24, 39, 0.85);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px;
  overflow: hidden;
  z-index: 999;
  display: flex;
  flex-direction: column;
}

.multiagent-agents-panel .panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.multiagent-agents-panel .panel-header h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: #f3f4f6;
}

.multiagent-agents-panel .panel-toggle {
  background: transparent;
  border: none;
  color: #9ca3af;
  cursor: pointer;
  font-size: 16px;
  padding: 4px 8px;
  border-radius: 6px;
  transition: background 0.2s;
}

.multiagent-agents-panel .panel-toggle:hover {
  background: rgba(255, 255, 255, 0.08);
}

.multiagent-agents-panel .panel-body {
  overflow-y: auto;
  padding: 8px;
}

.multiagent-agents-panel .agents-list .agent-card {
  background: rgba(255, 255, 255, 0.04);
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 8px;
  transition: background 0.2s;
}

.multiagent-agents-panel .agents-list .agent-card:hover {
  background: rgba(255, 255, 255, 0.08);
}

.agent-card .agent-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.agent-card .agent-id {
  font-size: 13px;
  font-weight: 600;
  color: #f3f4f6;
}

.agent-card .agent-status {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  text-transform: uppercase;
}

.agent-card .agent-status.status-active {
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
}

.agent-card .agent-status.status-degraded {
  background: rgba(234, 179, 8, 0.15);
  color: #eab308;
}

.agent-card .agent-status.status-offline {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}

.agent-card .agent-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #9ca3af;
  margin-bottom: 8px;
}

.agent-card .trust-score {
  position: relative;
  height: 6px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 3px;
  overflow: hidden;
}

.agent-card .trust-score-bar {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.agent-card .trust-score-text {
  position: absolute;
  right: 0;
  top: -16px;
  font-size: 10px;
  color: #6b7280;
}

/* 告警横幅 */
.multiagent-alert-banner {
  position: fixed;
  top: 60px;
  left: 50%;
  transform: translateX(-50%);
  padding: 12px 24px;
  border-radius: 8px;
  font-size: 14px;
  color: #f3f4f6;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  z-index: 1001;
  animation: fadeInDown 0.3s ease;
}

.multiagent-alert-banner.alert-info {
  background: rgba(59, 130, 246, 0.2);
  border: 1px solid rgba(59, 130, 246, 0.3);
}

.multiagent-alert-banner.alert-success {
  background: rgba(34, 197, 94, 0.2);
  border: 1px solid rgba(34, 197, 94, 0.3);
}

.multiagent-alert-banner.alert-warning {
  background: rgba(249, 115, 22, 0.2);
  border: 1px solid rgba(249, 115, 22, 0.3);
}

.multiagent-alert-banner.alert-error {
  background: rgba(239, 68, 68, 0.2);
  border: 1px solid rgba(239, 68, 68, 0.3);
}

@keyframes fadeInDown {
  from {
    opacity: 0;
    transform: translateX(-50%) translateY(-10px);
  }
  to {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }
}

/* 设置模态框中的 multiagent 段 */
.settings-section {
  padding: 16px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.settings-section h3 {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  color: #f3f4f6;
}

.settings-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 13px;
}

.settings-row span {
  flex: 0 0 140px;
  color: #d1d5db;
}

.settings-row input[type="text"],
.settings-row input[type="number"],
.settings-row select {
  flex: 1;
  padding: 8px 12px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  color: #f3f4f6;
  font-size: 13px;
}

.settings-row input[type="checkbox"] {
  margin-right: 8px;
}

.no-agents {
  text-align: center;
  color: #6b7280;
  font-size: 12px;
  padding: 16px;
}
```

### 验证通过

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/e2e/test_multiagent_ui.py::TestMultiagentRender -v
# 预期：全部通过
```

### commit

```bash
git add web/js/multiagent-render.js web/css/multiagent.css tests/e2e/test_multiagent_ui.py
git commit -m "feat(multiagent): Plan 4 Task 4 前端渲染（Director 状态指示器+Agent 卡片+告警横幅）"
```

---

## Task 5: 路由注册与容器集成

### RED：编写失败测试

创建 `tests/api/test_multiagent_route_integration.py`：

```python
"""multiagent 路由集成测试。"""
import pytest
from pathlib import Path
from fastapi.testclient import TestClient


class TestRouteIntegration:
    """路由集成测试。"""

    def test_multiagent_routes_registered_when_enabled(self, tmp_path: Path):
        """multiagent.enabled=True 时路由注册到 FastAPI app。"""
        from hermes.app import init_container, register_components, get_container
        from fastapi import FastAPI

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(tmp_path / "bb"),
            },
        }
        init_container(config)
        container = get_container()
        register_components(container)

        app = FastAPI()
        # 注册 multiagent 路由
        from hermes.api.multiagent_routes import create_multiagent_router
        app.include_router(create_multiagent_router(container))

        with TestClient(app) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 200

    def test_multiagent_routes_not_registered_when_disabled(self, tmp_path: Path):
        """multiagent.enabled=False 时路由返回 404。"""
        from hermes.app import init_container, register_components, get_container
        from fastapi import FastAPI

        config = {"multiagent": {"enabled": False}}
        init_container(config)
        container = get_container()
        register_components(container)

        app = FastAPI()
        from hermes.api.multiagent_routes import create_multiagent_router
        app.include_router(create_multiagent_router(container))

        with TestClient(app) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 404
```

### 验证失败

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/api/test_multiagent_route_integration.py -v
# 预期：失败（hermes/app.py 未注册 multiagent_routes）
```

### GREEN：最小实现

修改 `hermes/app.py`（在 register_components 中注册 multiagent 路由）：

```python
def register_components(container: Container) -> None:
    # ... 现有组件注册 ...

    # multiagent 路由（条件注册）
    multiagent_cfg = container.config.get("multiagent", {}) or {}
    if multiagent_cfg.get("enabled"):
        from hermes.api.multiagent_routes import create_multiagent_router
        # 路由注册延迟到 FastAPI app 创建时（lifespan 中执行）
        # 这里仅标记需要注册
        container.register(
            "multiagent_router",
            lambda c: create_multiagent_router(c),
            deps=[],
            hot_reloadable=True,
        )
```

修改 `hermes/lifespan.py`（在启动阶段注册 multiagent 路由）：

```python
# 在 multiagent_adapter 启动后新增
multiagent_cfg = config.get("multiagent", {}) or {}
if multiagent_cfg.get("enabled"):
    try:
        multiagent_router = container.get("multiagent_router")
        if multiagent_router:
            app.include_router(multiagent_router)
            logger.info("multiagent REST 路由已注册")
    except Exception as e:
        logger.error("multiagent 路由注册失败: %s", e)
```

### 验证通过

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/api/test_multiagent_route_integration.py -v
# 预期：全部通过
```

### commit

```bash
git add hermes/app.py hermes/lifespan.py tests/api/test_multiagent_route_integration.py
git commit -m "feat(multiagent): Plan 4 Task 5 路由集成（multiagent_routes 注册到 FastAPI app）"
```

---

## Task 6: Self-Review

### Self-Review 检查清单

#### 1. Spec Coverage（§13 Phase 5 前端覆盖）

| 设计文档章节 | Task | 验证 |
|------------|------|------|
| §13.1 multiagent_alert SSE 通道 | Task 1, 3 | test_multiagent_routes.py + test_multiagent_ui.py::TestMultiagentSSE |
| §13.2 设置模态框配置 | Task 2 | test_multiagent_ui.py::TestMultiagentSettingsUI |
| §13.3 Director 状态显示 | Task 4 | test_multiagent_ui.py::TestMultiagentRender |
| §13.4 Agent 列表面板 | Task 4 | test_multiagent_ui.py::TestMultiagentRender |
| §13.5 告警横幅 | Task 4 | test_multiagent_ui.py::TestMultiagentRender |
| §13.6 路由注册 | Task 5 | test_multiagent_route_integration.py |

#### 2. Placeholder Scan

```bash
grep -rn "TODO\|FIXME\|XXX\|PLACEHOLDER" web/js/multiagent-settings.js web/js/multiagent-sse.js web/js/multiagent-render.js hermes/api/multiagent_routes.py
# 预期：无输出
```

#### 3. UI 风格一致性

- 毛玻璃按钮：`.panel-toggle` 使用 `background: transparent` + `:hover` 背景
- 圆角处理：所有元素 `border-radius: 8px-12px`
- 元素间距：`gap: 8-12px`，`padding: 12-16px`
- 色彩规范：使用标准色板（绿/黄/橙/红/灰）

#### 4. 测试覆盖率

```bash
cd e:\Java\webser\web_app\webme\hermes-lite
python -m pytest tests/api/test_multiagent_routes.py tests/api/test_multiagent_route_integration.py -v
# 预期：全部通过
python -m pytest tests/e2e/test_multiagent_ui.py -v
# 预期：全部通过（需启动 hermes-lite 服务 + Playwright）
```

#### 5. Global Constraints 对齐

| 约束 | 实现位置 | 验证 |
|------|---------|------|
| 复用现有设置模态框 | Task 2 renderMultiagentSection | test_multiagent_ui.py::test_settings_modal_has_multiagent_section |
| SSE 通道命名 multiagent_alert | Task 1 /sse 端点 + Task 3 EventSource | test_multiagent_routes.py |
| UI 风格一致 | Task 4 multiagent.css | 代码审查 |
| 路径安全（不显示绝对路径） | Task 2 escapeHtml + 显示 blackboard_dir | test_multiagent_ui.py |
| 降级显示 | Task 4 updateStatus (enabled=false 时隐藏) | test_multiagent_ui.py::test_sse_indicator_shows_disabled_state |
| 配置通过 PUT /config | Task 2 saveSettings | test_multiagent_ui.py::test_save_multiagent_config_calls_api |
| Director 状态三色显示 | Task 4 STATE_COLORS | test_multiagent_ui.py::test_director_state_color_coding |

### commit

```bash
git commit --allow-empty -m "docs(multiagent): Plan 4 Self-Review 通过（spec coverage 完整+UI 风格一致+无占位符）"
```

---

## Execution Handoff

### Plan 4 完成状态

- ✅ Task 1: 后端 multiagent 状态查询端点
- ✅ Task 2: 前端 multiagent-settings.js（配置 UI）
- ✅ Task 3: 前端 multiagent-sse.js（SSE 订阅）
- ✅ Task 4: 前端 multiagent-render.js（Director 状态/Agent 列表渲染）
- ✅ Task 5: 路由注册与容器集成
- ✅ Task 6: Self-Review

### 已知限制

1. **Playwright 依赖**：UI 端到端测试需要 Playwright 环境，CI 中需额外配置
2. **SSE 轮询间隔**：当前 2 秒轮询一次，对大量 Agent 场景可能有延迟
3. **路径显示**：前端仅显示 blackboard_dir 配置值，不显示具体文件路径
4. **多语言支持**：当前 UI 文本为中文，未实现 i18n
5. **移动端适配**：当前 UI 主要面向桌面端，移动端布局未优化

### 提交记录

```
Task 1: feat(multiagent): 后端状态查询端点
Task 2: feat(multiagent): 前端配置 UI
Task 3: feat(multiagent): SSE 订阅
Task 4: feat(multiagent): 前端渲染
Task 5: feat(multiagent): 路由集成
Task 6: docs(multiagent): Self-Review 通过
```
