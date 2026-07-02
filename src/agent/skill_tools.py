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

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

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
    """Handler 1：生成 Skill 模板文件（各字段说明 + tools.py）。

    propose_skill 的 schema 已改为结构化字段（description/version/requires/
    skill_body），LLM 不再需要自己组装完整的 SKILL.md。本工具返回各字段
    用途说明和 tools.py 模板。

    参数:
        tool_input: 工具输入（当前未使用，预留）。

    返回:
        含 ``fields``（各字段说明）与 ``tools_py``（模板）的 JSON 字符串。
    """
    fields = {
        "name": "my_skill",
        "description": "A sample skill — 简短描述，必填",
        "version": "0.1.0 — 语义化版本，默认 1.0.0",
        "requires": "[] — 依赖的其他 Skill 名称列表",
        "skill_body": "# my_skill\n\nDescribe what this skill does.\n  — 正文 markdown，可选",
    }

    tools_py_template = (
        '"""Skill tools for my_skill."""\n'
        "\n"
        "from typing import Any, Dict\n"
        "\n"
        "TOOLS = [\n"
        "    {\n"
        '        "name": "my_tool",\n'
        '        "description": "Tool description",\n'
        '        "handler": "my_handler",\n'
        '        "input_schema": {\n'
        '            "type": "object",\n'
        '            "properties": {\n'
        '                "param": {\n'
        '                    "type": "string",\n'
        '                    "description": "Parameter description",\n'
        "                },\n"
        "            },\n"
        '            "required": ["param"],\n'
        "        },\n"
        "    },\n"
        "]\n"
        "\n"
        "\n"
        'def my_handler(param: str) -> str:\n'
        '    """Handle my_tool."""\n'
        "    return f\"Hello, {param}!\"\n"
    )

    return json.dumps(
        {"fields": fields, "tools_py": tools_py_template},
        ensure_ascii=False,
    )


def _handle_propose_skill(
    tool_input: dict,
    registry: Any,
    skill_loader: Any,
) -> str:
    """Handler 2：新增一个本地 Skill 到 ``skills/`` 目录。

    流程：
    1. 校验技能名称、frontmatter YAML 解析、名称一致性。
    2. 检查技能是否已存在。
    3. 对 ``tools.py`` 做语法检查（``compile``）。
    4. 创建目录并写入文件；失败时 ``shutil.rmtree`` 回滚。
    5. 调用 ``skill_loader.unload`` + ``skill_loader.load`` 重新发现，
       然后 ``load_skill_to_registry`` 注册到 Deferred Tier。

    参数:
        tool_input: 含 ``name`` / ``description`` / ``version`` / ``requires`` /
            ``skill_body`` / ``tools_py`` 字段的 dict。
        registry: ``ToolRegistry`` 实例（通过 closure 捕获）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。

    返回:
        操作结果 JSON 字符串。成功时含 ``status="activated"`` 与 ``tools`` 列表。
    """
    name = (tool_input.get("name") or "").strip()
    description = (tool_input.get("description") or "").strip()
    version = (tool_input.get("version") or "").strip() or "1.0.0"
    requires = tool_input.get("requires", []) or []
    skill_body = (tool_input.get("skill_body") or "").strip()
    tools_py_content = (tool_input.get("tools_py") or "").strip()

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

    # 5. 对 tools.py 做语法检查
    if tools_py_content:
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
        if tools_py_content:
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

    # 7. 重新加载并注册到 registry
    tools_list: List[Dict[str, Any]] = []
    try:
        if skill_loader is not None:
            skill_loader.unload(name)
            skill = skill_loader.load(name)
            if skill is not None:
                from ..skill.loader import load_skill_to_registry

                load_skill_to_registry(registry, skill)
                tools_list = [
                    {
                        "name": f"skill__{name}__{t['name']}",
                        "description": t.get("description", ""),
                        "input_schema": t.get("input_schema", {}),
                    }
                    for t in skill.tools
                    if t.get("name")
                ]
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
) -> str:
    """Handler 3：重新加载指定 Skill 到 Deferred Tier。

    流程：
    1. 从 registry 卸载该 Skill 所有工具（前缀 ``skill__{name}__``）。
    2. 清除 skill_loader 缓存后重新加载（``unload`` + ``load``）。
    3. 注册到 registry Deferred Tier。

    参数:
        tool_input: 含 ``name`` 字段的 dict。
        registry: ``ToolRegistry`` 实例（通过 closure 捕获）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。

    返回:
        JSON 字符串，含 ``status`` 字段（``"reloaded"`` 或 ``"error"``）。
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

    # 1. 卸载旧工具
    prefix = f"skill__{name}__"
    _unregister_by_prefix(registry, prefix)

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

    # 3. 注册到 Deferred Tier
    try:
        from ..skill.loader import load_skill_to_registry

        load_skill_to_registry(registry, skill)
    except Exception as e:
        logger.error("注册技能 %s 失败: %s", name, e)
        return json.dumps(
            {"status": "error", "reason": f"注册失败: {e}"},
            ensure_ascii=False,
        )

    return json.dumps({"status": "reloaded"}, ensure_ascii=False)


def _handle_toggle_skill(
    tool_input: dict,
    registry: Any,
    skill_loader: Any,
) -> str:
    """Handler 4：启用/禁用指定 Skill。

    禁用：从 registry Deferred Tier 移除该 Skill 所有工具，并记入状态文件
    ``disabled`` 列表。
    启用：从状态文件 ``disabled`` 列表移除，通过 ``skill_loader`` 重新加载
    并注册到 registry。

    锁定列表中的技能不可切换。

    参数:
        tool_input: 含 ``name`` 与 ``action``（``"enable"`` / ``"disable"``）
            的 dict。
        registry: ``ToolRegistry`` 实例（通过 closure 捕获）。
        skill_loader: ``SkillLoader`` 实例（通过 closure 捕获）。

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
        # 禁用：从 registry 移除工具 + 记入禁用清单
        prefix = f"skill__{name}__"
        _unregister_by_prefix(registry, prefix)

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

        # 重新加载并注册（通过 skill_loader）
        if skill_loader is not None:
            try:
                skill_loader.unload(name)
                skill = skill_loader.load(name)
                if skill is not None:
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

    传 ``name`` 时返回该技能的详细工具列表。

    参数:
        tool_input: 可含 ``name`` 字段的 dict。
        registry: ``ToolRegistry`` 实例（忽略，预留）。
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
        skill = None
        if hasattr(skill_loader, "_skills"):
            skill = skill_loader._skills.get(skill_name)
        if skill is None:
            try:
                skill = skill_loader.load(skill_name)
            except Exception:
                pass
        if skill is None:
            return json.dumps(
                {"status": "error", "reason": f"技能 {skill_name!r} 不存在"},
                ensure_ascii=False,
            )

        tools_list = []
        for t in skill.tools:
            tname = t.get("name", "")
            tools_list.append({
                "name": f"skill__{skill.name}__{tname}" if tname else "",
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {}),
            })

        return json.dumps(
            {
                "name": skill.name,
                "description": getattr(skill, "description", ""),
                "system_prompt": skill.system_prompt,
                "tools": tools_list,
            },
            ensure_ascii=False,
        )

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
# 注册函数
# ---------------------------------------------------------------------------


def register_skill_tools(registry: Any, skill_loader: Any) -> None:
    """注册 5 个 Skill 管理工具到 ToolRegistry 的 Core Tier。

    所有工具通过 ``register_core()`` 注册，保证始终全量注入
    （字节级稳定，KV cache 100% 命中）。

    工具清单：
    1. ``skill_template`` — 生成 SKILL.md 与 tools.py 模板。
    2. ``propose_skill`` — 新增本地 Skill 到 ``skills/`` 目录。
    3. ``reload_skill`` — 重新加载指定 Skill。
    4. ``toggle_skill`` — 启用/禁用指定 Skill。
    5. ``list_skills`` — 列出可用技能或查询详情。

    参数:
        registry: ``ToolRegistry`` 实例。
        skill_loader: ``SkillLoader`` 实例。
    """
    # ------------------------------------------------------------------
    # 1. skill_template
    # ------------------------------------------------------------------
    def _template(**kwargs) -> str:
        """生成 SKILL.md 与 tools.py 模板内容。"""
        return _handle_skill_template({})

    registry.register_core(
        name="skill_template",
        description=(
            "生成 Skill 模板：返回各结构化字段说明（name/description/"
            "version/requires/skill_body）与 tools.py 代码模板，供 "
            "propose_skill 工具参考使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_template,
    )

    # ------------------------------------------------------------------
    # 2. propose_skill
    # ------------------------------------------------------------------
    def _propose(**kwargs) -> str:
        """新增一个本地 Skill 到 skills/ 目录并激活。"""
        return _handle_propose_skill(kwargs, registry, skill_loader)

    registry.register_core(
        name="skill_propose",
        description=(
            "新增一个本地 Skill 到 skills/ 目录：传入结构化字段（name/description/version/requires/skill_body），系统自动组装 SKILL.md；可选传入 tools_py，语法检查通过后注册到 Deferred Tier 并激活。"
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
                    "description": "依赖的其他 Skill 名称列表，默认 []。",
                    "default": [],
                },
                "skill_body": {
                    "type": "string",
                    "description": (
                        "SKILL.md 正文部分（frontmatter 之后的 markdown 内容，不含 ---）。"
                        "可选，不含时仅生成 frontmatter。"
                    ),
                },
                "tools_py": {
                    "type": "string",
                    "description": (
                        "tools.py 完整内容（Python 源码，定义 TOOLS 列表与 "
                        "handler 函数）。可选，不含时仅创建 SKILL.md。"
                    ),
                    "default": "",
                },
            },
            "required": ["name", "description"],
        },
        handler=_propose,
    )

    # ------------------------------------------------------------------
    # 3. reload_skill
    # ------------------------------------------------------------------
    def _reload(**kwargs) -> str:
        """重新加载指定 Skill。"""
        return _handle_reload_skill(kwargs, registry, skill_loader)

    registry.register_core(
        name="skill_reload",
        description=(
            "重新加载指定 Skill：从 registry 卸载旧工具，清除缓存后重新读取"
            " skills/ 目录下的文件并注册到 Deferred Tier。用于代码修改后热更新。"
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
    # 4. toggle_skill
    # ------------------------------------------------------------------
    def _toggle(**kwargs) -> str:
        """启用或禁用指定 Skill。"""
        return _handle_toggle_skill(kwargs, registry, skill_loader)

    registry.register_core(
        name="skill_toggle",
        description=(
            "启用或禁用指定 Skill。禁用时从注册中心移除所有工具并记入禁用清单；"
            "启用时从禁用清单移除并重新加载注册。锁定列表中的技能不可操作。"
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
    # 5. list_skills
    # ------------------------------------------------------------------
    def _list(**kwargs) -> str:
        """列出可用技能或查询单技能详情。"""
        return _handle_list_skills(kwargs, registry, skill_loader)

    registry.register_core(
        name="skill_list",
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
