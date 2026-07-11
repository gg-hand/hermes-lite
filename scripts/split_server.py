"""自动拆分 server.py 路由到 routes/ 文件。
读取 server.py,按 URL 前缀分组提取 @app.xxx 路由,生成独立路由文件。"""
import re
import os
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
SERVER_PY = SRC_DIR / "server.py"
ROUTES_DIR = SRC_DIR / "routes"

# 路由分组:(文件名, URL 前缀列表)
ROUTE_GROUPS = [
    ("chat", ["/chat", "/chat/stream", "/chat/cancel"]),
    ("config", ["/config"]),
    ("schedules", ["/schedules"]),
    ("approvals", ["/approvals"]),
    ("proposals", ["/proposals"]),
    ("memory", ["/memories", "/profile"]),
    ("health", ["/health", "/metrics", "/metrics/signals"]),
    ("files", ["/files", "/sessions/*/files", "/admin/files"]),
    ("skills", ["/skills"]),
    ("cron_tools", ["/cron_tools"]),
    ("sessions", ["/sessions"]),
    # misc: 所有其他路由
]

# 全局变量列表(从 server.py 提取)
GLOBALS = [
    "orchestrator", "session_logger", "metrics_collector", "metrics_store",
    "metrics_persist_task", "_metrics_baseline_reset", "audit_logger",
    "approval_manager", "skill_loader", "skill_tools_registered",
    "mcp_manager", "task_manager", "cron_scheduler", "proposal_store",
    "cron_tool_registry", "health_checker", "stream_manager",
    "upload_manager", "etl_engine", "file_context_injector",
]

# 常量
CONSTANTS = ["VERSION", "CONFIG_PATH", "SERVER_LOG_PATH", "SKILL_MCP_AVAILABLE",
             "SKILL_TOOLS_AVAILABLE", "FILE_MODULE_AVAILABLE"]


def read_server_py():
    """读取 server.py 全文。"""
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def find_routes(lines):
    """找到所有 @app.xxx 路由定义,返回 [(line_idx, decorator_line, func_name, url), ...]"""
    routes = []
    decorator_re = re.compile(r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']')

    for i, line in enumerate(lines):
        m = decorator_re.match(line.strip())
        if m:
            method = m.group(1)
            url = m.group(2)
            # 找到函数名(下一行或装饰器后)
            func_name = ""
            for j in range(i + 1, min(i + 5, len(lines))):
                func_match = re.match(r'\s*(?:async\s+)?def\s+(\w+)', lines[j])
                if func_match:
                    func_name = func_match.group(1)
                    break
            routes.append((i, line.rstrip(), func_name, url, method))

    return routes


def extract_function_body(lines, start_idx):
    """从装饰器行开始,提取完整的函数定义(含装饰器),直到下一个 @app.xxx 或文件末尾。"""
    # 找到 def 行
    def_idx = None
    for j in range(start_idx + 1, min(start_idx + 10, len(lines))):
        if re.match(r'\s*(?:async\s+)?def\s+\w+', lines[j]):
            def_idx = j
            break

    if def_idx is None:
        return lines[start_idx]

    # 找到函数缩进
    def_line = lines[def_idx]
    func_indent = len(def_line) - len(def_line.lstrip())

    # 找到函数结束(下一个同缩进或更少的非空行,或下一个 @app.xxx)
    end_idx = len(lines)
    for k in range(def_idx + 1, len(lines)):
        line = lines[k]
        if line.strip() == "":
            continue
        # 检查是否是下一个 @app.xxx
        if re.match(r'@app\.(get|post|put|delete|patch)\(', line.strip()):
            end_idx = k
            break
        # 检查是否是同缩进或更少的顶层语句
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= func_indent and not line.strip().startswith("#"):
            # 检查不是函数体的延续
            if not line.strip().startswith(")") and not line.strip().startswith("]"):
                end_idx = k
                break

    # 包含装饰器前的空行
    body_lines = lines[start_idx:end_idx]

    # 去掉尾部空行
    while body_lines and body_lines[-1].strip() == "":
        body_lines.pop()

    return "\n".join(body_lines)


def get_route_group(url):
    """根据 URL 确定所属路由文件。"""
    for group_name, prefixes in ROUTE_GROUPS:
        for prefix in prefixes:
            if "*" in prefix:
                # 通配符匹配
                pattern = prefix.replace("*", "[^/]+")
                if re.match(f"^{pattern}", url):
                    return group_name
            elif url.startswith(prefix):
                return group_name

    # 特殊路由
    if url in ("/", "/monitor", "/scheduler", "/workflow"):
        return "misc"
    if url in ("/consolidation/flush", "/reasoning/toggle", "/reasoning/status",
               "/tools", "/audit/logs", "/recall", "/restart"):
        return "misc"
    # /chat GET 也在 misc
    return "misc"


def replace_app_with_router(code):
    """将 @app.xxx 替换为 @router.xxx"""
    return re.sub(r'@app\.(get|post|put|delete|patch)\(',
                  r'@router.\1(', code)


def generate_route_file(group_name, route_codes):
    """生成路由文件内容。"""
    # 确定该组需要的全局变量(扫描代码引用)
    needed_globals = []
    for g in GLOBALS:
        for code in route_codes:
            if re.search(r'\b' + re.escape(g) + r'\b', code):
                if g not in needed_globals:
                    needed_globals.append(g)
                break

    # 确定需要的常量
    needed_constants = []
    for c in CONSTANTS:
        for code in route_codes:
            if re.search(r'\b' + re.escape(c) + r'\b', code):
                if c not in needed_constants:
                    needed_constants.append(c)
                break

    # 确定需要的 schema 导入
    schema_imports = []
    schema_classes = {
        "ChatRequest": "chat", "CancelRequest": "chat", "ChatResponse": "chat",
        "ConfigResponse": "config", "ConfigUpdateRequest": "config", "ConfigUpdateResponse": "config",
        "ScheduleCreateRequest": "schedules", "ScheduleUpdateRequest": "schedules",
        "ScheduleListResponse": "schedules", "ScheduleResponse": "schedules",
        "ApprovalResolveRequest": "approvals", "ApprovalResolveResponse": "approvals",
        "ApprovalListItem": "approvals", "ApprovalListResponse": "approvals",
        "ProposalModifyRequest": "proposals",
        "FileUploadResponse": "files", "FileItem": "files",
        "FileListResponse": "files", "FileDeleteResponse": "files",
        "HealthResponse": "common", "SessionItem": "common", "SessionListResponse": "common",
        "SessionTitleUpdate": "common", "MessageItem": "common", "MessageListResponse": "common",
        "DeleteSessionResponse": "common", "FlushResponse": "common",
    }

    for cls, module in schema_classes.items():
        for code in route_codes:
            if re.search(r'\b' + re.escape(cls) + r'\b', code):
                import_line = f"from schemas.{module} import {cls}"
                if import_line not in schema_imports:
                    schema_imports.append(import_line)
                break

    # 确定需要的 FastAPI 导入
    fastapi_imports = ["APIRouter"]
    for imp in ["BackgroundTasks", "Body", "HTTPException", "Query", "Request",
                "FileResponse", "JSONResponse", "StreamingResponse", "UploadFile", "File"]:
        for code in route_codes:
            if re.search(r'\b' + imp + r'\b', code):
                if imp not in fastapi_imports:
                    fastapi_imports.append(imp)
                break

    # 确定需要的标准库导入 — logging 始终需要(用于 logger)
    std_imports = ["import logging"]
    for imp_name, imp_line in [
        ("json", "import json"),
        ("os", "import os"),
        ("time", "import time"),
        ("asyncio", "import asyncio"),
        ("threading", "import threading"),
        ("copy", "import copy"),
        ("shutil", "import shutil"),
        ("datetime", "from datetime import datetime, timedelta"),
        ("Path", "from pathlib import Path"),
        ("yaml", "import yaml"),
    ]:
        for code in route_codes:
            if re.search(r'\b' + imp_name + r'\b', code):
                if imp_line not in std_imports:
                    std_imports.append(imp_line)
                break

    # 确定需要的其他导入
    other_imports = []
    for imp_name, imp_line in [
        ("clear_config_cache", "from config import clear_config_cache"),
        ("get_llm_timeouts", "from config import get_llm_timeouts"),
        ("load_config", "from config import load_config"),
        ("validate_required_env_vars", "from config import validate_required_env_vars"),
        ("Orchestrator", "from orchestrator import Orchestrator"),
        ("StreamManager", "from stream_manager import StreamManager, StreamCancelled"),
        ("StreamCancelled", "from stream_manager import StreamCancelled"),
        ("BreakpointDetector", "from breakpoint_detector import BreakpointDetector"),
        ("ActivityTimeout", "from llm.client import ActivityTimeout"),
        ("MetricsCollector", "from monitoring.metrics import MetricsCollector"),
        ("MetricsStore", "from monitoring.metrics_store import MetricsStore, compute_delta"),
        ("compute_delta", "from monitoring.metrics_store import compute_delta"),
        ("HealthChecker", "from monitoring.health import HealthChecker"),
        ("AuditLogger", "from agent.audit import AuditLogger"),
        ("SessionLogger", "from storage.sqlite_log import SessionLogger"),
        ("ApprovalManager", "from agent.approval import ApprovalManager"),
        ("TaskManager", "from tasks.task_manager import TaskManager"),
        ("CronScheduler", "from tasks.scheduler import CronScheduler"),
        ("CronExpr", "from tasks.cron_expr import CronExpr"),
        ("ProposalStore", "from agent.cron_proposals import ProposalStore"),
        ("register_cron_tools", "from agent.cron_tools import register_cron_tools"),
        ("CronToolRegistry", "from agent.cron_tool_registry import CronToolRegistry"),
        ("register_write_cron_tool", "from agent.cron_tool_writer import register_write_cron_tool"),
        ("register_bash_tool", "from agent.builtin_tools import register_bash_tool"),
        ("register_skill_tools", "from agent.skill_tools import register_skill_tools"),
        ("_make_skill_activate_handler", "from agent.skill_tools import _make_skill_activate_handler"),
        ("SkillLoader", "from skill.loader import SkillLoader, load_skill_to_registry, register_skill_stub"),
        ("load_skill_to_registry", "from skill.loader import load_skill_to_registry, register_skill_stub"),
        ("register_skill_stub", "from skill.loader import register_skill_stub"),
        ("MCPServerDef", "from mcp.client import MCPServerDef"),
        ("MCPManager", "from mcp.manager import MCPManager, register_mcp_tools_to_registry"),
        ("register_mcp_tools_to_registry", "from mcp.manager import register_mcp_tools_to_registry"),
        ("UploadManager", "from files.upload_manager import UploadManager"),
        ("WaterfallParser", "from files.parser import WaterfallParser"),
        ("DocumentChunker", "from files.chunker import DocumentChunker"),
        ("ETLEngine", "from files.etl_engine import ETLEngine"),
        ("FileContextInjector", "from files.context_injector import FileContextInjector"),
        ("CronToolError", "from tasks.cron_tool_loader import CronToolError"),
        ("_CRON_TOOL_BASE_DIR", "from tasks.cron_tool_loader import DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR"),
        ("_list_pending_cron_tools", "from tasks.cron_tool_loader import list_pending_tools as _list_pending_cron_tools"),
        ("_list_active_cron_tools", "from tasks.cron_tool_loader import list_tools as _list_active_cron_tools"),
        ("_load_cron_tool", "from tasks.cron_tool_loader import load_tool as _load_cron_tool"),
    ]:
        for code in route_codes:
            if re.search(r'\b' + re.escape(imp_name) + r'\b', code):
                if imp_line not in other_imports:
                    other_imports.append(imp_line)
                break

    # 去重 other_imports: 移除被更长导入行包含的短导入行
    deduped_other = []
    for imp in other_imports:
        is_subset = False
        for other in other_imports:
            if other != imp and imp in other and len(imp) < len(other):
                is_subset = True
                break
        if not is_subset and imp not in deduped_other:
            deduped_other.append(imp)

    # 合并同模块的 schema 导入
    schema_by_module: dict[str, list[str]] = {}
    for imp in schema_imports:
        parts = imp.split(" import ")
        module = parts[0].replace("from ", "")
        cls = parts[1] if len(parts) > 1 else ""
        schema_by_module.setdefault(module, []).append(cls)
    merged_schema_imports = []
    for module, classes in schema_by_module.items():
        merged_schema_imports.append(f"from {module} import {', '.join(classes)}")

    # 生成文件内容
    lines = []
    lines.append(f'"""{group_name} 路由。从 server.py 提取。"""')
    lines.append("from __future__ import annotations")
    lines.append("")

    # 标准库导入
    for imp in std_imports:
        lines.append(imp)

    if std_imports:
        lines.append("")

    # FastAPI 导入
    lines.append("from fastapi import " + ", ".join(fastapi_imports))

    # 其他导入
    if deduped_other:
        lines.append("")
        for imp in deduped_other:
            lines.append(imp)

    # Schema 导入
    if merged_schema_imports:
        lines.append("")
        for imp in merged_schema_imports:
            lines.append(imp)

    lines.append("")
    lines.append("logger = logging.getLogger(__name__)")
    lines.append("")
    lines.append("router = APIRouter()")
    lines.append("")

    # 全局变量(由 app.py lifespan 设置)
    lines.append("# 全局组件(由 app.py lifespan 初始化)")
    for g in needed_globals:
        lines.append(f"{g} = None")
    lines.append("")

    # 需要的常量
    if needed_constants:
        lines.append("# 常量(从 server.py 复制)")
        for c in needed_constants:
            if c == "VERSION":
                lines.append(f'{c} = "0.1.0"')
            elif c == "CONFIG_PATH":
                lines.append(f'{c} = os.environ.get("HERMES_CONFIG", "config.yaml")')
            elif c == "SERVER_LOG_PATH":
                lines.append(f'{c} = os.environ.get("HERMES_SERVER_LOG", "data/server.log")')
            elif c == "SKILL_MCP_AVAILABLE":
                lines.append(f"{c} = False")
            elif c == "SKILL_TOOLS_AVAILABLE":
                lines.append(f"{c} = False")
            elif c == "FILE_MODULE_AVAILABLE":
                lines.append(f"{c} = False")
        lines.append("")

    # 路由代码
    for code in route_codes:
        # 替换 @app.xxx → @router.xxx
        code = replace_app_with_router(code)
        lines.append(code)
        lines.append("")

    return "\n".join(lines)


def main():
    """主函数:读取 server.py,拆分路由,生成 routes/ 文件。"""
    content = read_server_py()
    lines = content.split("\n")

    # 找到所有路由
    routes = find_routes(lines)
    print(f"找到 {len(routes)} 个路由")

    # 按组分类
    groups: dict[str, list[str]] = {}
    for line_idx, decorator, func_name, url, method in routes:
        group = get_route_group(url)
        func_code = extract_function_body(lines, line_idx)
        if group not in groups:
            groups[group] = []
        groups[group].append(func_code)

    # 创建 routes/ 目录
    ROUTES_DIR.mkdir(exist_ok=True)

    # 生成路由文件
    for group_name, route_codes in groups.items():
        file_content = generate_route_file(group_name, route_codes)
        output_path = ROUTES_DIR / f"{group_name}.py"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(file_content)
        print(f"  生成 {output_path.name}: {len(route_codes)} 个路由")

    # 生成 __init__.py
    init_content = '"""路由模块。"""\n'
    with open(ROUTES_DIR / "__init__.py", "w", encoding="utf-8") as f:
        f.write(init_content)
    print(f"  生成 __init__.py")

    print(f"\n完成!共生成 {len(groups)} 个路由文件")


if __name__ == "__main__":
    main()
