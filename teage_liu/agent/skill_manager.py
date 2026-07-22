"""技能管理:激活/停用/构建技能上下文。

从 Orchestrator 提取的技能管理职责:
- activate: 标记 Skill 为已激活（去重，保留首次激活顺序）
- deactivate: 取消激活指定 Skill
- build_active_section: 构建已激活 Skill body 段（注入 injection_text 末位）
- per-session 隔离: 不同会话的激活状态独立
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SkillManager:
    """管理技能激活状态（per-session 隔离）。

    使用 List[str] 存储激活顺序（而非 Set），保留首次激活顺序。
    """

    def __init__(self, skill_loader: Optional[Any] = None) -> None:
        self._active_skills: Dict[str, List[str]] = {}
        self._skill_loader = skill_loader

    def activate(self, skill_name: str, session_id: str = "default") -> None:
        """标记 Skill 为已激活（下一轮注入 body 到 messages[0] 末位）。

        重复激活同一 Skill 不重复追加（去重），但保留首次激活顺序。
        """
        active = self._active_skills.setdefault(session_id, [])
        if skill_name not in active:
            active.append(skill_name)
            logger.info("Skill 已激活: %s (session=%s)", skill_name, session_id)

    def deactivate(self, skill_name: str, session_id: str = "default") -> None:
        """取消激活指定 Skill。"""
        active = self._active_skills.get(session_id, [])
        if skill_name in active:
            active.remove(skill_name)
            logger.info("Skill 已取消激活: %s (session=%s)", skill_name, session_id)

    def get_active_skills(self, session_id: str = "default") -> List[str]:
        """返回指定会话的激活技能列表（副本）。"""
        return list(self._active_skills.get(session_id, []))

    def build_active_section(self, session_id: str) -> str:
        """构建已激活 Skill body 段（注入 injection_text 末位）。

        返回拼接好的 skill body 段字符串。无激活 Skill 或 skill_loader
        缺失时返回空串。
        """
        active = self._active_skills.get(session_id, [])
        if not active:
            return ""
        if self._skill_loader is None:
            return ""
        sections = []
        for name in active:
            try:
                body = self._skill_loader.load_body(name)
                if body:
                    sections.append(f"## 已激活 Skill: {name}\n{body}")
                else:
                    sections.append(f"## 已激活 Skill: {name}\n(body 为空)")
            except Exception as e:
                logger.warning("加载 Skill %s body 失败: %s", name, e)
        return "\n\n".join(sections)

    def clear_session(self, session_id: str) -> None:
        """清除指定会话的所有激活状态。"""
        self._active_skills.pop(session_id, None)
