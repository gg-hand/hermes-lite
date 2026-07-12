"""内置工具包：按领域分组的工具函数与注册器。

子模块：
- ``file_tools``: 文件读写/搜索工具 + v2 注册器（联动 FileOperationRegistry）
- ``shell_tools``: execute_command / bash_exec / kill_running_process
- ``web_tools``: http_request / web_search（含反爬升级策略）
- ``memory_tools``: search_memory / update_memory / update_profile
- ``plan_tools``: plan_task / update_todo

本 ``__init__`` 模块聚合：
- ``BUILTIN_TOOLS``：Core Tier 工具定义清单（name/description/schema/handler）
- ``register_builtin_tools``：将 BUILTIN_TOOLS + 元工具注册到 ToolRegistry
- 所有子模块的公开 API re-export（向后兼容 ``from .builtin_tools import *``）
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable, Optional

from .file_tools import (
    delete_file,
    file_edit,
    file_glob,
    file_grep,
    list_directory,
    read_file,
    register_file_tools,
    write_file,
    _register_delete_file_v2,
    _register_write_file_v2,
)
from .memory_tools import (
    _register_update_profile,
    register_memory_tools,
)
from .plan_tools import register_plan_tools
from .shell_tools import (
    execute_command,
    kill_running_process,
    register_bash_tool,
    _get_and_clear_running_proc,
    _has_shell_metachar,
    _maybe_rewrite_multiline_python_c,
    _execute_command_inner,
    _set_running_proc,
)
from .web_tools import (
    http_request,
    web_search,
    _build_enhanced_headers,
    _classify_quality,
    _extract_domain,
    _html_to_plain_text,
    _load_domain_state,
    _persist_success_headers,
    _save_domain_state,
    _search_baidu,
)

if TYPE_CHECKING:
    from ..file_registry import FileOperationRegistry
    from ..memory.consolidation import ConsolidationEngine


# 工具定义列表：[(name, description, input_schema, handler), ...]
BUILTIN_TOOLS = [
    (
        "file_read",
        "读取指定路径文件的内容并返回文本。读取文件应优先使用此工具，而非通过 bash_exec 执行 cat/type 命令——本工具更安全、无需 shell 权限、自动处理编码。\n\n⚠ 路径边界：Hermes Lite 自身源码（src/、tests/、config.yaml 等）受 PolicyEngine 黑名单保护，调用 file_read 读取这些路径会被直接 deny。如需了解项目实现请询问用户。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径。",
                },
                "offset": {
                    "type": "integer",
                    "description": "可选，起始行号（从 0 开始），0 表示从开头读取。",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多读取的行数，0 表示读取全部行。",
                    "default": 0,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "可选，最多返回的字符数，超过时截断。默认 20000。",
                    "default": 20000,
                },
            },
            "required": ["path"],
        },
        read_file,
    ),
    (
        "file_write",
        "将内容写入指定路径文件（覆盖写入）。自动创建父目录、编码安全、记录操作到审计。✅ 写入代码、配置、文档 ❌ 简单文本拼接（用 echo）",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容。",
                },
            },
            "required": ["path", "content"],
        },
        write_file,
    ),
    (
        "file_delete",
        "删除指定路径文件。删除文件应优先使用此工具，而非通过 bash_exec 执行 rm——本工具集成审计与策略决策，更安全可控。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件路径。",
                },
            },
            "required": ["path"],
        },
        delete_file,
    ),
    (
        "file_listdir",
        "列出指定目录下的文件与子目录。列出目录应优先使用此工具，而非通过 bash_exec 执行 ls/dir——本工具输出格式统一、无 shell 开销。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录路径，默认当前目录。",
                    "default": ".",
                },
            },
            "required": [],
        },
        list_directory,
    ),
    (
        "file_edit",
        "在文件中做精准文本替换（将 old_string 替换为 new_string）。比 file_read+file_write 更安全高效，推荐用于局部修改代码或配置。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径。",
                },
                "old_string": {
                    "type": "string",
                    "description": "要被替换的原有文本（必须存在且唯一）。",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新文本。",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        file_edit,
    ),
    (
        "file_glob",
        "【文件名搜索】按通配符模式查找文件路径。适合知道文件名但不确定路径的场景，如 ``**/*.py``、``src/**/*.ts``。",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "通配符模式，如 ``**/*.py``。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回条数，默认 100。",
                    "default": 100,
                },
            },
            "required": ["pattern"],
        },
        file_glob,
    ),
    (
        "file_grep",
        "【文本字符串搜索】在文件中搜索精确关键词，返回 文件路径:行号:行内容。适合搜索代码变量名、函数名、特定字符串等精确匹配场景。",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "要搜索的文本（支持子串匹配）。",
                },
                "glob": {
                    "type": "string",
                    "description": "文件通配符，默认 ``**/*``（所有文件）。",
                    "default": "**/*",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回行数，默认 50。",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        },
        file_grep,
    ),
    (
        "web_fetch",
        "发起 HTTP 请求并返回响应文本，优先使用此工具而非 curl/wget。\n"
        "\n"
        "返回格式：首行为 [HTTP {状态码}] + [URL] + [Type] 元数据，空行后为响应体。\n"
        "\n"
        "状态码含义与策略：\n"
        "- 403/412 或响应含「验证码」「人机验证」「access denied」等 = 目标有反爬虫保护，"
        "不要对相同目标用相同参数重试，应添加 Cookie/Referer 等请求头或换用其他方式。\n"
        "- 404/410 = 资源永久不存在，重试无效。\n"
        "- 429/5xx = 临时性错误，可适当重试。\n"
        "- 永久或反爬错误响应末尾会附加 [系统提示] 引导改正策略，请注意阅读。\n\n"
        "⚠ 失败重试上限：同一域名连续失败 2 次后，不要再换 URL 重试，改用 web_search 工具。\n"
        "⚠ 不要自己拼 URL 抓站查实时信息（如价格、新闻）——直接用 web_search。",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "请求的 URL。",
                },
                "method": {
                    "type": "string",
                    "description": "HTTP 方法，如 GET/POST，默认 GET。",
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": "自定义 HTTP 请求头 dict，如 {\"Cookie\": \"...\", \"Referer\": \"...\"}。与默认浏览器头合并，自定义头优先。反爬虫网站可通过此参数添加认证信息。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 30。",
                    "default": 30,
                },
                "no_cache": {
                    "type": "boolean",
                    "description": "跳过域名状态缓存，不注入已保存的 Cookie/Referer。",
                    "default": False,
                },
                "save_state": {
                    "type": "boolean",
                    "description": "成功后是否将 Cookie/Referer 等存入域名缓存，下次自动注入。默认 True。",
                    "default": True,
                },
            },
            "required": ["url"],
        },
        http_request,
    ),
    (
        "web_search",
        "通过百度搜索 API 执行网页搜索，返回结构化结果摘要（标题 + URL + 摘要片段）。"
        "搜索公开信息应优先使用此工具，而非通过 web_fetch 抓取搜索引擎页面。"
        "需要配置 BAIDU_API_KEY（环境变量或 config.yaml）。免费额度：每日 100 次。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（支持中文、英文等自然语言查询）。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果条数，默认 5，最大 10。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        web_search,
    ),
]


def register_builtin_tools(
    registry,
    file_registry: Optional["FileOperationRegistry"] = None,
    get_session_id: Callable[[], Optional[str]] = lambda: None,
    consolidation_engine: Optional["ConsolidationEngine"] = None,
    signal_pool=None,
) -> None:
    """将内置工具与元工具注册到 ToolRegistry 实例。

    Core Tier 工具通过 ``register_core()`` 注册（高频，字节级稳定）；
    Deferred Tier 工具通过 ``register_deferred()`` 注册（低频/高风险，按需加载）。

    注册清单：
    - 6 个内置工具（BUILTIN_TOOLS 列表，含基础版 write_file / delete_file）
    - 若注入 ``file_registry``：通过 closure 覆盖注册 write_file v2 版本
      （执行后调用 ``file_registry.record_write`` 记录新建/修改状态），
      并覆盖注册 delete_file v2 版本（执行后调用 ``file_registry.remove``
      同步集合状态）。
    - 若注入 ``consolidation_engine``：注册 update_profile 工具（Core Tier），
      允许 LLM 通过 add/replace/delete 三种操作显式修改用户画像 memory.md。
      add 操作走信号池累积（若注入 signal_pool），达阈值才写入；replace/delete
      直接入 pending 队列，下次 consolidate 时统一合并。为 ``None`` 时不注册。
    - 2 个元工具（list_tools / call_tool，closure 模式访问 registry 实例）

    参数:
        registry: ToolRegistry 实例。
        file_registry: v2 可选注入 ``FileOperationRegistry``。注入后 write_file
            与 delete_file 会通过 closure 覆盖为基础版本，记录文件操作到
            集合供 PolicyEngine 决策。为 ``None`` 时使用 BUILTIN_TOOLS 中的
            基础版本（向后兼容）。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 ``None``。用于 write_file / delete_file v2 版本通过 closure 获取
            当前会话 ID。默认返回 ``None``（不记录到 file_registry）。
        consolidation_engine: 可选的 ``ConsolidationEngine`` 实例。注入后注册
            update_profile 工具（Core Tier）。为 ``None`` 时不注册该工具（向后
            兼容，避免在 ConsolidationEngine 不可用的部署中注册无用工具）。
        signal_pool: 可选的 ``SignalPool`` 实例。注入后 update_profile 的 add
            操作走信号池累积（L1 入池，达阈值才入 pending 队列写入画像）；
            为 ``None`` 时 add 操作回退到直接入 pending 队列（向后兼容）。
            replace/delete 不受此参数影响，始终直接入队。
    """
    # 1. 注册内置工具（文件 / 命令 / HTTP / Plan 模式等）为 Core Tier
    for name, description, input_schema, handler in BUILTIN_TOOLS:
        registry.register_core(name, description, input_schema, handler)

    # 1.5 v2 覆盖：若注入 file_registry，用 closure 版本覆盖 write_file / delete_file
    if file_registry is not None:
        _register_write_file_v2(registry, file_registry, get_session_id)
        _register_delete_file_v2(registry, file_registry, get_session_id)

    # 1.6 若注入 consolidation_engine，注册 update_profile 工具（Core Tier）
    if consolidation_engine is not None:
        _register_update_profile(registry, consolidation_engine, signal_pool)

    # 2. 注册元工具 list_tools / call_tool（Core Tier，始终全量注入）
    #    使用 closure 模式，使元工具内部能访问 registry 实例。

    def list_tools(query: str, top_k: int = 5) -> str:
        """搜索并按需加载可用工具，返回匹配工具的完整 schema JSON。

        参数:
            query: ``select:Tool1,Tool2`` 精确加载，或自然语言关键词搜索。
            top_k: 返回匹配工具的最大数量，默认 5。

        返回:
            匹配工具完整 schema 的 JSON 字符串。
        """
        try:
            results = registry.search_and_load(query, top_k=top_k)
            return json.dumps(results, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"list_tools 执行出错: {e}"

    def call_tool(name: str, arguments: dict) -> str:
        """调用一个已通过 list_tools 加载的工具。

        参数:
            name: 工具名称。
            arguments: 工具参数 dict。

        返回:
            工具执行结果字符串。
        """
        try:
            return registry.execute_tool(name, arguments or {})
        except Exception as e:
            return f"call_tool 执行出错: {e}"

    registry.register_core(
        name="tool_list",
        description=(
            "搜索并按需加载可用工具。精确匹配用 select:ToolName1,ToolName2，"
            "或输入自然语言关键词搜索。返回工具的完整 schema。"
            "注:skill__* / mcp__* 已直接注册到 Core Tier,无需通过本工具加载。"
            "本工具仅用于未来动态发现的 Deferred 工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "select:Tool1,Tool2 精确加载，或关键词搜索",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回匹配工具的最大数量，默认 5。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=list_tools,
    )

    registry.register_core(
        name="tool_call",
        description=(
            "调用一个已通过 list_tools 加载的工具。如果工具未加载,先调用 list_tools。"
            "注:skill__* / mcp__* 可直接调用,无需通过本工具中转。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工具名称"},
                "arguments": {"type": "object", "description": "工具参数"},
            },
            "required": ["name", "arguments"],
        },
        handler=call_tool,
    )


__all__ = [
    # BUILTIN_TOOLS + 注册器（本模块定义）
    "BUILTIN_TOOLS",
    "register_builtin_tools",
    # file_tools
    "read_file",
    "write_file",
    "delete_file",
    "list_directory",
    "file_edit",
    "file_glob",
    "file_grep",
    "register_file_tools",
    # shell_tools
    "execute_command",
    "kill_running_process",
    "register_bash_tool",
    # web_tools
    "http_request",
    "web_search",
    # memory_tools
    "register_memory_tools",
    # plan_tools
    "register_plan_tools",
]
