"""独立的 cron_tool 注册中心（Phase 8 Task 5.3）。

本模块提供 :class:`CronToolRegistry`，与全局 :class:`ToolRegistry` 接口兼容
（``get_tools_schema`` / ``get_tool_handler`` / ``execute_tool``），但**不进全局
registry**——用户会话不可见，仅用于 cron 调度会话。

设计要点：
- **隔离性硬约束**：cron_tool 工具的 schema **绝不**注入全局 ToolRegistry，
  保证用户会话的 tools schema 字节级稳定（缓存硬约束 1）。
- **懒加载**：工具在激活（``register``）时才解析 TOOL.md 并缓存 meta，避免
  启动时全量扫描 ``cron_tool/`` 目录。
- **与 ToolRegistry 接口兼容**：``get_tools_schema`` 返回 Anthropic tool use
  格式的 schema 列表（含 name/description/input_schema），``execute_tool``
  通过 cron_tool_loader 子进程执行。
- **可热更新**：``reload`` 方法重新解析 TOOL.md（编辑后调用），
  ``unregister`` 方法从 registry 移除（删除工具前调用）。

与 :mod:`src.agent.tool_registry` 的对比：
- 全局 ``ToolRegistry``：Core/Deferred 双层，用户会话可见，KV cache 命中区
- ``CronToolRegistry``：单层（全部等价于 Core），仅 cron 会话可见，子进程执行

模块依赖：
- :mod:`src.tasks.cron_tool_loader`（``load_tool`` / ``execute_tool`` /
  ``list_tools``）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

# 兼容相对导入与直接运行两种方式
from teage_liu.tasks.cron_tool_loader import (
    CronToolError,
    CronToolMeta,
    DEFAULT_BASE_DIR,
    execute_tool as _execute_tool,
    list_tools as _list_tools,
    load_tool as _load_tool,
)
from teage_liu.agent.tool_error import (
    ParamError,
    ToolError,
    ToolNotFoundError,
    from_cron_error,
    from_exception,
)
logger = logging.getLogger(__name__)

# jsonschema 延迟导入
try:
    import jsonschema  # type: ignore
    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False

#: Q5 决策：cron_tool registry key 前缀（与 schedules.yaml 的
#: granted_tools / active_tools_snapshot / step.config.tool 命名约定一致）
_PREFIX = "cron_tool__"


class CronToolRegistry:
    """cron_tool 独立注册中心（与全局 ToolRegistry 接口兼容，但不进全局）。

    所有 cron_tool 工具通过子进程执行（:func:`cron_tool_loader.execute_tool`），
    schema 由 TOOL.md 的 input_schema 字段生成。

    Q5 决策：``_tools`` / ``_handlers`` 的 key 统一为带前缀的
    ``registered_name``（``cron_tool__{dir_name}``），与 schedules.yaml 的
    naming convention 对齐。``_build_handler`` 内部用 ``meta.dir_name``
    拼接文件路径，避免前缀导致的路径错误。

    Attributes:
        base_dir: cron_tool 根目录，默认 :data:`DEFAULT_BASE_DIR`。
        _tools: 已注册工具的 registered_name → :class:`CronToolMeta` 映射。
        _handlers: 已注册工具的 registered_name → handler callable 映射
            （闭包捕获 meta 与 base_dir，调用 ``_execute_tool``）。
    """

    def __init__(self, base_dir: str = DEFAULT_BASE_DIR) -> None:
        """初始化 cron_tool 注册中心。

        参数:
            base_dir: cron_tool 根目录。默认 :data:`DEFAULT_BASE_DIR`。
                支持相对路径与绝对路径。
        """
        self.base_dir = base_dir
        self._tools: Dict[str, CronToolMeta] = {}
        self._handlers: Dict[str, Callable[..., str]] = {}
        # schema validator 缓存：registered_name -> Draft7Validator
        self._schema_cache: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 注册 / 注销 / 重载
    # ------------------------------------------------------------------

    def register(self, name: str) -> CronToolMeta:
        """注册一个 cron_tool（解析 TOOL.md 并缓存 meta）。

        Q5 决策：分离 ``dir_name``（文件系统标识）与 ``registered_name``
        （registry key）。传入的 ``name`` 可带 ``cron_tool__`` 前缀也可
        不带，统一剥离后作为 ``dir_name``，再加前缀生成 ``registered_name``。

        重复注册同名工具会覆盖旧定义（重新加载场景）。

        参数:
            name: cron_tool 名称（可带 ``cron_tool__`` 前缀，也可裸目录名）。

        返回:
            解析得到的 :class:`CronToolMeta`（已填充 dir_name / registered_name）。

        Raises:
            CronToolError: TOOL.md 解析失败或 run.* 脚本缺失。
        """
        # Q5: 剥离前缀得到裸目录名（用于文件路径）
        dir_name = name[len(_PREFIX):] if name.startswith(_PREFIX) else name
        meta = _load_tool(dir_name, base_dir=self.base_dir)
        # Q5: 填充 dir_name / registered_name
        meta.dir_name = dir_name
        meta.registered_name = f"{_PREFIX}{dir_name}"
        self._tools[meta.registered_name] = meta
        # 构造 handler 闭包（捕获 meta，避免每次执行重新解析 TOOL.md）
        self._handlers[meta.registered_name] = self._build_handler(meta)
        logger.info(
            "已注册 cron_tool: %s (dir=%s, version=%s)",
            meta.registered_name, meta.dir_name, meta.version,
        )
        return meta

    def unregister(self, name: str) -> bool:
        """从 registry 注销一个 cron_tool。

        仅移除内存中的 meta 与 handler，**不删除磁盘文件**。删除文件由
        上层（server.py 端点）负责。

        Q5: ``name`` 应为带前缀的 ``registered_name``（与 ``_tools`` key 一致）。

        参数:
            name: 工具名（带 ``cron_tool__`` 前缀）。

        返回:
            ``True`` 表示已移除；``False`` 表示工具未注册。
        """
        removed = False
        if name in self._tools:
            del self._tools[name]
            removed = True
        if name in self._handlers:
            del self._handlers[name]
        if name in self._schema_cache:
            del self._schema_cache[name]
        if removed:
            logger.info("已注销 cron_tool: %s", name)
        return removed

    def reload(self, name: str) -> CronToolMeta:
        """重新加载 cron_tool（编辑 TOOL.md / run.* 后调用）。

        等价于 ``register``（覆盖式注册），语义上强调「重新加载」。

        参数:
            name: 工具名（可带前缀也可裸目录名）。

        返回:
            重新解析得到的 :class:`CronToolMeta`。
        """
        return self.register(name)

    def load_all(self) -> Dict[str, CronToolMeta]:
        """扫描 ``base_dir`` 下所有已激活的 cron_tool 并注册。

        用于服务启动时批量加载。单工具加载失败仅记录 warning，不中断整体
        加载（防御性：单工具损坏不影响其他工具）。

        返回:
            成功加载的 registered_name → meta 映射（Q5: key 为带前缀名）。
        """
        names = _list_tools(base_dir=self.base_dir)
        loaded: Dict[str, CronToolMeta] = {}
        for name in names:
            try:
                meta = self.register(name)
                loaded[meta.registered_name] = meta
            except CronToolError as exc:
                logger.warning(
                    "启动加载 cron_tool '%s' 失败，跳过: %s", name, exc
                )
        return loaded

    # ------------------------------------------------------------------
    # 查询（与 ToolRegistry 接口兼容）
    # ------------------------------------------------------------------

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        """返回所有已注册 cron_tool 的 schema 列表（Anthropic tool use 格式）。

        Q5: schema 的 ``name`` 字段用 ``meta.get_registered_name()``（带前缀），
        与 schedules.yaml / LLM tool_use 命名约定一致。

        返回:
            schema 列表，按注册顺序。
        """
        return [
            {
                "name": meta.get_registered_name(),
                "description": meta.description,
                "input_schema": meta.input_schema,
            }
            for meta in self._tools.values()
        ]

    def get_tool_handler(self, name: str) -> Optional[Callable[..., str]]:
        """返回指定工具的 handler callable。

        Q5: ``name`` 应为带前缀的 ``registered_name``。未注册返回 ``None``。

        参数:
            name: 工具名（带 ``cron_tool__`` 前缀）。

        返回:
            handler callable，未注册返回 ``None``。
        """
        return self._handlers.get(name)

    def get_tool_meta(self, name: str) -> Optional[CronToolMeta]:
        """返回指定工具的 meta（含 version / timeout 等元数据）。

        Q5: ``name`` 应为带前缀的 ``registered_name``。

        参数:
            name: 工具名（带 ``cron_tool__`` 前缀）。

        返回:
            :class:`CronToolMeta`，未注册返回 ``None``。
        """
        return self._tools.get(name)

    def has_tool(self, name: str) -> bool:
        """判断工具是否已注册。

        Q5: ``name`` 应为带前缀的 ``registered_name``。裸目录名不再被接受
        （移除 ``_normalize_name`` 后调用方需统一传带前缀名）。
        """
        return name in self._tools

    def list_tool_names(self) -> List[str]:
        """返回所有已注册工具名（按注册顺序）。

        Q5: 返回的是 ``registered_name``（带前缀）。
        """
        return list(self._tools.keys())

    # ------------------------------------------------------------------
    # 执行（与 ToolRegistry 接口兼容）
    # ------------------------------------------------------------------

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """执行 cron_tool，返回结果字符串。

        失败语义（与 :meth:`ToolRegistry.execute_tool` 对齐）：
        - 工具未注册 → 抛 :class:`ToolNotFoundError`
        - 参数校验失败 → 抛 :class:`ParamError`
        - 子进程返回错误 JSON → 由 :func:`from_cron_error` 归一化
        - 其他异常 → 由 :func:`from_exception` 归一化

        Q5: ``tool_name`` 应为带前缀的 ``registered_name``。

        参数:
            tool_name: 工具名（带 ``cron_tool__`` 前缀）。
            tool_input: 工具入参 dict。

        返回:
            执行结果字符串（成功时）。

        抛出:
            ToolError: 工具执行失败的统一异常基类。
        """
        handler = self._handlers.get(tool_name)
        meta = self._tools.get(tool_name)
        if handler is None or meta is None:
            raise ToolNotFoundError(
                tool_name=tool_name,
                reason=f"未注册的 cron_tool: {tool_name}",
                suggestion="检查 cron_tool 配置或调用 list_tools 查看可用工具",
            )

        # schema 校验（meta.input_schema 在主进程可用）
        schema = getattr(meta, "input_schema", None)
        if _HAS_JSONSCHEMA and schema:
            validator = self._get_validator(tool_name, schema)
            try:
                validator.validate(tool_input or {})
            except jsonschema.ValidationError as ve:
                allowed = list(schema.get("properties", {}).keys())
                raise ParamError(
                    tool_name=tool_name,
                    reason=f"参数校验失败：{ve.message}",
                    suggestion=f"cron_tool 支持参数：{allowed}" if allowed else "检查 TOOL.md input_schema",
                ) from ve
            # 额外属性检查
            allowed_keys = set(schema.get("properties", {}).keys())
            if allowed_keys:
                extra = set((tool_input or {}).keys()) - allowed_keys
                if extra:
                    raise ParamError(
                        tool_name=tool_name,
                        reason=f"未声明的参数：{sorted(extra)}",
                        suggestion=f"cron_tool 支持参数：{sorted(allowed_keys)}",
                    )
            # 必填字段检查
            # 注意：必须用 ``k not in tool_input`` 而非 ``not tool_input.get(k)``，
            # 后者会把合法的 falsy 值（0/False/""）误判为缺失。
            required = schema.get("required", [])
            missing = [k for k in required if k not in (tool_input or {})]
            if missing:
                raise ParamError(
                    tool_name=tool_name,
                    reason=f"缺少必填参数：{missing}",
                    suggestion=f"必填参数：{required}",
                )

        try:
            raw = handler(**(tool_input or {}))
            result_str = str(raw)
            # 检测子进程返回的错误 JSON（cron_tool_loader._format_error 格式）
            if result_str.startswith('{"error"') and result_str.endswith("}"):
                try:
                    err_dict = json.loads(result_str)
                    if isinstance(err_dict, dict) and "error" in err_dict:
                        raise from_cron_error(tool_name, err_dict)
                except json.JSONDecodeError:
                    pass  # 不是错误 JSON，按正常结果处理
            return result_str
        except ToolError:
            raise
        except Exception as exc:
            logger.error("cron_tool %s 执行失败: %s", tool_name, exc)
            raise from_exception(tool_name, exc) from exc

    def _get_validator(self, name: str, schema: dict):
        """获取（或编译缓存）jsonschema Draft7Validator。

        Q5: ``name`` 为 ``registered_name``（带前缀），与 ``_tools`` key 一致。
        """
        if name not in self._schema_cache:
            self._schema_cache[name] = jsonschema.Draft7Validator(schema)
        return self._schema_cache[name]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_handler(
        self, meta: CronToolMeta
    ) -> Callable[..., str]:
        """为指定 meta 构造 handler 闭包。

        handler 接收工具入参（keyword args），内部组装 ``input`` dict 与
        ``context`` dict（context 由调用方通过 ``set_context_provider``
        注入或为空），调用 :func:`cron_tool_loader.execute_tool`。

        Q5: 用 ``meta.dir_name``（裸目录名）作为 ``_execute_tool`` 的 ``name``
        参数，避免带前缀名导致的文件路径错误。

        参数:
            meta: 工具元数据（必须已填充 dir_name）。

        返回:
            handler callable，签名 ``(**kwargs) -> str``。
        """

        def _handler(**kwargs: Any) -> str:
            # 从 context provider 取上下文（如 session_id / schedule_id）
            context = None
            provider = getattr(self, "_context_provider", None)
            if callable(provider):
                try:
                    context = provider() or {}
                except Exception:
                    context = {}
            return _execute_tool(
                name=meta.dir_name,  # Q5: 用裸目录名拼路径
                input=kwargs,
                context=context,
                meta=meta,
                base_dir=self.base_dir,
            )

        return _handler

    def set_context_provider(
        self, provider: Optional[Callable[[], Dict[str, Any]]]
    ) -> None:
        """注入上下文 provider 回调。

        provider 在 handler 执行时被调用，返回 ``context`` dict（含
        session_id / schedule_id / current_time 等）注入子进程 stdin。

        参数:
            provider: 返回 context dict 的 callable。为 ``None`` 时禁用。
        """
        self._context_provider = provider
