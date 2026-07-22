"""SkillManager 测试:技能激活/停用/构建上下文。

从 Orchestrator 提取的技能管理职责:
- activate: 标记 Skill 为已激活（去重，保留首次激活顺序）
- deactivate: 取消激活指定 Skill
- build_active_section: 构建已激活 Skill body 段（注入 injection_text 末位）
- per-session 隔离: 不同会话的激活状态独立
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teage_liu"))

import pytest
from unittest.mock import MagicMock
from teage_liu.agent.skill_manager import SkillManager


class TestSkillManagerBasics:
    def test_activate_skill(self):
        mgr = SkillManager()
        mgr.activate("test-skill", "session-1")
        assert "test-skill" in mgr.get_active_skills("session-1")

    def test_deactivate_skill(self):
        mgr = SkillManager()
        mgr.activate("test-skill", "session-1")
        mgr.deactivate("test-skill", "session-1")
        assert "test-skill" not in mgr.get_active_skills("session-1")

    def test_isolation_between_sessions(self):
        mgr = SkillManager()
        mgr.activate("test-skill", "session-1")
        assert "test-skill" not in mgr.get_active_skills("session-2")

    def test_activate_dedup_preserves_order(self):
        """重复激活同一 Skill 不重复追加，保留首次激活顺序。"""
        mgr = SkillManager()
        mgr.activate("skill-a", "s1")
        mgr.activate("skill-b", "s1")
        mgr.activate("skill-a", "s1")  # 重复
        active = mgr.get_active_skills("s1")
        assert active == ["skill-a", "skill-b"]

    def test_deactivate_nonexistent_no_error(self):
        """停用未激活的 Skill 不抛异常。"""
        mgr = SkillManager()
        mgr.deactivate("nonexistent", "s1")  # 不应抛异常

    def test_default_session_id(self):
        """未指定 session_id 时使用 default。"""
        mgr = SkillManager()
        mgr.activate("test-skill")
        assert "test-skill" in mgr.get_active_skills()


class TestBuildActiveSection:
    def test_empty_active_returns_empty(self):
        """无激活 Skill 返回空串。"""
        mgr = SkillManager()
        assert mgr.build_active_section("s1") == ""

    def test_no_skill_loader_returns_empty(self):
        """skill_loader 为 None 时返回空串。"""
        mgr = SkillManager(skill_loader=None)
        mgr.activate("test-skill", "s1")
        assert mgr.build_active_section("s1") == ""

    def test_build_section_with_loader(self):
        """有 skill_loader 时构建 skill body 段。"""
        loader = MagicMock()
        loader.load_body.return_value = "skill body content"
        mgr = SkillManager(skill_loader=loader)
        mgr.activate("test-skill", "s1")
        section = mgr.build_active_section("s1")
        assert "## 已激活 Skill: test-skill" in section
        assert "skill body content" in section

    def test_build_section_loader_error(self):
        """skill_loader 抛异常时跳过该 Skill，记录 warning。"""
        loader = MagicMock()
        loader.load_body.side_effect = RuntimeError("load failed")
        mgr = SkillManager(skill_loader=loader)
        mgr.activate("bad-skill", "s1")
        # 不应抛异常
        section = mgr.build_active_section("s1")
        assert section == ""

    def test_build_section_empty_body(self):
        """skill body 为空时标注 (body 为空)。"""
        loader = MagicMock()
        loader.load_body.return_value = ""
        mgr = SkillManager(skill_loader=loader)
        mgr.activate("empty-skill", "s1")
        section = mgr.build_active_section("s1")
        assert "(body 为空)" in section

    def test_clear_session(self):
        """clear_session 清除指定会话的所有激活状态。"""
        mgr = SkillManager()
        mgr.activate("skill-a", "s1")
        mgr.activate("skill-b", "s1")
        mgr.clear_session("s1")
        assert mgr.get_active_skills("s1") == []
