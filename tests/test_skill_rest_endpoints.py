"""Skill REST 端点单元测试（P1 双路径化 + 软禁用改造）。

覆盖 ``src/server.py`` 的 4 个 Skill 端点：
- ``GET /skills/{name}``：返回 meta 信息（含 stub_registered / disabled / body_preview）
- ``POST /skills/{name}/reload``：双路径注册，返回 stub_registered: True
- ``POST /skills/{name}/toggle``：enable + disable 两个 action（基于状态文件翻转）
- ``DELETE /skills/{name}``：删除 skill 目录与 registry 中的工具

setup 要点（参考 ``tests/test_task_api.py`` / ``tests/test_memory_dashboard_api.py``）：
- 使用 ``tests/_mock_deps.py`` 注入 chromadb / numpy 等 mock。
- 真实 ``ToolRegistry`` + ``register_skill_stub`` 验证端到端注册行为。
- ``MagicMock(spec=SkillLoader)`` 但 ``_metas`` 设为真实 dict（含真实 ``SkillMeta``）。
- ``orchestrator`` 用最小 mock 类持有 ``tool_registry`` 属性。
- 每个测试用例使用独立临时目录隔离文件 I/O。

运行方式:
    python -m pytest tests/test_skill_rest_endpoints.py -v
    python -m unittest tests.test_skill_rest_endpoints -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.server import app  # noqa: E402
from teage_liu.app import get_skill_loader, get_orchestrator  # noqa: E402
from teage_liu.agent.tool_registry import ToolRegistry  # noqa: E402
from teage_liu.skill.loader import (  # noqa: E402
    Skill,
    SkillLoader,
    SkillMeta,
    register_skill_stub,
)

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def _make_meta(
    name: str = "rest_skill",
    description: str = "REST test skill",
    body: str = "REST skill body content",
) -> SkillMeta:
    """构造真实 SkillMeta 实例（避免 isinstance 检查失败）。"""
    return SkillMeta(
        name=name,
        version="0.1.0",
        description=description,
        requires=[],
        body=body,
    )


def _write_skill_md(skill_dir: Path, name: str, description: str = "test") -> None:
    """在指定目录下创建 SKILL.md（合法 frontmatter + body）。"""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        f"description: {description}\n"
        "requires: []\n"
        "---\n"
        f"\n# {name}\n\nSkill body.\n",
        encoding="utf-8",
    )


class _MockOrchestrator:
    """最小可调用 orchestrator mock，仅暴露 tool_registry 属性。

    不使用 ``MagicMock`` 是因为 ``_make_skill_activate_handler`` 内
    ``getattr(orchestrator, "_current_session_id", None)`` 会取到 MagicMock
    的自动属性（非 None），影响 handler 行为。本类显式不设置该属性，
    使 ``getattr`` 返回 None，handler 走 "default" 兜底分支。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.tool_registry = registry
        self._current_session_id = None


# ===========================================================================
# 测试类：Skill REST 端点
# ===========================================================================


class TestSkillRestEndpoints(unittest.TestCase):
    """4 个 Skill REST 端点的集成测试。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        # 真实 ToolRegistry（不 mock，验证端到端注册行为）
        self.registry = ToolRegistry()
        # 真实 SkillLoader mock，但 _metas / _skills 设为真实容器
        self.skill_loader = MagicMock(spec=SkillLoader)
        self.skill_loader._metas = {}
        self.skill_loader._skills = {}
        self.skill_loader.discover = MagicMock(return_value=[])
        self.skill_loader.skill_dir = self.tmpdir
        # orchestrator mock 持有 tool_registry
        self.orch = _MockOrchestrator(self.registry)
        # DI 覆盖：通过 dependency_overrides 注入 mock 组件
        app.dependency_overrides[get_skill_loader] = lambda: self.skill_loader
        app.dependency_overrides[get_orchestrator] = lambda: self.orch
        # SKILL_STATE_PATH 仍通过 patch 注入（非 Depends 组件）
        self._patches = [
            patch("teage_liu.server.SKILL_STATE_PATH", self.tmpdir / "state.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        app.dependency_overrides.pop(get_skill_loader, None)
        app.dependency_overrides.pop(get_orchestrator, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def _register_stub(self, name: str) -> SkillMeta:
        """预置一个真实 SkillMeta 并注册 stub 到 Core Tier。"""
        meta = _make_meta(name=name)
        self.skill_loader._metas[name] = meta
        register_skill_stub(self.registry, meta, lambda **kw: "ok")
        return meta

    # ---- GET /skills/{name} ---------------------------------------------

    def test_get_skill_returns_meta_info(self):
        """GET /skills/{name} 返回 meta + stub_registered/disabled/body_preview 字段。"""
        self._register_stub("rest_skill")
        client = self._client()
        resp = client.get("/skills/rest_skill")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # meta 字段
        self.assertEqual(data["name"], "rest_skill")
        self.assertEqual(data["version"], "0.1.0")
        self.assertEqual(data["description"], "REST test skill")
        # 新增字段
        self.assertIn("body_preview", data)
        self.assertIn("stub_registered", data)
        self.assertIn("disabled", data)
        # stub 已注册 → True；未禁用 → False
        self.assertTrue(data["stub_registered"])
        self.assertFalse(data["disabled"])
        # body_preview 含 body 内容
        self.assertIn("REST skill body", data["body_preview"])

    def test_get_skill_404_when_not_found(self):
        """GET /skills/{name} 不存在时返回 404。"""
        # _metas 空 + discover 返回空 → 404
        self.skill_loader._metas = {}
        self.skill_loader.discover.return_value = []
        client = self._client()
        resp = client.get("/skills/nonexistent_skill")
        self.assertEqual(resp.status_code, 404)

    def test_get_skill_503_when_skill_loader_none(self):
        """skill_loader 为 None 时返回 503。"""
        # 临时将 skill_loader 覆盖为 None
        app.dependency_overrides[get_skill_loader] = lambda: None
        try:
            client = self._client()
            resp = client.get("/skills/any")
        finally:
            # 恢复为 setUp 中的 mock
            app.dependency_overrides[get_skill_loader] = lambda: self.skill_loader
        self.assertEqual(resp.status_code, 503)

    # ---- POST /skills/{name}/reload -------------------------------------

    def test_reload_skill_returns_stub_registered_true(self):
        """POST /skills/{name}/reload 双路径注册，返回 stub_registered: True。"""
        name = "reload_skill"
        # 预置磁盘 SKILL.md 与 _metas 缓存
        _write_skill_md(self.tmpdir / name, name)
        meta = _make_meta(name=name)
        self.skill_loader._metas[name] = meta
        # reload 返回真实 Skill（无业务工具，仅走新路径）
        self.skill_loader.reload.return_value = Skill(
            name=name, tools=[], system_prompt="", handlers={}
        )
        # _parse_meta 兜底返回真实 meta
        self.skill_loader._parse_meta.return_value = meta

        client = self._client()
        resp = client.post(f"/skills/{name}/reload")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "reloaded")
        self.assertEqual(data["skill_name"], name)
        # 新路径 stub 注册成功
        self.assertTrue(data["stub_registered"])
        # registry 中确实有 skill__{name} 激活按钮
        schema = self.registry.get_full_schema(f"skill__{name}")
        self.assertTrue(schema, "skill stub 应已注册到 Core Tier")

    def test_reload_skill_404_when_load_fails(self):
        """reload 时 skill_loader.reload 返回 None → 404。"""
        name = "missing_skill"
        self.skill_loader.reload.return_value = None
        client = self._client()
        resp = client.post(f"/skills/{name}/reload")
        self.assertEqual(resp.status_code, 404)

    # ---- POST /skills/{name}/toggle -------------------------------------

    def test_toggle_disable_returns_disabled_and_soft_disables(self):
        """POST /skills/{name}/toggle（未在 disabled 列表）→ disable，软禁用。"""
        name = "toggle_disable"
        self._register_stub(name)

        client = self._client()
        resp = client.post(f"/skills/{name}/toggle")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "disabled")
        self.assertEqual(data["skill_name"], name)

        # 验证软禁用：工具仍在 registry 中，schema 标 enabled: False
        self.assertTrue(self.registry.is_skill_disabled(name))
        schemas = self.registry.get_tools_schema()
        entry = next(s for s in schemas if s["name"] == f"skill__{name}")
        self.assertIn("enabled", entry)
        self.assertFalse(entry["enabled"])

    def test_toggle_enable_returns_enabled_and_restores(self):
        """POST /skills/{name}/toggle（已在 disabled 列表）→ enable，恢复启用。"""
        name = "toggle_enable"
        meta = self._register_stub(name)
        # 先软禁用，并写入状态文件
        self.registry.disable_skill(name)
        state_path = self.tmpdir / "state.json"
        state_path.write_text(
            json.dumps({"disabled": [name], "locked": []}),
            encoding="utf-8",
        )
        # 创建磁盘 SKILL.md 供 enable 路径加载
        _write_skill_md(self.tmpdir / name, name)
        # enable 路径会调 skill_loader.load + _parse_meta
        self.skill_loader.load.return_value = Skill(
            name=name, tools=[], system_prompt="", handlers={}
        )
        self.skill_loader._parse_meta.return_value = meta
        # _metas 仍含 meta（reload 不会清空 _metas）
        self.skill_loader._metas[name] = meta

        client = self._client()
        resp = client.post(f"/skills/{name}/toggle")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "enabled")
        self.assertEqual(data["skill_name"], name)

        # 软禁用标记清除
        self.assertFalse(self.registry.is_skill_disabled(name))
        # schema 中 enabled: False 字段被移除（默认启用）
        schemas = self.registry.get_tools_schema()
        entry = next(s for s in schemas if s["name"] == f"skill__{name}")
        self.assertNotEqual(entry.get("enabled"), False)

    def test_toggle_locked_returns_403(self):
        """锁定列表中的 skill 不可切换 → 403。"""
        name = "locked_skill"
        state_path = self.tmpdir / "state.json"
        state_path.write_text(
            json.dumps({"disabled": [], "locked": [name]}),
            encoding="utf-8",
        )
        client = self._client()
        resp = client.post(f"/skills/{name}/toggle")
        self.assertEqual(resp.status_code, 403)

    # ---- DELETE /skills/{name} ------------------------------------------

    def test_delete_skill_removes_directory_and_registry(self):
        """DELETE /skills/{name} 删除磁盘目录与 registry 中的工具。"""
        name = "delete_skill"
        meta = self._register_stub(name)
        # 额外注册一个 deferred 业务工具（skill__{name}__tool1），
        # 验证 DELETE 端点按前缀 skill__{name}__ 卸载
        self.registry.register_deferred(
            name=f"skill__{name}__tool1",
            description="test business tool",
            input_schema={},
            handler=lambda: "",
        )
        # 创建磁盘 SKILL.md
        skill_dir = self.tmpdir / name
        _write_skill_md(skill_dir, name)
        # skill_loader.unload 是 MagicMock（默认无副作用）
        self.skill_loader.unload = MagicMock()

        client = self._client()
        resp = client.delete(f"/skills/{name}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "deleted")
        self.assertEqual(data["skill_name"], name)
        # 磁盘目录已删除
        self.assertFalse(skill_dir.exists())
        # skill__{name}__* 业务工具已从 deferred 层卸载
        self.assertNotIn(f"skill__{name}__tool1", self.registry._deferred_tools)
        # skill_loader.unload 被调用
        self.skill_loader.unload.assert_called_once_with(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
