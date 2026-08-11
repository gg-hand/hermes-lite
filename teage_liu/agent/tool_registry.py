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

from .tool_error import (
    InternalError,
    ParamError,
    ToolError,
    ToolNotFoundError,
    from_exception,
)

logger = logging.getLogger(__name__)

# jsonschema 延迟导入，避免在缺失时阻断模块加载
try:
    import jsonschema  # type: ignore
    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False
    logger.debug("jsonschema 不可用，将跳过工具参数 schema 校验")


@dataclass
class ToolDef:
    """工具定义（dataclass）。

    Attributes:
        name: 工具名称（唯一标识）。
        description: 工具描述。
        input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
        handler: 工具执行函数，接收关键字参数，返回 str 结果。
        blocking: 是否为阻塞型工具（内部可能长时间轮询/等待，如 subagent
            协作）。True 时由 sync/stream runner 用 asyncio.to_thread 放到
            工作线程执行，避免冻结事件循环线程。
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str]
    blocking: bool = False


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
        # schema validator 缓存：name -> Draft7Validator
        self._schema_cache: Dict[str, Any] = {}

    def register_core(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., str],
        blocking: bool = False,
    ) -> None:
        """注册工具到 Core Tier（永远全量注入）。

        参数:
            name: 工具名称（唯一标识，重复注册将覆盖旧定义）。
            description: 工具描述。
            input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
            handler: 工具执行函数，接收关键字参数，返回 str 结果。
            blocking: 阻塞型工具标记，True 时由 runner 用 asyncio.to_thread
                执行（见 ToolDef.blocking）。
        """
        self._core_tools[name] = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            blocking=blocking,
        )
        logger.debug("已注册 Core 工具: %s", name)

    def register_deferred(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., str],
        blocking: bool = False,
    ) -> None:
        """注册工具到 Deferred Tier（仅注入 stub，按需加载）。

        参数:
            name: 工具名称（唯一标识，重复注册将覆盖旧定义）。
            description: 工具描述。
            input_schema: 工具输入参数的 JSON Schema（Anthropic tool use 格式）。
            handler: 工具执行函数，接收关键字参数，返回 str 结果。
            blocking: 阻塞型工具标记（见 ToolDef.blocking）。
        """
        self._deferred_tools[name] = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            blocking=blocking,
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

    def get_handler(self, name: str) -> Optional[Callable[..., str]]:
        """获取工具 handler（跨所有 tier 查找），用于后端 API 直接调用。

        与 :meth:`execute_tool` 不同，此方法也查找 Deferred Tier（无需先
        加载），适合 server.py 中 ``confirm_proposal`` 等后端路由直接调用
        工具逻辑（非 LLM 工具调用流程）。

        参数:
            name: 工具名称。

        返回:
            handler 函数；工具不存在返回 ``None``。
        """
        for store in (self._loaded_tools, self._core_tools, self._deferred_tools):
            tool = store.get(name)
            if tool is not None:
                return tool.handler
        return None

    def is_blocking_tool(self, name: str) -> bool:
        """判断工具是否为阻塞型（需 asyncio.to_thread 执行）。

        查找顺序与 ``execute_tool`` 一致：``_loaded_tools`` → ``_core_tools``
        （Deferred 未加载时视为非阻塞）。

        参数:
            name: 工具名称。

        返回:
            True 表示阻塞型工具。
        """
        tool = self._loaded_tools.get(name)
        if tool is None:
            tool = self._core_tools.get(name)
        if tool is None:
            tool = self._deferred_tools.get(name)
        return bool(tool and tool.blocking)

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        """返回工具 schema 列表（Anthropic tool use 格式）。

        - Core Tier：返回完整 schema（``name``/``description``/``input_schema``）。
          P1-5：被 ``_disabled_prefixes`` 命中的 Core 工具追加 ``enabled: False``
          （如已禁用的 skill 激活按钮 ``skill__{name}``）。
        - Deferred Tier：返回 stub（``name``/``description``/``defer_loading: True``），
          **不含** ``input_schema``。
        - 顺序：先 Core 后 Deferred。

        返回:
            工具 schema 列表。
        """
        schemas: List[Dict[str, Any]] = []
        # Core Tier：完整 schema（被禁用的工具追加 enabled: False）
        for t in self._core_tools.values():
            disabled = any(t.name.startswith(prefix) for prefix in self._disabled_prefixes)
            entry = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            if disabled:
                entry["enabled"] = False
            schemas.append(entry)
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

        失败语义（统一异常层次）：
        - 工具未注册/已禁用/Deferred 未加载 → 抛 :class:`ToolNotFoundError`
        - 参数校验失败（schema 不匹配） → 抛 :class:`ParamError`
        - handler 抛 :class:`ToolError` 子类 → 原样上抛
        - handler 抛其他异常 → 由 :func:`from_exception` 归一化为对应 ToolError 子类

        ReactLoop 调用方需捕获 :class:`ToolError` 并按 ``stage`` 分流处理。

        参数:
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。

        返回:
            执行结果字符串（成功时）。

        抛出:
            ToolError: 工具执行失败的统一异常基类。
        """
        if any(tool_name.startswith(prefix) for prefix in self._disabled_prefixes):
            raise ToolNotFoundError(
                tool_name=tool_name,
                reason=f"工具 '{tool_name}' 已被禁用",
                suggestion="启用后再试或换用其他工具",
            )
        tool = self._loaded_tools.get(tool_name)
        if tool is None:
            tool = self._core_tools.get(tool_name)
        if tool is None:
            # Deferred 但未加载
            if tool_name in self._deferred_tools:
                raise ToolNotFoundError(
                    tool_name=tool_name,
                    reason=f"工具 '{tool_name}' 未加载",
                    suggestion="先调用 tool_list 加载该 Deferred 工具",
                )
            raise ToolNotFoundError(
                tool_name=tool_name,
                reason=f"未注册的工具: {tool_name}",
                suggestion="确认工具名拼写或调用 tool_list 查看可用工具",
            )

        # schema 校验（jsonschema），拦截幻觉性参数
        if _HAS_JSONSCHEMA and tool.input_schema:
            validator = self._get_validator(tool_name, tool.input_schema)
            try:
                validator.validate(tool_input or {})
            except jsonschema.ValidationError as ve:
                allowed = list(tool.input_schema.get("properties", {}).keys())
                raise ParamError(
                    tool_name=tool_name,
                    reason=f"参数校验失败：{ve.message}",
                    suggestion=f"工具支持参数：{allowed}" if allowed else "检查工具 schema",
                ) from ve
            # 额外属性检查（jsonschema 默认允许 additionalProperties，需手动拦截）
            allowed_keys = set(tool.input_schema.get("properties", {}).keys())
            if allowed_keys:
                extra = set((tool_input or {}).keys()) - allowed_keys
                if extra:
                    raise ParamError(
                        tool_name=tool_name,
                        reason=f"未声明的参数：{sorted(extra)}",
                        suggestion=f"工具支持参数：{sorted(allowed_keys)}",
                    )
            # 必填字段检查（双保险，jsonschema 已覆盖但此处给出更友好提示）
            # 注意：必须用 ``k not in tool_input`` 而非 ``not tool_input.get(k)``，
            # 后者会把合法的 falsy 值（0/False/""）误判为缺失。
            required = tool.input_schema.get("required", [])
            missing = [k for k in required if k not in (tool_input or {})]
            if missing:
                raise ParamError(
                    tool_name=tool_name,
                    reason=f"缺少必填参数：{missing}",
                    suggestion=f"必填参数：{required}",
                )

        try:
            # 将 tool_input 作为关键字参数传给 handler
            result = tool.handler(**(tool_input or {}))
            return str(result)
        except ToolError:
            # ToolError 子类直接上抛，不再吞没
            raise
        except Exception as e:
            # 非 ToolError 异常 → 归一化为 ToolError 子类
            logger.error("工具 %s 执行失败: %s", tool_name, e)
            raise from_exception(tool_name, e) from e

    def _get_validator(self, name: str, schema: dict):
        """获取（或编译缓存）jsonschema Draft7Validator。

        参数:
            name: 工具名（缓存 key）。
            schema: 工具的 input_schema。

        返回:
            ``jsonschema.Draft7Validator`` 实例。
        """
        if name not in self._schema_cache:
            self._schema_cache[name] = jsonschema.Draft7Validator(schema)
        return self._schema_cache[name]

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

        P1-5：双前缀兼容，覆盖两类工具：
        - ``skill__{skill_name}``：精确匹配激活按钮（Core Tier，新主流程）
        - ``skill__{skill_name}__``：前缀匹配旧业务工具（Deferred Tier，向后兼容）

        参数:
            skill_name: 要禁用的 skill 名称。
        """
        self._disabled_prefixes.add(f"skill__{skill_name}")
        self._disabled_prefixes.add(f"skill__{skill_name}__")
        logger.info("已禁用 skill: %s", skill_name)

    def enable_skill(self, skill_name: str) -> None:
        """启用指定 skill 的所有工具。

        参数:
            skill_name: 要启用的 skill 名称。
        """
        self._disabled_prefixes.discard(f"skill__{skill_name}")
        self._disabled_prefixes.discard(f"skill__{skill_name}__")
        logger.info("已启用 skill: %s", skill_name)

    def is_skill_disabled(self, skill_name: str) -> bool:
        """检查指定 skill 是否已被禁用。

        参数:
            skill_name: 要检查的 skill 名称。

        返回:
            如果该 skill 已被禁用返回 True，否则返回 False。
        """
        return (
            f"skill__{skill_name}" in self._disabled_prefixes
            or f"skill__{skill_name}__" in self._disabled_prefixes
        )

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
