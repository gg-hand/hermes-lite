"""工具注册中心，支持 Core/Deferred 分层结构与按需加载机制。

分层设计：
- Core Tier：永远全量注入（返回完整 schema），保证 KV cache 100% 命中。
  适用于高频核心工具（如内置工具、list_tools/call_tool 元工具）。
- Deferred Tier：仅注入轻量 stub（name + description + defer_loading: True），
  不含 input_schema，不参与缓存 key 计算。适用于低频扩展工具（Skill/MCP）。
  Agent 需要使用 Deferred 工具时，先通过 list_tools 元工具搜索并加载到
  _loaded_tools，再通过 call_tool 或直接 execute_tool 调用。

向后兼容：
- 旧 ``register()`` 方法保留为 ``register_core()`` 的别名。
- 旧 ``search_tools()`` 方法保留，内部转调 ``search_and_load()``。
- ``defer_loading_threshold`` 与 ``enable_defer_loading`` 字段保留仅为向后兼容，
  不再驱动核心切换逻辑（原单层 stub 切换已废弃）。

满足 ReactLoop 的 ToolRegistry Protocol 接口：
- get_tools_schema() -> list
- execute_tool(tool_name, tool_input) -> str
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    """工具定义（dataclass）。

    Attributes:
        name: 工具名称（唯一标识）。
        description: 工具描述。
        input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
        handler: 工具执行函数，接收关键字参数，返回 str 结果。
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str]


class ToolRegistry:
    """工具注册中心，满足 ReactLoop 的 ToolRegistry Protocol 接口。

    采用 Core/Deferred 双层分层结构：
    - Core Tier：``_core_tools``，永远全量注入完整 schema。
    - Deferred Tier：``_deferred_tools``，仅注入 stub（不含 input_schema）。
    - Loaded：``_loaded_tools``，Deferred 工具经 list_tools 加载后的缓存，
      ``execute_tool`` 查找时优先于此层。

    Attributes:
        defer_loading_threshold: [deprecated] 保留仅为向后兼容，不再驱动
            核心切换逻辑。
        enable_defer_loading: [deprecated] 保留仅为向后兼容，不再驱动
            核心切换逻辑。
    """

    def __init__(
        self,
        defer_loading_threshold: int = 20,
        enable_defer_loading: Optional[bool] = None,
    ) -> None:
        """初始化工具注册中心。

        参数:
            defer_loading_threshold: [deprecated] 保留仅为向后兼容，不再驱动
                核心切换逻辑。默认 20。
            enable_defer_loading: [deprecated] 保留仅为向后兼容，不再驱动
                核心切换逻辑。
        """
        # 保留字段（deprecated，仅为向后兼容，不再驱动核心切换逻辑）
        self.defer_loading_threshold = defer_loading_threshold
        self.enable_defer_loading = enable_defer_loading
        # 分层存储：name -> ToolDef
        self._core_tools: Dict[str, ToolDef] = {}
        self._deferred_tools: Dict[str, ToolDef] = {}
        self._loaded_tools: Dict[str, ToolDef] = {}
        self._disabled_prefixes: Set[str] = set()

    def register_core(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., str],
    ) -> None:
        """注册工具到 Core Tier（永远全量注入）。

        参数:
            name: 工具名称（唯一标识，重复注册将覆盖旧定义）。
            description: 工具描述。
            input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
            handler: 工具执行函数，接收关键字参数，返回 str 结果。
        """
        self._core_tools[name] = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )
        logger.debug("已注册 Core 工具: %s", name)

    def register_deferred(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., str],
    ) -> None:
        """注册工具到 Deferred Tier（仅注入 stub，按需加载）。

        参数:
            name: 工具名称（唯一标识，重复注册将覆盖旧定义）。
            description: 工具描述。
            input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
            handler: 工具执行函数，接收关键字参数，返回 str 结果。
        """
        self._deferred_tools[name] = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )
        logger.debug("已注册 Deferred 工具: %s", name)

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., str],
    ) -> None:
        """注册工具（deprecated，使用 register_core 代替）。

        向后兼容别名，等价于 ``register_core()``。

        参数:
            name: 工具名称（唯一标识，重复注册将覆盖旧定义）。
            description: 工具描述。
            input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
            handler: 工具执行函数，接收关键字参数，返回 str 结果。
        """
        self.register_core(name, description, input_schema, handler)

    def unregister(self, name: str) -> None:
        """注销工具（从所有层级移除）。

        参数:
            name: 要注销的工具名称。不存在时静默忽略。
        """
        removed = False
        for store in (self._core_tools, self._deferred_tools, self._loaded_tools):
            if name in store:
                del store[name]
                removed = True
        if removed:
            logger.debug("已注销工具: %s", name)

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        """返回工具 schema 列表（Anthropic tool use 格式）。

        - Core Tier：返回完整 schema（``name``/``description``/``input_schema``）。
        - Deferred Tier：返回 stub（``name``/``description``/``defer_loading: True``），
          **不含** ``input_schema``。
        - 顺序：先 Core 后 Deferred。

        返回:
            工具 schema 列表。
        """
        schemas: List[Dict[str, Any]] = []
        # Core Tier：完整 schema
        for t in self._core_tools.values():
            schemas.append({
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            })
        # Deferred Tier：stub（不含 input_schema）
        for t in self._deferred_tools.values():
            disabled = any(t.name.startswith(prefix) for prefix in self._disabled_prefixes)
            entry = {
                "name": t.name,
                "description": t.description,
                "defer_loading": True,
            }
            if disabled:
                entry["enabled"] = False
            schemas.append(entry)
        return schemas

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """执行工具调用，返回结果字符串。

        查找顺序：``_loaded_tools`` → ``_core_tools``。
        如果是 Deferred 工具但未加载：返回提示信息。
        异常时返回错误信息字符串（不抛异常，保证 ReactLoop 稳定）。

        参数:
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。

        返回:
            执行结果字符串。
        """
        if any(tool_name.startswith(prefix) for prefix in self._disabled_prefixes):
            return f"工具 '{tool_name}' 已被禁用"
        tool = self._loaded_tools.get(tool_name)
        if tool is None:
            tool = self._core_tools.get(tool_name)
        if tool is None:
            # Deferred 但未加载
            if tool_name in self._deferred_tools:
                return f"工具 '{tool_name}' 未加载，请先调用 tool_list 加载"
            return f"未注册的工具: {tool_name}"

        try:
            # 将 tool_input 作为关键字参数传给 handler
            result = tool.handler(**(tool_input or {}))
            return str(result)
        except Exception as e:
            logger.error("工具 %s 执行失败: %s", tool_name, e)
            return f"工具 {tool_name} 执行出错: {e}"

    def search_and_load(
        self, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """搜索并按需加载 Deferred 工具。

        - ``query`` 以 ``select:`` 开头：解析逗号分隔的工具名，精确加载到
          ``_loaded_tools``，返回完整 schema 列表。
        - 否则：在 ``_deferred_tools`` 的 name/description 中模糊匹配关键词，
          匹配的加入 ``_loaded_tools``，返回完整 schema 列表。

        参数:
            query: 搜索查询（``select:Name1,Name2`` 精确加载，或自然语言关键词）。
            top_k: 返回匹配工具的最大数量，默认 5。

        返回:
            匹配工具的完整 schema 列表 ``[{name, description, input_schema}]``。
        """
        # 精确加载：select:Name1,Name2
        if query.startswith("select:"):
            names_str = query[len("select:"):]
            names = [n.strip() for n in names_str.split(",") if n.strip()]
            results: List[Dict[str, Any]] = []
            for n in names:
                tool = self._deferred_tools.get(n)
                if tool is not None:
                    self._loaded_tools[n] = tool
                    results.append({
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    })
            return results[:top_k]

        # 关键词模糊匹配（参考旧 search_tools 评分逻辑）
        if not query:
            # 无关键词时返回前 top_k 个 Deferred 工具
            matches = list(self._deferred_tools.values())[:top_k]
        else:
            query_lower = query.lower()
            # 拆分关键词（按空白符）
            keywords = [kw for kw in query_lower.split() if kw]
            scored: List[tuple] = []
            for t in self._deferred_tools.values():
                name = t.name.lower()
                desc = t.description.lower()
                score = 0
                # 名称完全匹配权重最高
                if query_lower == name:
                    score += 100
                # 名称包含查询串
                elif query_lower in name:
                    score += 50
                # 描述包含查询串
                if query_lower in desc:
                    score += 20
                # 关键词分别匹配
                for kw in keywords:
                    if kw in name:
                        score += 10
                    if kw in desc:
                        score += 5
                if score > 0:
                    scored.append((score, t))
            # 按分数降序，取前 top_k
            scored.sort(key=lambda x: x[0], reverse=True)
            matches = [t for _, t in scored[:top_k]]

        # 加载到 _loaded_tools 并返回完整 schema
        results = []
        for t in matches:
            self._loaded_tools[t.name] = t
            results.append({
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            })
        return results

    def search_tools(self, query: str, top_k: int = 5) -> list:
        """工具搜索（deprecated，使用 search_and_load 代替）。

        向后兼容方法，内部转调 ``search_and_load()``。

        参数:
            query: 搜索关键词。
            top_k: 返回匹配工具的最大数量，默认 5。

        返回:
            匹配的工具列表 ``[{name, description, input_schema}]``。
        """
        return self.search_and_load(query, top_k=top_k)

    def get_full_schema(self, tool_name: str) -> dict:
        """获取单个工具的完整 schema。

        查找顺序：``_core_tools`` → ``_deferred_tools`` → ``_loaded_tools``。

        参数:
            tool_name: 工具名称。

        返回:
            工具的完整 schema dict。若工具不存在返回空 dict。
        """
        tool = self._core_tools.get(tool_name)
        if tool is None:
            tool = self._deferred_tools.get(tool_name)
        if tool is None:
            tool = self._loaded_tools.get(tool_name)
        if tool is None:
            return {}
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

    def disable_skill(self, skill_name: str) -> None:
        """禁用指定 skill 的所有工具。

        Deferred tier 中以 ``skill__{skill_name}__`` 为前缀的工具将被标记为已禁用。

        参数:
            skill_name: 要禁用的 skill 名称。
        """
        prefix = f"skill__{skill_name}__"
        self._disabled_prefixes.add(prefix)
        logger.info("已禁用 skill: %s", skill_name)

    def enable_skill(self, skill_name: str) -> None:
        """启用指定 skill 的所有工具。

        参数:
            skill_name: 要启用的 skill 名称。
        """
        prefix = f"skill__{skill_name}__"
        self._disabled_prefixes.discard(prefix)
        logger.info("已启用 skill: %s", skill_name)

    def is_skill_disabled(self, skill_name: str) -> bool:
        """检查指定 skill 是否已被禁用。

        参数:
            skill_name: 要检查的 skill 名称。

        返回:
            如果该 skill 已被禁用返回 True，否则返回 False。
        """
        return f"skill__{skill_name}__" in self._disabled_prefixes

    def unregister_by_prefix(self, prefix: str) -> int:
        """根据前缀从 Deferred 和 Loaded 层批量注销工具。

        不会影响 Core Tier（``_core_tools``）中的工具。

        参数:
            prefix: 工具名称前缀。

        返回:
            被注销的工具数量。
        """
        count = 0
        for store in (self._deferred_tools, self._loaded_tools):
            to_remove = [name for name in store if name.startswith(prefix)]
            for name in to_remove:
                del store[name]
                count += 1
        if count > 0:
            logger.debug("已按前缀 '%s' 注销 %d 个工具", prefix, count)
        return count
