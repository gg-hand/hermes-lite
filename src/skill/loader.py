"""Hermes Skill 加载器实现。

扫描 ``skills/`` 目录下的子目录，解析 ``SKILL.md`` YAML frontmatter 作为
元数据（轻量发现），按需通过 ``importlib`` 动态加载 ``tools.py`` 与
``prompt.py``，并将 Skill 工具注册到 ToolRegistry 的 Deferred Tier。

设计要点：
- discover() 仅扫描元数据，不 import Python 代码（轻量发现）。
- load() 缓存 Skill 实例，重复调用不重复 import。
- 依赖缺失只 warning，不阻断加载（handler 在调用时再报错）。
- 注册到 registry 时工具名加前缀 ``skill__{skill_name}__{tool_name}``。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SkillMeta:
    """Skill 元数据（仅来自 SKILL.md frontmatter）。

    Attributes:
        name: Skill 名称（同时是 ``skills/`` 子目录名）。
        version: Skill 版本，默认 ``0.1.0``。
        description: Skill 描述。
        requires: 该 Skill 依赖的 Python 包名列表（用于依赖检查）。
        body: SKILL.md 正文（frontmatter 之后的 markdown）。L2 body 注入用。
        resources: ``scripts/`` 子目录下的相对路径列表（L3 资源加载用）。
    """

    name: str
    version: str
    description: str
    requires: List[str]
    body: str = ""
    resources: List[str] = field(default_factory=list)


@dataclass
class Skill:
    """已加载的 Skill 实例。

    Attributes:
        name: Skill 名称。
        tools: 工具定义列表（每个元素为含 ``name``/``description``/
            ``input_schema``/``handler`` 字段的 dict，对应 ``tools.py`` 中
            ``TOOLS`` 列表的元素）。
        system_prompt: 该 Skill 的 system prompt（来自 ``prompt.py`` 的
            ``SYSTEM_PROMPT``，无则空串）。
        handlers: 工具名 -> handler 函数 的映射。
    """

    name: str
    tools: List[Dict[str, Any]]
    system_prompt: str
    handlers: Dict[str, Callable]


class SkillLoader:
    """Skill 加载器，负责发现与动态加载本地 Skill 插件。

    Attributes:
        skill_dir: Skill 根目录（默认 ``skills/``）。
    """

    def __init__(self, skill_dir: Path = Path("skills/")) -> None:
        """初始化 SkillLoader。

        参数:
            skill_dir: Skill 根目录，其下每个子目录为一个 Skill。
        """
        self.skill_dir = Path(skill_dir)
        # 缓存：name -> Skill
        self._skills: Dict[str, Skill] = {}
        # 缓存：name -> SkillMeta
        self._metas: Dict[str, SkillMeta] = {}

    def discover(self) -> List[SkillMeta]:
        """扫描 ``skill_dir`` 下所有子目录的 ``SKILL.md``，返回元数据列表。

        - ``skill_dir`` 不存在时返回 ``[]``。
        - 仅解析 frontmatter，**不** import 任何 Python 代码。
        - 解析成功的元数据存入 ``_metas`` 缓存。

        返回:
            发现的 ``SkillMeta`` 列表。
        """
        if not self.skill_dir.exists():
            return []

        metas: List[SkillMeta] = []
        for child in self.skill_dir.iterdir():
            if not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if not skill_file.exists():
                continue
            meta = self._parse_meta(skill_file)
            if meta is None:
                continue
            self._metas[meta.name] = meta
            metas.append(meta)
        return metas

    def _parse_meta(self, skill_file: Path) -> Optional[SkillMeta]:
        """解析 ``SKILL.md`` 的 YAML frontmatter。

        - 文件不以 ``---`` 开头时返回 None。
        - YAML 解析失败时返回 None（不抛异常）。
        - 解析成功后同时提取 body（frontmatter 之后的 markdown 正文）与
          resources（扫描 ``scripts/`` 子目录的文件列表）。

        参数:
            skill_file: ``SKILL.md`` 的路径。

        返回:
            解析得到的 ``SkillMeta``，或 None。
        """
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("读取 SKILL.md 失败 %s: %s", skill_file, e)
            return None

        if not content.startswith("---"):
            return None

        # split("---", 2): ["", frontmatter, body...]
        parts = content.split("---", 2)
        if len(parts) < 2:
            return None
        frontmatter = parts[1]
        body = parts[2].strip() if len(parts) >= 3 else ""

        try:
            data = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as e:
            logger.warning("解析 SKILL.md frontmatter 失败 %s: %s", skill_file, e)
            return None

        if not isinstance(data, dict):
            return None

        # 扫描 scripts/ 子目录的文件列表（L3 资源）
        skill_dir = skill_file.parent
        resources = self._scan_resources(skill_dir)

        return SkillMeta(
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            requires=data.get("requires", []) or [],
            body=body,
            resources=resources,
        )

    @staticmethod
    def _scan_resources(skill_dir: Path) -> List[str]:
        """扫描 ``skill_dir/scripts/`` 子目录下的文件相对路径列表。

        递归扫描一层（仅 scripts/ 直接子文件），不深入子目录。
        目录不存在时返回空列表。

        参数:
            skill_dir: Skill 根目录路径。

        返回:
            scripts/ 下文件的相对路径列表（如 ``["scripts/calculator.py"]``）。
        """
        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.exists() or not scripts_dir.is_dir():
            return []
        resources: List[str] = []
        try:
            for child in sorted(scripts_dir.iterdir()):
                if child.is_file():
                    resources.append(f"scripts/{child.name}")
        except OSError as e:
            logger.warning("扫描 scripts/ 失败 %s: %s", scripts_dir, e)
        return resources

    def load(self, skill_name: str) -> Optional[Skill]:
        """按需动态加载 Skill 代码。

        .. deprecated::
            对齐 agentskills.io 标准后，Skill 不再通过 importlib 加载可执行
            代码。新主流程改用 :meth:`load_body` / :meth:`load_resource` /
            :func:`register_skill_stub`。本方法保留仅为向后兼容（仍可工作）。

        - 缓存命中直接返回。
        - 依赖缺失只 warning，不抛异常。
        - ``tools.py`` 不存在时返回空 Skill。
        - ``prompt.py`` 可选，存在则提取 ``SYSTEM_PROMPT``。

        参数:
            skill_name: Skill 名称（``skills/`` 下子目录名）。

        返回:
            加载得到的 ``Skill`` 实例；目录或 ``SKILL.md`` 不存在返回 None。
        """
        # 缓存命中
        if skill_name in self._skills:
            return self._skills[skill_name]

        skill_dir = self.skill_dir / skill_name
        if not skill_dir.exists():
            return None

        # 获取 meta（先从缓存，没有则读 SKILL.md）
        meta = self._metas.get(skill_name)
        if meta is None:
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                return None
            meta = self._parse_meta(skill_file)
            if meta is None:
                return None
            self._metas[skill_name] = meta

        # 依赖检查：缺失只 warning，不阻断
        for dep in meta.requires:
            try:
                importlib.import_module(dep)
            except ImportError:
                logger.warning("Skill %s 缺少依赖: %s", skill_name, dep)

        # 动态加载 tools.py
        tools_file = skill_dir / "tools.py"
        tools: List[Dict[str, Any]] = []
        handlers: Dict[str, Callable] = {}
        if not tools_file.exists():
            # 无 tools.py，返回空 Skill
            skill = Skill(
                name=skill_name,
                tools=tools,
                system_prompt="",
                handlers=handlers,
            )
            self._skills[skill_name] = skill
            return skill

        module = self._load_module(f"skills.{skill_name}", tools_file)
        if module is not None:
            tools = list(getattr(module, "TOOLS", []) or [])
            handlers = {
                tool["name"]: getattr(module, tool["handler"])
                for tool in tools
                if tool.get("name") and tool.get("handler")
            }

        # 可选加载 prompt.py 提取 SYSTEM_PROMPT
        system_prompt = ""
        prompt_file = skill_dir / "prompt.py"
        if prompt_file.exists():
            pm = self._load_module(f"skills.{skill_name}.prompt", prompt_file)
            if pm is not None:
                system_prompt = getattr(pm, "SYSTEM_PROMPT", "") or ""

        skill = Skill(
            name=skill_name,
            tools=tools,
            system_prompt=system_prompt,
            handlers=handlers,
        )
        self._skills[skill_name] = skill
        return skill

    @staticmethod
    def _load_module(module_name: str, file_path: Path):
        """通过 ``importlib.util`` 动态加载 Python 文件为模块。

        参数:
            module_name: 模块全名（如 ``skills.calculator``）。
            file_path: Python 文件路径。

        返回:
            加载后的模块对象；加载失败返回 None（不抛异常）。
        """
        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            logger.error("动态加载模块 %s 失败: %s", file_path, e)
            return None

    def unload(self, skill_name: str) -> None:
        """卸载 Skill（仅从缓存移除，不卸载已 import 的模块）。

        Python 限制：已 ``importlib`` 加载的模块无法真正卸载，本方法仅清除
        ``_skills`` 缓存，下次 ``load`` 会重新执行 ``tools.py``。

        参数:
            skill_name: 要卸载的 Skill 名称。
        """
        if skill_name in self._skills:
            del self._skills[skill_name]

    def reload(self, skill_name: str) -> Optional[Skill]:
        """重新加载 Skill。

        - 清除 ``_skills`` 与 ``_metas`` 缓存条目。
        - 移除 ``sys.modules`` 中所有以 ``skills.{skill_name}`` 开头的模块。
        - 删除对应 ``__pycache__`` 目录，使 Python 从源码重新编译。
        - 调用 ``self.load(skill_name)`` 重新加载并返回结果。

        参数:
            skill_name: 要重新加载的 Skill 名称。

        返回:
            重新加载后的 ``Skill`` 实例，失败返回 None。
        """
        if skill_name in self._skills:
            del self._skills[skill_name]
        if skill_name in self._metas:
            del self._metas[skill_name]
        for mod_name in list(sys.modules.keys()):
            if mod_name == f"skills.{skill_name}" or mod_name.startswith(f"skills.{skill_name}."):
                del sys.modules[mod_name]
        # 清除 bytecode 缓存，防止 SourceFileLoader 加载过时的 .pyc
        pycache = self.skill_dir / skill_name / "__pycache__"
        if pycache.exists():
            import shutil
            shutil.rmtree(pycache)
        importlib.invalidate_caches()
        logger.info("重新加载 Skill: %s", skill_name)
        return self.load(skill_name)

    def list_loaded(self) -> List[str]:
        """返回当前已加载的 Skill 名称列表。

        返回:
            已加载的 Skill 名称列表。
        """
        return list(self._skills.keys())

    def skill_exists(self, skill_name: str) -> bool:
        """检查 Skill 是否已加载或存在于磁盘上。

        参数:
            skill_name: Skill 名称。

        返回:
            若已加载或 ``SKILL.md`` 文件存在则返回 True。
        """
        if skill_name in self._skills:
            return True
        return (self.skill_dir / skill_name / "SKILL.md").exists()

    def load_body(self, skill_name: str) -> str:
        """返回 SKILL.md 正文（frontmatter 之后的 markdown）。

        缓存命中（``_metas`` 中已含 body）直接返回；否则读取并解析
        ``skills/{name}/SKILL.md``。文件不存在或解析失败返回空串。

        参数:
            skill_name: Skill 名称。

        返回:
            SKILL.md 正文 markdown 字符串。
        """
        meta = self._metas.get(skill_name)
        if meta is not None and meta.body:
            return meta.body
        # 缓存未命中，重新解析
        skill_file = self.skill_dir / skill_name / "SKILL.md"
        if not skill_file.exists():
            return ""
        meta = self._parse_meta(skill_file)
        if meta is None:
            return ""
        self._metas[skill_name] = meta
        return meta.body

    def load_resource(self, skill_name: str, rel_path: str) -> str:
        """读取 ``skills/{name}/scripts/{rel_path}`` 文件内容。

        路径穿越防护：``rel_path`` 不允许包含 ``..`` 或绝对路径。

        参数:
            skill_name: Skill 名称。
            rel_path: scripts/ 下的相对路径（如 ``"scripts/calculator.py"``）。

        返回:
            文件内容字符串。文件不存在或路径非法返回错误信息字符串。

        异常:
            无（路径非法或读取失败返回错误字符串，不抛异常）。
        """
        # 路径穿越防护
        if ".." in rel_path or Path(rel_path).is_absolute():
            return f"rel_path 不允许包含 .. 或绝对路径: {rel_path}"

        # 兼容两种传参：rel_path 含 "scripts/" 前缀 或 不含
        if rel_path.startswith("scripts/"):
            full_path = self.skill_dir / skill_name / rel_path
        else:
            full_path = self.skill_dir / skill_name / "scripts" / rel_path

        if not full_path.exists():
            return f"资源文件不存在: {rel_path}"
        try:
            return full_path.read_text(encoding="utf-8")
        except OSError as e:
            return f"读取资源文件失败: {e}"

    def list_resources(self, skill_name: str) -> List[str]:
        """返回该 Skill 可用的资源相对路径列表（``scripts/`` 下）。

        缓存命中（``_metas`` 中已含 resources）直接返回；否则扫描
        ``skills/{name}/scripts/`` 目录。

        参数:
            skill_name: Skill 名称。

        返回:
            scripts/ 下文件的相对路径列表（如 ``["scripts/calculator.py"]``）。
            目录不存在返回空列表。
        """
        meta = self._metas.get(skill_name)
        if meta is not None and meta.resources:
            return list(meta.resources)
        # 缓存未命中，重新扫描
        skill_dir = self.skill_dir / skill_name
        if not skill_dir.exists():
            return []
        resources = self._scan_resources(skill_dir)
        if meta is not None:
            meta.resources = resources
        return resources


def load_skill_to_registry(registry, skill: Skill) -> None:
    """将 Skill 的工具注册到 ToolRegistry 的 Deferred Tier。

    每个工具通过 ``registry.register_deferred(...)`` 注册，工具名加前缀
    ``skill__{skill.name}__{tool['name']}`` 避免冲突。单个工具注册失败只
    记录 error，不抛异常。

    参数:
        registry: ``ToolRegistry`` 实例（需提供 ``register_deferred`` 方法）。
        skill: 已加载的 ``Skill`` 实例。
    """
    for tool in skill.tools:
        tool_name = tool.get("name")
        if not tool_name:
            continue
        handler = skill.handlers.get(tool_name)
        if handler is None:
            continue
        try:
            registry.register_deferred(
                name=f"skill__{skill.name}__{tool_name}",
                description=tool.get("description", "") + f" [Skill: {skill.name}]",
                input_schema=tool.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
                handler=handler,
            )
        except Exception as e:
            logger.error("注册 Skill %s 工具失败: %s", skill.name, e)


def register_skill_stub(
    registry,
    meta: SkillMeta,
    activate_handler: Callable,
) -> None:
    """注册 ``skill__{name}`` 到 Core Tier（空 schema 激活按钮）。

    对齐 agentskills.io 三层加载模型：
    - L1 元数据：本 stub 注入 tools schema（空 schema 激活按钮）
    - L2 body：LLM 调用 ``skill__{name}()`` 后，``activate_handler`` 触发激活，
      body 在下一轮注入 messages[0] 末位（由 orchestrator.activate_skill 处理）
    - L3 资源：通过 ``skill__resource`` 工具读取 scripts/ 下文件

    参数:
        registry: ``ToolRegistry`` 实例（需提供 ``register_core`` 方法）。
        meta: ``SkillMeta`` 实例（含 name 与 description）。
        activate_handler: 激活 handler 函数，签名 ``(**kwargs) -> str``。
            由 ``_make_skill_activate_handler`` 工厂生成（closure 捕获 skill_name）。
    """
    try:
        registry.register_core(
            name=f"skill__{meta.name}",
            description=f"[Skill] 激活 {meta.name}: {meta.description}",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=activate_handler,
        )
        logger.debug("已注册 Skill stub: skill__%s", meta.name)
    except Exception as e:
        logger.error("注册 Skill stub %s 失败: %s", meta.name, e)
