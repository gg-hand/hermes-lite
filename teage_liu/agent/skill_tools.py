"""Skill 管理工具（5 个 handler + register 函数）。

提供 Skill 模板生成、新增、重新加载、启用/禁用、列表查询 5 个管理工具，
注册到 ToolRegistry 的 Core Tier，供 LLM 在 Agent 会话中使用。

设计要点：
- 通过 closure 捕获 ``registry`` 与 ``skill_loader`` 实例，避免全局变量。
- 状态持久化到 ``data/skills_state.json``，记录禁用/锁定清单。
- 文件写操作使用 ``shutil.rmtree`` 回滚，保证部分写入失败时自动清理。
- 所有 handler 返回 JSON 字符串，与 ToolRegistry 返回值类型一致。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# SkillMeta / Skill 用于 _handle_list_skills 中的 isinstance 类型检查
# （防御 MagicMock 测试场景下 _metas / _skills 返回 MagicMock 而非真实实例）
from teage_liu.skill.loader import SkillMeta, Skill
SKILL_BASE_DIR = Path("skills/")
SKILL_STATE_PATH = Path("data/skills_state.json")

# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


def _load_skill_state() -> Dict[str, Any]:
    """读取技能状态文件 ``data/skills_state.json``。

    返回:
        包含 ``disabled``（已禁用技能名列表）与 ``locked``（锁定技能名列表）
        的 dict。文件不存在或解析失败时返回默认值 ``{"disabled": [], "locked": []}``。
    """
    try:
        with _state_lock:
            if SKILL_STATE_PATH.exists():
                raw = SKILL_STATE_PATH.read_text(encoding="utf-8")
                state = json.loads(raw)
                if isinstance(state, dict):
                    return {
                        "disabled": state.get("disabled", []),
                        "locked": state.get("locked", []),
                    }
    except Exception as e:
        logger.warning("读取 %s 失败: %s", SKILL_STATE_PATH, e)
    return {"disabled": [], "locked": []}


def _save_skill_state(state: Dict[str, Any]) -> None:
    """持久化技能状态到 ``data/skills_state.json``。

    参数:
        state: 含 ``disabled`` 与 ``locked`` 键的 dict。
    """
    try:
        with _state_lock:
            SKILL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SKILL_STATE_PATH.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception as e:
        logger.error("写入 %s 失败: %s", SKILL_STATE_PATH, e)


# ---------------------------------------------------------------------------
# 名称校验
# ---------------------------------------------------------------------------

_PATH_SEP_RE = re.compile(r"[/\\\\]")


def _validate_skill_name(name: str) -> str:
    """校验技能名称是否合法。

    规则：
    - 不能为空。
    - 不能含路径分隔符（``/`` 或 ``\\``）。
    - 不能以点号开头（防止隐藏目录遍历）。
    - 不能以点号结尾（防止 Windows 下末尾点号兼容问题）。

    参数:
        name: 待校验的技能名称。

    返回:
        校验通过返回空字符串 ``""``，失败返回错误描述字符串。
    """
    if not name:
        return "技能名称不能为空"
    if _PATH_SEP_RE.search(name):
        return f"技能名称不能包含路径分隔符: {name}"
    if name.startswith("."):
        return f"技能名称不能以点号开头: {name}"
    if name.endswith("."):
        return f"技能名称不能以点号结尾: {name}"
    return ""


# ---------------------------------------------------------------------------
# 5 个 Handler
# ---------------------------------------------------------------------------


def _handle_skill_template(tool_input: dict) -> str:
    """Handler 1：生成 Skill 模板文件（各字段说明 + scripts/ + SKILL.md）。

    propose_skill 的 schema 已改为结构化字段（description/version/requires/
    skill_body），且新范式以 ``scripts_files`` 路径为准（SKILL.md frontmatter
    + scripts/main.py + 调用示例）。本工具返回各字段用途说明、scripts/main.py
    模板与 SKILL.md 模板。

    参数:
        tool_input: 工具输入（当前未使用，预留）。

    返回:
        含 ``fields``（各字段说明）、``scripts_template``（scripts/main.py
        模板）与 ``skill_md_template``（SKILL.md 模板）的 JSON 字符串。
    """
    fields = {
        "name": "Skill 名称（小写，下划线分隔）",
        "description": "一句话描述 Skill 用途",
        "version": "语义化版本，默认 0.1.0",
        "requires": "依赖的 Python 包名列表",
        "skill_body": "SKILL.md 的 body 部分内容（激活后注入 LLM 上下文）",
    }

    scripts_template = '''#!/usr/bin/env python3
"""Skill scripts 模板 - 通过 skill__resource 读取后由 bash_exec 调用"""
import sys
import json


def my_handler(arg1: str, arg2: int = 0) -> dict:
    """示例 handler：处理输入并返回结果"""
    # TODO: 实现具体逻辑
    return {"status": "ok", "input": arg1, "count": arg2}


def _main(argv):
    """CLI 入口：解析 JSON 参数并调用 handler"""
    if len(argv) < 2:
        print(json.dumps({"error": "missing json argument"}))
        return 1
    args = json.loads(argv[1])
    result = my_handler(**args)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
'''

    skill_md_template = '''---
name: my_skill
description: 一句话描述 Skill 用途
version: 0.1.0
requires:
  - requests
---

# My Skill

本 Skill 用于处理 XX 任务。

## 调用方式

通过 bash_exec 执行：
```bash
python scripts/main.py '{"arg1": "hello", "arg2": 1}'
```

或通过 skill__resource 读取 scripts/main.py 内容后由 LLM 自行调用。
'''

    return json.dumps(
        {
            "fields": fields,
            "scripts_template": scripts_template,
            "skill_md_template": skill_md_template,
        },
        ensure_ascii=False,
    )


def _handle_propose_skill(
    tool_input: dict,
    registry: Any,
    skill_loader: Any,
    orchestrator: Any = None,
) -> str:
    """Handler 2：新增一个本地 Skill 到 ``skills/`` 目录。

    双路径支持：
    - **新路径**（``scripts_files`` 非空）：写入 ``scripts/`` 多文件 +
      ``register_skill_stub`` 注册激活按钮到 Core Tier（对齐 agentskills.io）。
    - **旧路径**（``tools_py`` 非空，``scripts_files`` 为空）：写入 ``tools.py`` +
      ``load_skill_to_registry`` 通过 importlib 注册业务工具到 Deferred Tier。
    - **两者都空**：仅创建 SKILL.md + 注册激活按钮（若 orchestrator 可用）。

    参数:
        tool_input: 含 ``name`` / ``description`` / ``version`` / ``requires`` /
            ``skill_body`` / ``scripts_files`` / ``tools_py`` 字段的 dict。
        registry: ``ToolRegistry`` 实例（通过 closure 捕获）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。
        orchestrator: ``Orchestrator`` 实例（可选，供注册 activate handler）。

    返回:
        操作结果 JSON 字符串。成功时含 ``status="activated"`` 与 ``tools`` 列表。
    """
    name = (tool_input.get("name") or "").strip()
    description = (tool_input.get("description") or "").strip()
    version = (tool_input.get("version") or "").strip() or "1.0.0"
    requires = tool_input.get("requires", []) or []
    skill_body = (tool_input.get("skill_body") or "").strip()
    tools_py_content = (tool_input.get("tools_py") or "").strip()
    scripts_files = tool_input.get("scripts_files", {}) or {}

    # 1. 校验技能名称
    err = _validate_skill_name(name)
    if err:
        return json.dumps({"status": "error", "reason": err}, ensure_ascii=False)

    # 2. 校验 description 非空
    if not description:
        return json.dumps(
            {"status": "error", "reason": "description 不能为空"},
            ensure_ascii=False,
        )

    # 3. 组装 SKILL.md（从结构化字段生成 frontmatter + 正文）
    import yaml

    frontmatter = {
        "name": name,
        "version": version,
        "description": description,
        "requires": requires,
    }
    skill_md_content = (
        "---\n"
        + yaml.dump(frontmatter, allow_unicode=True).strip()
        + "\n---\n"
    )
    if skill_body:
        skill_md_content += "\n" + skill_body + "\n"

    # 4. 检查技能是否已存在
    if skill_loader is not None:
        existing = False
        # 检查内存缓存
        if hasattr(skill_loader, "_metas") and name in skill_loader._metas:
            existing = True
        # 检查磁盘目录
        if not existing:
            base_dir = (
                Path(skill_loader.skill_dir)
                if hasattr(skill_loader, "skill_dir")
                else SKILL_BASE_DIR
            )
            if (base_dir / name).exists():
                existing = True
        if existing:
            return json.dumps(
                {"status": "error", "reason": f"技能 {name!r} 已存在"},
                ensure_ascii=False,
            )

    # 5. 对 tools.py 做语法检查（旧路径）
    if tools_py_content and not scripts_files:
        try:
            compile(tools_py_content, f"<skills/{name}/tools.py>", "exec")
        except SyntaxError as e:
            return json.dumps(
                {"status": "error", "reason": f"tools.py 语法错误: {e}"},
                ensure_ascii=False,
            )

    # 6. 创建目录并写入文件
    target_dir = SKILL_BASE_DIR / name
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return json.dumps(
            {"status": "error", "reason": f"目录 {target_dir} 已存在"},
            ensure_ascii=False,
        )
    except OSError as e:
        return json.dumps(
            {"status": "error", "reason": f"创建目录失败: {e}"},
            ensure_ascii=False,
        )

    rollback = False
    reason = ""
    try:
        (target_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")

        # 新路径：写入 scripts/ 多文件
        if scripts_files:
            scripts_dir = target_dir / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            for rel_path, content in scripts_files.items():
                # 防路径穿越
                if ".." in rel_path or Path(rel_path).is_absolute():
                    rollback = True
                    reason = f"scripts_files 路径非法: {rel_path}"
                    break
                file_path = scripts_dir / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")

        # 旧路径：写入 tools.py（仅当未提供 scripts_files 时）
        elif tools_py_content:
            (target_dir / "tools.py").write_text(tools_py_content, encoding="utf-8")
    except Exception as e:
        rollback = True
        reason = f"写入文件失败: {e}"

    if rollback:
        shutil.rmtree(target_dir, ignore_errors=True)
        return json.dumps(
            {"status": "error", "reason": reason},
            ensure_ascii=False,
        )

    # 7. 注册到 registry（双路径）
    tools_list: List[Dict[str, Any]] = []
    try:
        if skill_loader is not None:
            skill_loader.unload(name)
            # 重新发现以更新 meta 缓存
            skill_file = target_dir / "SKILL.md"
            if hasattr(skill_loader, "_parse_meta"):
                meta = skill_loader._parse_meta(skill_file)
                if meta is not None:
                    skill_loader._metas[name] = meta

                    # 新路径：注册 skill__{name} 激活按钮到 Core Tier
                    if orchestrator is not None and register_skill_stub is not None:
                        try:
                            from ..skill.loader import register_skill_stub as _register_stub
                            handler = _make_skill_activate_handler(
                                skill_loader, orchestrator, name
                            )
                            _register_stub(registry, meta, handler)
                            tools_list.append({
                                "name": f"skill__{name}",
                                "description": f"[Skill] 激活 {name}",
                            })
                        except Exception as e:
                            logger.error("注册 skill stub %s 失败: %s", name, e)

            # 旧路径：通过 importlib 加载 tools.py 并注册业务工具
            if not scripts_files and tools_py_content:
                skill = skill_loader.load(name)
                if skill is not None and skill.tools:
                    from ..skill.loader import load_skill_to_registry

                    load_skill_to_registry(registry, skill)
                    tools_list.extend([
                        {
                            "name": f"skill__{name}__{t['name']}",
                            "description": t.get("description", ""),
                            "input_schema": t.get("input_schema", {}),
                        }
                        for t in skill.tools
                        if t.get("name")
                    ])
    except Exception as e:
        # 注册失败不回滚文件（文件已写入，允许用户修复后重试 reload）
        logger.error("注册技能 %s 失败: %s", name, e)
        return json.dumps(
            {"status": "error", "reason": f"注册失败: {e}"},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "status": "activated",
            "tools": tools_list,
        },
        ensure_ascii=False,
    )


def _handle_reload_skill(
    tool_input: dict,
    registry: Any,
    skill_loader: Any,
    orchestrator: Any = None,
) -> str:
    """Handler 3：重新加载指定 Skill（双路径注册）。

    流程：
    1. 卸载旧工具：``skill__{name}`` 激活按钮（精确名）+ ``skill__{name}__``
       旧业务工具（前缀）。
    2. 清除 skill_loader 缓存后重新加载（``unload`` + ``load``）。
    3. 双路径注册：
       - 新路径（``orchestrator`` 可用）：``register_skill_stub`` 注册激活按钮到 Core Tier。
       - 旧路径（``skill.tools`` 非空）：``load_skill_to_registry`` 注册业务工具到 Deferred Tier。

    参数:
        tool_input: 含 ``name`` 字段的 dict。
        registry: ``ToolRegistry`` 实例（通过 closure 捕获）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。
        orchestrator: ``Orchestrator`` 实例（可选，供注册 activate handler）。

    返回:
        JSON 字符串，含 ``status`` 字段（``"reloaded"`` 或 ``"error"``）+ ``tools`` 列表。
    """
    name = (tool_input.get("name") or "").strip()
    err = _validate_skill_name(name)
    if err:
        return json.dumps({"status": "error", "reason": err}, ensure_ascii=False)

    if skill_loader is None:
        return json.dumps(
            {"status": "error", "reason": "skill_loader 不可用"},
            ensure_ascii=False,
        )

    # 1. 卸载旧工具：激活按钮（精确名）+ 旧业务工具（前缀）
    try:
        registry.unregister(f"skill__{name}")
    except Exception:
        pass  # 未注册时忽略
    _unregister_by_prefix(registry, f"skill__{name}__")

    # 2. 清除缓存并重新加载
    try:
        skill_loader.unload(name)
        skill = skill_loader.load(name)
    except Exception as e:
        return json.dumps(
            {"status": "error", "reason": f"重新加载失败: {e}"},
            ensure_ascii=False,
        )

    if skill is None:
        return json.dumps(
            {
                "status": "error",
                "reason": f"技能 {name!r} 不存在或加载失败",
            },
            ensure_ascii=False,
        )

    # 3. 双路径注册
    tools_registered: List[str] = []

    # 新路径：注册 skill__{name} 激活按钮到 Core Tier
    if orchestrator is not None:
        try:
            from ..skill.loader import register_skill_stub as _register_stub

            meta = (
                skill_loader._metas.get(name)
                if hasattr(skill_loader, "_metas") else None
            )
            if meta is None and hasattr(skill_loader, "_parse_meta"):
                skill_file = SKILL_BASE_DIR / name / "SKILL.md"
                if skill_file.exists():
                    meta = skill_loader._parse_meta(skill_file)
            if meta is not None:
                handler = _make_skill_activate_handler(
                    skill_loader, orchestrator, name
                )
                _register_stub(registry, meta, handler)
                tools_registered.append(f"skill__{name}")
        except Exception as e:
            logger.error("注册 skill stub %s 失败: %s", name, e)

    # 旧路径：若 skill 有 tools，加载业务工具到 Deferred Tier（向后兼容）
    if skill is not None and skill.tools:
        try:
            from ..skill.loader import load_skill_to_registry

            load_skill_to_registry(registry, skill)
            tools_registered.extend(
                f"skill__{name}__{t.get('name', '')}"
                for t in skill.tools if t.get("name")
            )
        except Exception as e:
            logger.error("注册 skill 业务工具 %s 失败: %s", name, e)

    return json.dumps(
        {"status": "reloaded", "tools": tools_registered},
        ensure_ascii=False,
    )


def _handle_toggle_skill(
    tool_input: dict,
    registry: Any,
    skill_loader: Any,
    orchestrator: Any = None,
) -> str:
    """Handler 4：启用/禁用指定 Skill（软禁用语义 + 双路径注册）。

    禁用：通过 ``registry.disable_skill(name)`` 软禁用（schema 标 ``enabled: False``，
    执行抛 ``ToolNotFoundError``），保留工具在 registry 中保持 schema 稳定，
    并记入状态文件 ``disabled`` 列表。
    启用：从状态文件 ``disabled`` 列表移除，调 ``registry.enable_skill(name)``
    清除软禁用标记，并通过 ``skill_loader`` 重新加载走双路径注册
    （新路径 register_skill_stub + 旧路径 load_skill_to_registry）。

    锁定列表中的技能不可切换。

    参数:
        tool_input: 含 ``name`` 与 ``action``（``"enable"`` / ``"disable"``）
            的 dict。
        registry: ``ToolRegistry`` 实例（通过 closure 捕获）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。
        orchestrator: ``Orchestrator`` 实例（可选，供注册 activate handler）。

    返回:
        JSON 字符串，含 ``status`` 字段
        （``"disabled"`` / ``"enabled"`` / ``"error"``）。
    """
    name = (tool_input.get("name") or "").strip()
    action = (tool_input.get("action") or "").strip().lower()

    err = _validate_skill_name(name)
    if err:
        return json.dumps({"status": "error", "reason": err}, ensure_ascii=False)

    if action not in ("enable", "disable"):
        return json.dumps(
            {"status": "error", "reason": "action 必须是 enable 或 disable"},
            ensure_ascii=False,
        )

    # 检查锁定列表
    state = _load_skill_state()
    locked = state.get("locked", [])
    name_lower = name.lower()
    if any(locked_name.lower() == name_lower for locked_name in locked):
        return json.dumps(
            {"status": "error", "reason": f"技能 {name!r} 已锁定，不可切换"},
            ensure_ascii=False,
        )

    if action == "disable":
        # 软禁用：保留工具在 registry 中保持 schema 稳定，
        # schema 标 enabled: False，执行时抛 ToolNotFoundError。
        # 工具不再从 registry 中物理删除。
        registry.disable_skill(name)

        disabled = state.get("disabled", [])
        if name not in disabled:
            disabled.append(name)
        state["disabled"] = disabled
        _save_skill_state(state)
        return json.dumps({"status": "disabled"}, ensure_ascii=False)

    else:  # enable
        # 从禁用清单移除
        disabled = state.get("disabled", [])
        if name in disabled:
            disabled.remove(name)
        state["disabled"] = disabled
        _save_skill_state(state)

        # 清除软禁用标记（使 schema 中 enabled: False 移除）
        registry.enable_skill(name)

        # 重新加载并注册（双路径）
        if skill_loader is not None:
            try:
                skill_loader.unload(name)
                skill = skill_loader.load(name)
                if skill is not None:
                    # 新路径：注册 skill__{name} 激活按钮到 Core Tier
                    if orchestrator is not None:
                        try:
                            from ..skill.loader import register_skill_stub as _register_stub

                            meta = (
                                skill_loader._metas.get(name)
                                if hasattr(skill_loader, "_metas") else None
                            )
                            if meta is None and hasattr(skill_loader, "_parse_meta"):
                                skill_file = SKILL_BASE_DIR / name / "SKILL.md"
                                if skill_file.exists():
                                    meta = skill_loader._parse_meta(skill_file)
                            if meta is not None:
                                handler = _make_skill_activate_handler(
                                    skill_loader, orchestrator, name
                                )
                                _register_stub(registry, meta, handler)
                        except Exception as e:
                            logger.error("启用 skill stub %s 失败: %s", name, e)

                    # 旧路径：注册业务工具到 Deferred Tier
                    if skill.tools:
                        from ..skill.loader import load_skill_to_registry

                        load_skill_to_registry(registry, skill)
            except Exception as e:
                logger.error("启用技能 %s 后重新加载失败: %s", name, e)
                return json.dumps(
                    {"status": "enabled", "warning": f"重新加载失败: {e}"},
                    ensure_ascii=False,
                )

        return json.dumps({"status": "enabled"}, ensure_ascii=False)


def _handle_list_skills(
    tool_input: dict,
    registry: Any,
    skill_loader: Any,
) -> str:
    """Handler 5：列出可用技能或查询单技能详情。

    不传 ``name`` 时返回概览：
    - ``loaded_skills``：skill_loader 缓存中已加载的技能元数据列表。
    - ``on_disk_skills``：本地 ``skills/`` 目录中发现但未加载的技能。
    - ``disabled``：状态文件中的已禁用列表。
    - ``locked``：状态文件中的锁定列表。

    传 ``name`` 时返回该技能的元数据详情（``SkillMeta``）+ 软禁用/stub 状态。
    主信息改为 meta（``name``/``version``/``description``/``body_preview``/
    ``resources``/``disabled``/``stub_registered``），不再调 ``skill_loader.load()``
    读空的 ``tools.py``。若 ``_skills`` 缓存中已有 Skill 实例，则附加 ``tools``
    字段作为补充（向后兼容）。

    参数:
        tool_input: 可含 ``name`` 字段的 dict。
        registry: ``ToolRegistry`` 实例（用于查询软禁用/stub 状态）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。

    返回:
        技能列表或详情的 JSON 字符串。
    """
    skill_name = (tool_input.get("name") or "").strip()
    state = _load_skill_state()

    # 查询单个技能详情
    if skill_name:
        if skill_loader is None:
            return json.dumps(
                {"status": "error", "reason": "skill_loader 不可用"},
                ensure_ascii=False,
            )

        # 主信息：从 _metas 缓存或 discover() 取 SkillMeta
        meta = None
        if hasattr(skill_loader, "_metas"):
            cached = skill_loader._metas.get(skill_name)
            # isinstance 检查防御 MagicMock（测试场景下 _metas 可能不是真实 dict）
            if SkillMeta is not None and isinstance(cached, SkillMeta):
                meta = cached
        if meta is None:
            try:
                for m in skill_loader.discover():
                    if m.name == skill_name:
                        meta = m
                        break
            except Exception:
                pass
        # 退化路径：若 _parse_meta 可用，尝试直接解析磁盘上的 SKILL.md
        if meta is None and hasattr(skill_loader, "_parse_meta"):
            try:
                skill_file = SKILL_BASE_DIR / skill_name / "SKILL.md"
                if skill_file.exists():
                    parsed = skill_loader._parse_meta(skill_file)
                    if SkillMeta is not None and isinstance(parsed, SkillMeta):
                        meta = parsed
            except Exception:
                pass

        # 兼容性：若 _skills 缓存命中 Skill 实例，附带 tools/system_prompt 字段
        cached_skill = None
        if hasattr(skill_loader, "_skills"):
            cached_skill = skill_loader._skills.get(skill_name)
            # 防御 MagicMock：确保是真实 Skill 实例
            if Skill is not None and not isinstance(cached_skill, Skill):
                cached_skill = None

        # meta 与 cached_skill 都为空时返回 error
        if meta is None and cached_skill is None:
            return json.dumps(
                {"status": "error", "reason": f"技能 {skill_name!r} 不存在"},
                ensure_ascii=False,
            )

        # 组装返回结果（主信息来自 meta，cached_skill 仅作补充）
        result: Dict[str, Any] = {
            "name": (meta.name if meta is not None else cached_skill.name),
            "version": (meta.version if meta is not None else "0.1.0"),
            "description": (
                meta.description if meta is not None
                else getattr(cached_skill, "description", "")
            ),
            "body_preview": (meta.body or "")[:200] if meta is not None else "",
            "resources": (meta.resources or []) if meta is not None else [],
            "disabled": (
                registry.is_skill_disabled(skill_name)
                if hasattr(registry, "is_skill_disabled") else
                skill_name in state.get("disabled", [])
            ),
            "stub_registered": (
                bool(registry.get_full_schema(f"skill__{skill_name}"))
                if hasattr(registry, "get_full_schema") else False
            ),
        }

        # 兼容性：若 cached_skill 有 tools/system_prompt，附加到返回中
        if cached_skill is not None:
            tools_list = []
            for t in cached_skill.tools:
                tname = t.get("name", "")
                tools_list.append({
                    "name": f"skill__{cached_skill.name}__{tname}" if tname else "",
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {}),
                })
            result["tools"] = tools_list
            result["system_prompt"] = cached_skill.system_prompt

        return json.dumps(result, ensure_ascii=False)

    # 概览模式
    loaded_list: List[Dict[str, Any]] = []
    on_disk_list: List[Dict[str, Any]] = []

    if skill_loader is not None:
        # 已加载的技能（_skills 缓存中的）
        if hasattr(skill_loader, "_skills"):
            for sname, skill in skill_loader._skills.items():
                loaded_list.append({
                    "name": sname,
                    "description": getattr(skill, "description", ""),
                })

        # 磁盘上发现的技能（通过 discover 扫描）
        try:
            discovered = skill_loader.discover()
            discovered_names = {m.name for m in discovered}
            for meta in discovered:
                on_disk_list.append({
                    "name": meta.name,
                    "description": meta.description,
                    "version": meta.version,
                })
        except Exception:
            pass
    else:
        # 没有 skill_loader，直接扫描目录
        if SKILL_BASE_DIR.exists():
            for child in sorted(SKILL_BASE_DIR.iterdir()):
                if child.is_dir() and (child / "SKILL.md").exists():
                    on_disk_list.append({
                        "name": child.name,
                        "description": "",
                        "version": "0.1.0",
                    })

    return json.dumps(
        {
            "loaded_skills": loaded_list,
            "on_disk_skills": on_disk_list,
            "disabled": state.get("disabled", []),
            "locked": state.get("locked", []),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _unregister_by_prefix(registry: Any, prefix: str) -> None:
    """从 registry 的所有分层中移除名称以 ``prefix`` 开头的工具。

    遍历 ``_core_tools`` / ``_deferred_tools`` / ``_loaded_tools``，
    逐个调用 ``unregister`` 移除匹配项。

    参数:
        registry: ``ToolRegistry`` 实例。
        prefix: 工具名称前缀（如 ``"skill__my_skill__"``）。
    """
    names_to_remove: List[str] = []
    for store_key in ("_core_tools", "_deferred_tools", "_loaded_tools"):
        store = getattr(registry, store_key, {})
        for tname in store:
            if tname.startswith(prefix):
                names_to_remove.append(tname)

    for tname in names_to_remove:
        registry.unregister(tname)


# ---------------------------------------------------------------------------
# Skill 激活 handler + 资源 handler 工厂（P1-2）
# ---------------------------------------------------------------------------


def _make_skill_activate_handler(skill_loader: Any, orchestrator: Any, skill_name: str):
    """为指定 skill 生成激活 handler（closure 捕获 skill_name）。

    LLM 调用 ``skill__{name}()`` 后触发：
    1. 从 ``orchestrator._current_session_id`` 获取当前会话 ID
       （在 ``run``/``run_stream`` 入口已设置）
    2. 调用 ``orchestrator.skill_mgr.activate(skill_name, session_id=...)``
       标记 skill 为已激活（下一轮注入 body 到 messages[0] 末位）
    3. 返回激活成功信息 + body 预览（前 100 字）

    参数:
        skill_loader: ``SkillLoader`` 实例（用于检查存在性 + 加载 body 预览）。
        orchestrator: ``Orchestrator`` 实例（用于 skill_mgr.activate + session_id）。
        skill_name: Skill 名称（closure 捕获，每个 skill 独立 handler）。

    返回:
        handler 函数，签名 ``(**kwargs) -> str``。
    """
    def handler(**kwargs) -> str:
        if not skill_loader.skill_exists(skill_name):
            return f"Skill '{skill_name}' 不存在"
        session_id = getattr(orchestrator, "_current_session_id", None) or "default"
        try:
            orchestrator.skill_mgr.activate(skill_name, session_id=session_id)
        except Exception as e:
            return f"激活 Skill '{skill_name}' 失败: {e}"
        body_preview = skill_loader.load_body(skill_name)[:100]
        return (
            f"Skill '{skill_name}' 已激活，body 将在下一轮注入上下文。"
            f"预览: {body_preview}..."
        )
    return handler


def _make_skill_resource_handler(skill_loader: Any):
    """生成 ``skill__resource`` 工具的 handler（读取 L3 资源）。

    参数:
        skill_loader: ``SkillLoader`` 实例（调用 ``load_resource``）。

    返回:
        handler 函数，签名 ``(**kwargs) -> str``。
    """
    def handler(**kwargs) -> str:
        name = kwargs.get("name", "")
        rel_path = kwargs.get("rel_path", "")
        if not name or not rel_path:
            return "参数 name 和 rel_path 必填"
        # 防路径穿越（load_resource 内部也做了，这里前置检查给出明确错误）
        if ".." in rel_path or Path(rel_path).is_absolute():
            return f"rel_path 不允许包含 .. 或绝对路径: {rel_path}"
        try:
            content = skill_loader.load_resource(name, rel_path)
            return content
        except Exception as e:
            return f"读取 skill 资源失败: {e}"
    return handler


# ---------------------------------------------------------------------------
# 注册函数
# ---------------------------------------------------------------------------


def register_skill_tools(registry: Any, skill_loader: Any, orchestrator: Any = None) -> None:
    """注册 6 个 Skill 管理工具到 ToolRegistry 的 Core Tier。

    所有工具通过 ``register_core()`` 注册，保证始终全量注入
    （字节级稳定，KV cache 100% 命中）。

    工具清单（双下划线命名规范）：
    1. ``skill__template`` — 生成 SKILL.md 与 scripts/ 模板。
    2. ``skill__propose`` — 新增本地 Skill 到 ``skills/`` 目录。
    3. ``skill__reload`` — 重新加载指定 Skill。
    4. ``skill__toggle`` — 启用/禁用指定 Skill。
    5. ``skill__list`` — 列出可用技能或查询详情。
    6. ``skill__resource`` — 读取 Skill 的 scripts/ 资源文件内容。

    参数:
        registry: ``ToolRegistry`` 实例。
        skill_loader: ``SkillLoader`` 实例。
        orchestrator: ``Orchestrator`` 实例（可选，供 propose/reload/toggle
            创建 activate handler 注册 skill stub 到 Core Tier）。为 None 时
            这些 handler 退化为仅走旧 load_skill_to_registry 路径。
    """
    # ------------------------------------------------------------------
    # 1. skill__template
    # ------------------------------------------------------------------
    def _template(**kwargs) -> str:
        """生成 SKILL.md 与 scripts/ 模板内容。"""
        return _handle_skill_template({})

    registry.register_core(
        name="skill__template",
        description=(
            "生成 Skill 模板：返回各结构化字段说明（name/description/"
            "version/requires/skill_body）与 scripts/ 代码模板，供 "
            "skill__propose 工具参考使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_template,
    )

    # ------------------------------------------------------------------
    # 2. skill__propose
    # ------------------------------------------------------------------
    def _propose(**kwargs) -> str:
        """新增一个本地 Skill 到 skills/ 目录并激活。"""
        return _handle_propose_skill(kwargs, registry, skill_loader, orchestrator)

    registry.register_core(
        name="skill__propose",
        description=(
            "新增一个本地 Skill 到 skills/ 目录：传入结构化字段（name/description/version/requires/skill_body），"
            "系统自动组装 SKILL.md。可选传入 scripts_files（新路径，写入 scripts/ 并注册激活按钮）"
            "或 tools_py（旧路径，写入 tools.py 并通过 importlib 注册）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Skill 名称（同时也是 skills/ 下的子目录名，"
                        "不能含路径分隔符，不能以点号开头）。"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Skill 简短描述（SKILL.md frontmatter 的 description 字段，必填）。",
                },
                "version": {
                    "type": "string",
                    "description": "Skill 版本号，语义化版本，默认 1.0.0。",
                    "default": "1.0.0",
                },
                "requires": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "依赖的其他 Python 包名列表，默认 []。",
                    "default": [],
                },
                "skill_body": {
                    "type": "string",
                    "description": (
                        "SKILL.md 正文部分（frontmatter 之后的 markdown 内容，不含 ---）。"
                        "可选，不含时仅生成 frontmatter。"
                    ),
                },
                "scripts_files": {
                    "type": "object",
                    "description": (
                        "scripts/ 下要创建的文件映射 {相对路径: 内容}。"
                        "如 {\"calculator.py\": \"...\"}。提供此字段时走新路径"
                        "（写入 scripts/ + 注册 skill__{name} 激活按钮到 Core Tier）。"
                    ),
                    "default": {},
                },
                "tools_py": {
                    "type": "string",
                    "description": (
                        "tools.py 完整内容（旧路径，Python 源码，定义 TOOLS 列表与 "
                        "handler 函数）。可选，提供 scripts_files 时此字段忽略。"
                    ),
                    "default": "",
                },
            },
            "required": ["name", "description"],
        },
        handler=_propose,
    )

    # ------------------------------------------------------------------
    # 3. skill__reload
    # ------------------------------------------------------------------
    def _reload(**kwargs) -> str:
        """重新加载指定 Skill。"""
        return _handle_reload_skill(kwargs, registry, skill_loader, orchestrator)

    registry.register_core(
        name="skill__reload",
        description=(
            "重新加载指定 Skill：从 registry 卸载旧工具（skill__{name} 激活按钮 + "
            "skill__{name}__ 旧业务工具），清除缓存后重新读取 skills/ 目录下的文件。"
            "若 orchestrator 可用，注册 skill__{name} 激活按钮到 Core Tier；"
            "若 skill 有 tools.py，同时走旧路径注册业务工具。用于代码修改后热更新。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要重新加载的 Skill 名称。",
                },
            },
            "required": ["name"],
        },
        handler=_reload,
    )

    # ------------------------------------------------------------------
    # 4. skill__toggle
    # ------------------------------------------------------------------
    def _toggle(**kwargs) -> str:
        """启用或禁用指定 Skill。"""
        return _handle_toggle_skill(kwargs, registry, skill_loader, orchestrator)

    registry.register_core(
        name="skill__toggle",
        description=(
            "启用或禁用指定 Skill。禁用时从注册中心移除所有工具（skill__{name} + "
            "skill__{name}__）并记入禁用清单；启用时从禁用清单移除并重新加载注册。"
            "锁定列表中的技能不可操作。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要切换的 Skill 名称。",
                },
                "action": {
                    "type": "string",
                    "enum": ["enable", "disable"],
                    "description": "操作类型：enable（启用）或 disable（禁用）。",
                },
            },
            "required": ["name", "action"],
        },
        handler=_toggle,
    )

    # ------------------------------------------------------------------
    # 5. skill__list
    # ------------------------------------------------------------------
    def _list(**kwargs) -> str:
        """列出可用技能或查询单技能详情。"""
        return _handle_list_skills(kwargs, registry, skill_loader)

    registry.register_core(
        name="skill__list",
        description=(
            "列出可用技能或查询单技能详情。不含 name 参数时返回概览"
            "（已加载技能、磁盘上发现的技能、禁用列表、锁定列表）；"
            "含 name 参数时返回该技能的详细工具列表。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "要查询详情的 Skill 名称（可选，不含时返回概览）。"
                    ),
                    "default": "",
                },
            },
            "required": [],
        },
        handler=_list,
    )

    # ------------------------------------------------------------------
    # 6. skill__resource — 读取 Skill 的 scripts/ 资源文件
    # ------------------------------------------------------------------
    def _resource(**kwargs) -> str:
        """读取 Skill 的 scripts/ 资源文件内容。"""
        return _make_skill_resource_handler(skill_loader)(**kwargs)

    registry.register_core(
        name="skill__resource",
        description=(
            "读取 Skill 的 scripts/ 资源文件内容。"
            "参数 name(skill 名) + rel_path(scripts/ 下相对路径，"
            "不允许含 .. 或绝对路径)。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill 名称"},
                "rel_path": {
                    "type": "string",
                    "description": "scripts/ 下相对路径（如 calculator.py）",
                },
            },
            "required": ["name", "rel_path"],
        },
        handler=_resource,
    )
