"""Phase 8 Task 5: cron_tool 动态工具系统测试套件（SubTask 5.10）。

覆盖：
- ``cron_tool_loader``：TOOL.md 解析 + 子进程执行 + 列表辅助（SubTask 5.2）
- ``CronToolRegistry``：独立注册中心的增删改查与执行（SubTask 5.3）
- ``write_cron_tool``：LLM 工具写入 ``.pending/`` 与校验（SubTask 5.4）
- 缓存约束端到端验证（SubTask 5.7 + 5.10）：
  - 用户会话 ``_build_enhanced_context`` 返回 ``tools_override=None``
  - cron 会话 ``_build_cron_tools`` 按 ``active_tools_snapshot`` 请求级过滤
  - cron_tool 激活/更新后全局 ToolRegistry schema 字节级稳定
  - ``ReactLoop._execute_tool_with_dispatch`` 优先派发 cron_tool_registry

运行方式:
    python -m unittest tests.test_cron_tool -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.cron_tool_registry import CronToolRegistry  # noqa: E402
from src.tasks.cron_tool_loader import (  # noqa: E402
    CronToolError,
    CronToolNotFoundError,
    CronToolParseError,
    CronToolRunScriptNotFoundError,
    DEFAULT_BASE_DIR,
    DEFAULT_TIMEOUT,
    execute_tool,
    list_pending_tools,
    list_tools,
    load_tool,
)
from src.agent.cron_tool_writer import register_write_cron_tool  # noqa: E402
from src.agent.context_builder import ContextBuilder  # noqa: E402
from src.agent.cron_isolator import CronIsolator  # noqa: E402


# ---------------------------------------------------------------------------
# 测试夹具：在临时目录构造 cron_tool 工具
# ---------------------------------------------------------------------------


_ECHO_TOOL_MD = """---
name: echo_text
version: 1.0.0
description: 回显输入文本（测试用）
author: test
timeout: 5
input_schema:
  type: object
  properties:
    text:
      type: string
      description: 待回显文本
  required: [text]
---

# echo_text
测试用 cron_tool。
"""

_ECHO_RUN_PY = """#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read() or "{}")
text = (payload.get("input") or {}).get("text", "")
print(json.dumps({"result": f"echo: {text}"}, ensure_ascii=False))
"""

_BAD_JSON_RUN_PY = """#!/usr/bin/env python3
print("not a json")
"""


def _make_tool(base_dir: str, name: str, tool_md: str = _ECHO_TOOL_MD,
               run_script: str = _ECHO_RUN_PY,
               run_ext: str = ".py", pending: bool = False) -> Path:
    """在 base_dir 下创建一个 cron_tool 目录（含 TOOL.md + run.*）。

    参数:
        base_dir: cron_tool 根目录。
        name: 工具名（目录名）。TOOL.md frontmatter 的 name 字段会被替换为此值。
        tool_md: TOOL.md 全文（默认 _ECHO_TOOL_MD，其中 name 字段会被替换）。
        run_script: run.* 脚本内容。
        run_ext: 脚本扩展名（.py/.sh/.js）。
        pending: 为 True 时写入 ``.pending/{name}/``，否则写入 ``{name}/``。

    返回:
        工具目录 Path。
    """
    # 确保 TOOL.md frontmatter 的 name 字段与目录名一致
    effective_md = tool_md.replace("name: echo_text", f"name: {name}")
    if f"name: {name}" not in effective_md:
        # 兜底：原模板不含 echo_text 时直接替换首行 name 字段
        import re
        effective_md = re.sub(r"^name: .*$", f"name: {name}", effective_md,
                              count=1, flags=re.MULTILINE)
    if pending:
        root = Path(base_dir) / ".pending" / name
    else:
        root = Path(base_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "TOOL.md").write_text(effective_md, encoding="utf-8")
    (root / f"run{run_ext}").write_text(run_script, encoding="utf-8")
    return root


class _CronToolSandbox:
    """临时目录沙箱：构造独立的 cron_tool 根目录，测试后清理。"""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cron_tool_test_")
        self.base_dir = os.path.join(self.tmpdir, "cron_tool")
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(os.path.join(self.base_dir, ".pending"), exist_ok=True)

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_tool(self, name, tool_md=_ECHO_TOOL_MD, run_script=_ECHO_RUN_PY,
                  run_ext=".py", pending=False):
        return _make_tool(self.base_dir, name, tool_md, run_script, run_ext,
                          pending)


# ---------------------------------------------------------------------------
# SubTask 5.2: cron_tool_loader
# ---------------------------------------------------------------------------


class TestCronToolLoaderLoad(unittest.TestCase):
    """load_tool 解析 TOOL.md frontmatter。"""

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        self.sandbox.make_tool("echo_text")

    def tearDown(self):
        self.sandbox.cleanup()

    def test_load_tool_returns_meta(self):
        """load_tool 成功解析 TOOL.md，返回 CronToolMeta。"""
        meta = load_tool("echo_text", base_dir=self.sandbox.base_dir)
        self.assertEqual(meta.name, "echo_text")
        self.assertEqual(meta.version, "1.0.0")
        self.assertEqual(meta.description, "回显输入文本（测试用）")
        self.assertEqual(meta.author, "test")
        self.assertEqual(meta.timeout, 5)
        self.assertIsInstance(meta.input_schema, dict)
        self.assertIn("text", meta.input_schema["properties"])
        self.assertTrue(meta.run_script.endswith("run.py"))
        self.assertGreaterEqual(len(meta.run_interpreter), 1)

    def test_load_tool_timeout_fallback(self):
        """timeout 为 None 或非正数时 get_timeout 回退到 default。"""
        meta = load_tool("echo_text", base_dir=self.sandbox.base_dir)
        meta.timeout = None
        self.assertEqual(meta.get_timeout(), DEFAULT_TIMEOUT)
        meta.timeout = 0
        self.assertEqual(meta.get_timeout(), DEFAULT_TIMEOUT)
        meta.timeout = -5
        self.assertEqual(meta.get_timeout(), DEFAULT_TIMEOUT)

    def test_load_tool_to_schema_format(self):
        """to_schema 返回 Anthropic tool use 格式（name/description/input_schema）。"""
        meta = load_tool("echo_text", base_dir=self.sandbox.base_dir)
        schema = meta.to_schema()
        self.assertEqual(set(schema.keys()), {"name", "description", "input_schema"})
        self.assertEqual(schema["name"], "echo_text")

    def test_load_tool_not_found(self):
        """工具目录不存在抛 CronToolNotFoundError。"""
        with self.assertRaises(CronToolNotFoundError):
            load_tool("nonexistent_tool", base_dir=self.sandbox.base_dir)

    def test_load_tool_invalid_name_with_slash(self):
        """工具名含 / 抛 CronToolParseError（防路径穿越）。"""
        with self.assertRaises(CronToolParseError):
            load_tool("evil/path", base_dir=self.sandbox.base_dir)

    def test_load_tool_invalid_name_dot_prefix(self):
        """工具名以 . 开头抛 CronToolParseError（防穿越）。"""
        with self.assertRaises(CronToolParseError):
            load_tool(".hidden", base_dir=self.sandbox.base_dir)

    def test_load_tool_empty_name(self):
        """空工具名抛 CronToolParseError。"""
        with self.assertRaises(CronToolParseError):
            load_tool("", base_dir=self.sandbox.base_dir)

    def test_load_tool_missing_tool_md(self):
        """TOOL.md 缺失抛 CronToolParseError。"""
        # 创建只有 run.py 的目录
        bad_dir = Path(self.sandbox.base_dir) / "no_md"
        bad_dir.mkdir()
        (bad_dir / "run.py").write_text("print('hi')", encoding="utf-8")
        with self.assertRaises(CronToolParseError):
            load_tool("no_md", base_dir=self.sandbox.base_dir)

    def test_load_tool_missing_run_script(self):
        """run.* 脚本缺失抛 CronToolRunScriptNotFoundError。"""
        bad_dir = Path(self.sandbox.base_dir) / "no_run"
        bad_dir.mkdir()
        (bad_dir / "TOOL.md").write_text(_ECHO_TOOL_MD, encoding="utf-8")
        with self.assertRaises(CronToolRunScriptNotFoundError):
            load_tool("no_run", base_dir=self.sandbox.base_dir)

    def test_load_tool_bad_frontmatter_yaml(self):
        """frontmatter yaml 非法抛 CronToolParseError。"""
        bad_md = "---\nname: bad\n  invalid yaml: [\n---\n# bad\n"
        self.sandbox.make_tool("bad_yaml", tool_md=bad_md)
        with self.assertRaises(CronToolParseError):
            load_tool("bad_yaml", base_dir=self.sandbox.base_dir)

    def test_load_tool_missing_required_field(self):
        """frontmatter 缺少必填字段（input_schema）抛 CronToolParseError。"""
        bad_md = "---\nname: nofield\nversion: 1.0\ndescription: test\n---\n# no field\n"
        self.sandbox.make_tool("no_field", tool_md=bad_md)
        with self.assertRaises(CronToolParseError):
            load_tool("no_field", base_dir=self.sandbox.base_dir)


class TestCronToolLoaderExecute(unittest.TestCase):
    """execute_tool 子进程执行。"""

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        self.sandbox.make_tool("echo_text")

    def tearDown(self):
        self.sandbox.cleanup()

    def test_execute_tool_success(self):
        """成功执行返回 result 字符串。"""
        result = execute_tool(
            name="echo_text",
            input={"text": "hello"},
            base_dir=self.sandbox.base_dir,
        )
        self.assertEqual(result, "echo: hello")

    def test_execute_tool_with_context(self):
        """传入 context 不影响执行（echo 忽略 context）。"""
        result = execute_tool(
            name="echo_text",
            input={"text": "world"},
            context={"session_id": "cron:s1", "schedule_id": "s1"},
            base_dir=self.sandbox.base_dir,
        )
        self.assertEqual(result, "echo: world")

    def test_execute_tool_empty_input(self):
        """空 input dict 正常执行（text 默认空字符串）。"""
        result = execute_tool(
            name="echo_text",
            input={},
            base_dir=self.sandbox.base_dir,
        )
        self.assertEqual(result, "echo: ")

    def test_execute_tool_load_error_returns_error_json(self):
        """工具不存在返回结构化错误 JSON 字符串（不抛异常）。"""
        result = execute_tool(
            name="nonexistent",
            input={},
            base_dir=self.sandbox.base_dir,
        )
        self.assertIn("error", result)
        self.assertIn("load_error", result)
        # 错误 JSON 必须可解析
        parsed = json.loads(result)
        self.assertEqual(parsed["error_type"], "load_error")

    def test_execute_tool_timeout(self):
        """超时返回 timeout 错误 JSON。"""
        # 构造一个 sleep 的脚本
        sleep_script = (
            "import time, sys\n"
            "time.sleep(10)\n"
            "print(json.dumps({'result': 'done'}))\n"
        )
        self.sandbox.make_tool("slow_tool", run_script=sleep_script)
        result = execute_tool(
            name="slow_tool",
            input={},
            timeout=1,
            base_dir=self.sandbox.base_dir,
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["error_type"], "timeout")
        self.assertIn("超时", parsed["error"])

    def test_execute_tool_bad_stdout_json(self):
        """子进程 stdout 非 JSON 返回 output_parse_error。"""
        self.sandbox.make_tool("bad_json", run_script=_BAD_JSON_RUN_PY)
        result = execute_tool(
            name="bad_json",
            input={},
            base_dir=self.sandbox.base_dir,
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["error_type"], "output_parse_error")

    def test_execute_tool_crash_no_stdout(self):
        """子进程崩溃（非零退出 + 无 stdout）返回 crash 错误。"""
        crash_script = "import sys; sys.exit(1)\n"
        self.sandbox.make_tool("crash_tool", run_script=crash_script)
        result = execute_tool(
            name="crash_tool",
            input={},
            base_dir=self.sandbox.base_dir,
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["error_type"], "crash")

    def test_execute_tool_proactive_error(self):
        """子进程主动输出 {error: ...} 返回 tool_error。"""
        err_script = (
            "import json\n"
            "print(json.dumps({'error': 'something went wrong', 'error_type': 'tool_error'}))\n"
        )
        self.sandbox.make_tool("err_tool", run_script=err_script)
        result = execute_tool(
            name="err_tool",
            input={},
            base_dir=self.sandbox.base_dir,
        )
        parsed = json.loads(result)
        self.assertIn("error", parsed)
        self.assertEqual(parsed["error"], "something went wrong")

    def test_execute_tool_missing_result_field(self):
        """stdout JSON 缺 result/error 字段返回 output_format_error。"""
        empty_script = "import json\nprint(json.dumps({}))\n"
        self.sandbox.make_tool("empty_tool", run_script=empty_script)
        result = execute_tool(
            name="empty_tool",
            input={},
            base_dir=self.sandbox.base_dir,
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["error_type"], "output_format_error")

    def test_execute_tool_with_preloaded_meta(self):
        """传入 meta 参数避免重复解析 TOOL.md。"""
        meta = load_tool("echo_text", base_dir=self.sandbox.base_dir)
        result = execute_tool(
            name="echo_text",
            input={"text": "cached"},
            base_dir=self.sandbox.base_dir,
            meta=meta,
        )
        self.assertEqual(result, "echo: cached")


class TestCronToolLoaderList(unittest.TestCase):
    """list_tools / list_pending_tools 列表辅助。"""

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        self.sandbox.make_tool("alpha")
        self.sandbox.make_tool("beta")
        self.sandbox.make_tool("gamma_pending", pending=True)
        self.sandbox.make_tool("delta_pending", pending=True)

    def tearDown(self):
        self.sandbox.cleanup()

    def test_list_tools_excludes_pending_and_hidden(self):
        """list_tools 返回已激活工具，跳过 .pending 与隐藏目录。"""
        names = list_tools(base_dir=self.sandbox.base_dir)
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        self.assertNotIn("gamma_pending", names)
        self.assertNotIn("delta_pending", names)
        # 按字典序排序
        self.assertEqual(names, sorted(names))

    def test_list_tools_empty_base_dir(self):
        """base_dir 不存在返回空列表。"""
        self.assertEqual(list_tools(base_dir="/nonexistent/path/xyz"), [])

    def test_list_pending_tools(self):
        """list_pending_tools 返回 .pending/ 下的工具名。"""
        names = list_pending_tools(base_dir=self.sandbox.base_dir)
        self.assertIn("gamma_pending", names)
        self.assertIn("delta_pending", names)
        self.assertNotIn("alpha", names)
        self.assertEqual(names, sorted(names))

    def test_list_pending_tools_no_pending_dir(self):
        """.pending 目录不存在返回空列表。"""
        empty_sandbox = _CronToolSandbox()
        try:
            # 删除 .pending 目录
            shutil.rmtree(os.path.join(empty_sandbox.base_dir, ".pending"))
            self.assertEqual(list_pending_tools(base_dir=empty_sandbox.base_dir), [])
        finally:
            empty_sandbox.cleanup()


# ---------------------------------------------------------------------------
# SubTask 5.3: CronToolRegistry
# ---------------------------------------------------------------------------


class TestCronToolRegistry(unittest.TestCase):
    """CronToolRegistry 增删改查与执行。"""

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        self.sandbox.make_tool("echo_text")
        self.sandbox.make_tool("second_tool", tool_md=_ECHO_TOOL_MD.replace(
            "echo_text", "second_tool").replace("回显输入文本", "第二个工具"))
        self.registry = CronToolRegistry(base_dir=self.sandbox.base_dir)

    def tearDown(self):
        self.sandbox.cleanup()

    def test_register_returns_meta(self):
        """register 解析 TOOL.md 并返回 CronToolMeta。"""
        meta = self.registry.register("echo_text")
        self.assertEqual(meta.name, "echo_text")
        self.assertTrue(self.registry.has_tool("echo_text"))

    def test_get_tools_schema_format(self):
        """get_tools_schema 返回 Anthropic 格式 schema 列表。"""
        self.registry.register("echo_text")
        schemas = self.registry.get_tools_schema()
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["name"], "echo_text")
        self.assertIn("input_schema", schemas[0])

    def test_get_tools_schema_multiple(self):
        """多工具注册后 schema 列表按注册顺序。"""
        self.registry.register("echo_text")
        self.registry.register("second_tool")
        schemas = self.registry.get_tools_schema()
        self.assertEqual(len(schemas), 2)
        self.assertEqual(schemas[0]["name"], "echo_text")
        self.assertEqual(schemas[1]["name"], "second_tool")

    def test_unregister_removes_tool(self):
        """unregister 移除工具（仅内存，不删磁盘）。"""
        self.registry.register("echo_text")
        self.assertTrue(self.registry.unregister("echo_text"))
        self.assertFalse(self.registry.has_tool("echo_text"))
        # 磁盘文件仍在
        tool_dir = Path(self.sandbox.base_dir) / "echo_text"
        self.assertTrue(tool_dir.exists())

    def test_unregister_not_registered_returns_false(self):
        """unregister 未注册工具返回 False。"""
        self.assertFalse(self.registry.unregister("nonexistent"))

    def test_reload_overrides_meta(self):
        """reload 重新解析 TOOL.md（覆盖式注册）。"""
        self.registry.register("echo_text")
        # 修改 TOOL.md
        tool_md = _ECHO_TOOL_MD.replace("1.0.0", "2.0.0")
        _make_tool(self.sandbox.base_dir, "echo_text", tool_md=tool_md,
                   run_script=_ECHO_RUN_PY)
        meta = self.registry.reload("echo_text")
        self.assertEqual(meta.version, "2.0.0")

    def test_load_all_skips_broken(self):
        """load_all 单工具损坏不影响其他工具加载。"""
        # 添加一个损坏的工具（无 run.*）
        bad_dir = Path(self.sandbox.base_dir) / "broken"
        bad_dir.mkdir()
        (bad_dir / "TOOL.md").write_text(_ECHO_TOOL_MD.replace(
            "echo_text", "broken"), encoding="utf-8")
        loaded = self.registry.load_all()
        # echo_text 与 second_tool 加载成功，broken 跳过
        self.assertIn("echo_text", loaded)
        self.assertIn("second_tool", loaded)
        self.assertNotIn("broken", loaded)

    def test_execute_tool_success(self):
        """execute_tool 通过子进程执行返回结果。"""
        self.registry.register("echo_text")
        result = self.registry.execute_tool("echo_text", {"text": "via_registry"})
        self.assertEqual(result, "echo: via_registry")

    def test_execute_tool_not_registered(self):
        """execute_tool 未注册工具返回错误信息字符串。"""
        result = self.registry.execute_tool("nonexistent", {})
        self.assertIn("未注册", result)

    def test_get_tool_handler_returns_callable(self):
        """get_tool_handler 返回 handler callable。"""
        self.registry.register("echo_text")
        handler = self.registry.get_tool_handler("echo_text")
        self.assertIsNotNone(handler)
        self.assertTrue(callable(handler))
        result = handler(text="via_handler")
        self.assertEqual(result, "echo: via_handler")

    def test_get_tool_handler_not_registered(self):
        """get_tool_handler 未注册返回 None。"""
        self.assertIsNone(self.registry.get_tool_handler("nonexistent"))

    def test_get_tool_meta(self):
        """get_tool_meta 返回 CronToolMeta。"""
        self.registry.register("echo_text")
        meta = self.registry.get_tool_meta("echo_text")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.name, "echo_text")

    def test_list_tool_names(self):
        """list_tool_names 按注册顺序返回。"""
        self.registry.register("echo_text")
        self.registry.register("second_tool")
        self.assertEqual(self.registry.list_tool_names(), ["echo_text", "second_tool"])

    def test_set_context_provider_injects_context(self):
        """set_context_provider 注入的回调在 handler 执行时被调用。"""
        self.registry.register("echo_text")
        captured = {}

        def provider():
            captured["called"] = True
            return {"session_id": "cron:test", "schedule_id": "test"}

        self.registry.set_context_provider(provider)
        self.registry.execute_tool("echo_text", {"text": "ctx"})
        self.assertTrue(captured.get("called"))


# ---------------------------------------------------------------------------
# SubTask 5.4: write_cron_tool
# ---------------------------------------------------------------------------


class TestWriteCronTool(unittest.TestCase):
    """write_cron_tool 写入 .pending/ 与校验。"""

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        # 构造 mock ToolRegistry
        self.registered_tools = {}

        class MockToolRegistry:
            def register_core(inner_self, name, description, input_schema, handler):
                self.registered_tools[name] = {
                    "description": description,
                    "input_schema": input_schema,
                    "handler": handler,
                }

        self.mock_registry = MockToolRegistry()
        register_write_cron_tool(self.mock_registry, base_dir=self.sandbox.base_dir)

    def tearDown(self):
        self.sandbox.cleanup()

    def _call_write_tool(self, **kwargs):
        """调用 write_cron_tool handler。"""
        handler = self.registered_tools["cron_tool_create"]["handler"]
        return handler(**kwargs)

    def test_write_cron_tool_registered(self):
        """write_cron_tool 已注册到 mock registry。"""
        self.assertIn("cron_tool_create", self.registered_tools)
        schema = self.registered_tools["cron_tool_create"]["input_schema"]
        self.assertIn("tool_name", schema["properties"])
        self.assertIn("tool_md", schema["properties"])
        self.assertIn("run_script", schema["properties"])

    def test_write_tool_success_writes_pending(self):
        """成功写入 .pending/{name}/TOOL.md + run.py。"""
        result = self._call_write_tool(
            tool_name="new_tool",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "new_tool"),
            run_script=_ECHO_RUN_PY,
            llm_explanation="测试工具",
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "pending_review")
        self.assertTrue(parsed["pending_review"])
        self.assertEqual(parsed["tool_name"], "new_tool")
        # 文件已写入 .pending/
        pending_dir = Path(self.sandbox.base_dir) / ".pending" / "new_tool"
        self.assertTrue((pending_dir / "TOOL.md").exists())
        self.assertTrue((pending_dir / "run.py").exists())

    def test_write_tool_invalid_name(self):
        """非法工具名返回错误字符串。"""
        result = self._call_write_tool(
            tool_name="evil/path",
            tool_md=_ECHO_TOOL_MD,
            run_script=_ECHO_RUN_PY,
        )
        self.assertIn("错误", result)
        self.assertIn("tool_name", result)

    def test_write_tool_name_mismatch(self):
        """tool_md frontmatter.name 与 tool_name 不一致返回错误。"""
        result = self._call_write_tool(
            tool_name="alpha",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "beta"),
            run_script=_ECHO_RUN_PY,
        )
        self.assertIn("错误", result)
        self.assertIn("一致", result)

    def test_write_tool_bad_frontmatter(self):
        """tool_md 缺少 frontmatter 开头返回错误。"""
        result = self._call_write_tool(
            tool_name="bad",
            tool_md="# no frontmatter\njust text",
            run_script=_ECHO_RUN_PY,
        )
        self.assertIn("错误", result)
        self.assertIn("frontmatter", result)

    def test_write_tool_empty_run_script(self):
        """run_script 为空返回错误。"""
        result = self._call_write_tool(
            tool_name="empty",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "empty"),
            run_script="",
        )
        self.assertIn("错误", result)
        self.assertIn("不能为空", result)

    def test_write_tool_invalid_run_ext(self):
        """run_ext 不在支持列表返回错误。"""
        result = self._call_write_tool(
            tool_name="badext",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "badext"),
            run_script=_ECHO_RUN_PY,
            run_ext=".rb",
        )
        self.assertIn("错误", result)

    def test_write_tool_overwrite_pending_protection(self):
        """同名工具已在 .pending/ 返回错误（覆盖保护）。"""
        # 先写入一次
        self._call_write_tool(
            tool_name="dup",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "dup"),
            run_script=_ECHO_RUN_PY,
        )
        # 再次写入同名
        result = self._call_write_tool(
            tool_name="dup",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "dup"),
            run_script=_ECHO_RUN_PY,
        )
        self.assertIn("错误", result)
        self.assertIn("已存在", result)

    def test_write_tool_overwrite_active_protection(self):
        """同名工具已激活（在 cron_tool/ 下）返回错误。"""
        # 先创建已激活工具
        self.sandbox.make_tool("active_dup")
        result = self._call_write_tool(
            tool_name="active_dup",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "active_dup"),
            run_script=_ECHO_RUN_PY,
        )
        self.assertIn("错误", result)
        self.assertIn("已存在", result)

    def test_write_tool_supports_sh_ext(self):
        """run_ext=.sh 写入 run.sh。"""
        sh_script = "#!/bin/bash\necho '{\"result\": \"from sh\"}'\n"
        result = self._call_write_tool(
            tool_name="sh_tool",
            tool_md=_ECHO_TOOL_MD.replace("echo_text", "sh_tool"),
            run_script=sh_script,
            run_ext=".sh",
        )
        parsed = json.loads(result)
        self.assertTrue(parsed["pending_review"])
        run_file = Path(self.sandbox.base_dir) / ".pending" / "sh_tool" / "run.sh"
        self.assertTrue(run_file.exists())


# ---------------------------------------------------------------------------
# SubTask 5.7 + 5.10: 缓存约束端到端验证
# ---------------------------------------------------------------------------


class _MockToolRegistryForCache:
    """缓存约束测试专用 mock ToolRegistry。

    返回固定的 schema 列表，便于字节级比对。
    """

    def __init__(self, schemas=None):
        self._schemas = schemas or [
            {"name": "builtin_a", "description": "内置工具A", "input_schema": {"type": "object"}},
            {"name": "builtin_b", "description": "内置工具B", "input_schema": {"type": "object"}},
        ]

    def get_tools_schema(self):
        # 返回深拷贝避免测试间污染
        import copy
        return copy.deepcopy(self._schemas)

    def execute_tool(self, name, tool_input):
        return f"global_exec:{name}"


class _MockCronSchedulerForCache:
    """缓存约束测试专用 mock CronScheduler。

    按 cron_id 返回调度项 dict（含 active_tools_snapshot）。
    """

    def __init__(self, schedules=None):
        self._schedules = schedules or {}

    def get_schedule(self, cron_id):
        return self._schedules.get(cron_id)


class _MockReactLoopForCache:
    """缓存约束测试专用 mock ReactLoop，记录 tools_override 参数。"""

    def __init__(self):
        self.cron_tool_registry = None  # 由 _build_cron_tools 注入
        self.last_tools_override = "NOT_CALLED"
        self.tool_registry = None

    def run(self, user_input=None, history=None, system=None, session_id=None,
            tools_override=None, **kwargs):
        self.last_tools_override = tools_override
        # ReactLoop.run 返回四元组 (response, messages, is_complete, termination_reason)
        return "mock response", [], True, "normal"

    def run_stream(self, user_input=None, history=None, system=None, session_id=None,
                   tools_override=None, **kwargs):
        self.last_tools_override = tools_override
        yield "mock chunk"


class TestCacheConstraintEndToEnd(unittest.IsolatedAsyncioTestCase):
    """缓存约束端到端验证（SubTask 5.10 核心）。

    验证：
    - 用户会话 tools_override 始终为 None（字节级稳定）
    - cron 会话按 active_tools_snapshot 请求级过滤
    - cron_tool 激活/更新不改变全局 ToolRegistry schema

    注：``_build_enhanced_context`` 已 async（Phase 10 异步化改造），
    本类用 IsolatedAsyncioTestCase；仅调用 _build_enhanced_context 的用例
    改为 async def + await，其余同步用例（仅调用 _build_cron_tools 等
    同步方法）保持原样。
    """

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        self.sandbox.make_tool("cron_echo")
        # 全局 registry schema（固定，用于字节级比对）
        self.global_schemas = [
            {"name": "builtin_a", "description": "内置A", "input_schema": {"type": "object"}},
            {"name": "builtin_b", "description": "内置B", "input_schema": {"type": "object"}},
        ]
        self.global_registry = _MockToolRegistryForCache(self.global_schemas)
        self.cron_tool_registry = CronToolRegistry(base_dir=self.sandbox.base_dir)
        self.react_loop = _MockReactLoopForCache()
        # 构造 Orchestrator（绕过完整初始化，只设置需要的属性）
        from src.orchestrator import Orchestrator
        from src.orchestrator.enhanced_context import EnhancedContextBuilder
        self.orch = Orchestrator.__new__(Orchestrator)
        self.orch.tool_registry = self.global_registry
        self.orch.react_loop = self.react_loop
        self.orch.cron_scheduler = None
        self.orch.cron_tool_registry = None
        # _build_enhanced_context 访问的可选组件（设为 None 走降级路径）
        self.orch.context_manager = None
        self.orch.memory_retriever = None
        self.orch.task_manager = None
        self.orch.metrics = None
        # Phase 9 Task 5: _build_enhanced_context 现访问 todo_registry，
        # 此测试不验证 plan 模式注入，置 None 走降级路径。
        self.orch.todo_registry = None
        # 委托管理器（方法对象模式，持有 orch 引用）
        self.orch.context_builder = ContextBuilder()
        self.orch.cron_isolator = CronIsolator(orchestrator=self.orch)
        self.orch.enhanced_context_builder = EnhancedContextBuilder(self.orch)

    def tearDown(self):
        self.sandbox.cleanup()

    async def test_user_session_returns_none_tools_override(self):
        """用户会话 _build_enhanced_context 返回 tools_override=None。"""
        system_text, history, tools_override = await self.orch.enhanced_context_builder.build(
            session_id="user_session_123",
            user_input="hello",
            history=[],
        )
        self.assertIsNone(tools_override)

    async def test_cron_session_without_deps_returns_none(self):
        """cron 会话但 cron_scheduler 未注入时返回 None（降级）。"""
        system_text, history, tools_override = await self.orch.enhanced_context_builder.build(
            session_id="cron:sched1",
            user_input="hello",
            history=[],
        )
        # cron_scheduler 为 None，降级返回 None
        self.assertIsNone(tools_override)

    def test_cron_session_with_snapshot_filters(self):
        """cron 会话按 active_tools_snapshot 过滤内置工具集。"""
        # 注入 cron 依赖
        sched = {"active_tools_snapshot": ["builtin_a"]}
        self.orch.cron_scheduler = _MockCronSchedulerForCache({"sched1": sched})
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=self.cron_tool_registry,
        )
        # 注册一个 cron_tool
        self.cron_tool_registry.register("cron_echo")

        tools_override = self.orch.cron_isolator.build_cron_tools("cron:sched1")
        self.assertIsNotNone(tools_override)
        names = [t["name"] for t in tools_override]
        # snapshot 只允许 builtin_a
        self.assertIn("builtin_a", names)
        self.assertNotIn("builtin_b", names)
        # cron_tool 始终可见（不受 snapshot 限制）
        self.assertIn("cron_echo", names)

    def test_cron_session_empty_snapshot_returns_all(self):
        """active_tools_snapshot 为空时返回完整内置工具集 + cron_tool。"""
        sched = {"active_tools_snapshot": []}
        self.orch.cron_scheduler = _MockCronSchedulerForCache({"sched2": sched})
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=self.cron_tool_registry,
        )
        self.cron_tool_registry.register("cron_echo")

        tools_override = self.orch.cron_isolator.build_cron_tools("cron:sched2")
        names = [t["name"] for t in tools_override]
        self.assertIn("builtin_a", names)
        self.assertIn("builtin_b", names)
        self.assertIn("cron_echo", names)

    def test_cron_session_none_snapshot_returns_all(self):
        """active_tools_snapshot 为 None 时返回完整工具集 + cron_tool。"""
        sched = {"active_tools_snapshot": None}
        self.orch.cron_scheduler = _MockCronSchedulerForCache({"sched3": sched})
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=self.cron_tool_registry,
        )
        self.cron_tool_registry.register("cron_echo")

        tools_override = self.orch.cron_isolator.build_cron_tools("cron:sched3")
        self.assertEqual(len(tools_override), 3)  # 2 内置 + 1 cron_tool

    def test_cron_session_schedule_not_found_returns_none(self):
        """调度项不存在返回 None（用完整工具集，向后兼容）。"""
        self.orch.cron_scheduler = _MockCronSchedulerForCache({})  # 空映射
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=self.cron_tool_registry,
        )
        tools_override = self.orch.cron_isolator.build_cron_tools("cron:nonexistent")
        self.assertIsNone(tools_override)

    def test_global_registry_byte_stable_after_cron_tool_register(self):
        """cron_tool 注册后全局 ToolRegistry schema 字节级不变（缓存硬约束 1）。"""
        # 取注册前的全局 schema 快照
        before = self.global_registry.get_tools_schema()
        # 注册新 cron_tool
        self.sandbox.make_tool("new_cron_tool", tool_md=_ECHO_TOOL_MD.replace(
            "echo_text", "new_cron_tool"))
        self.cron_tool_registry.register("new_cron_tool")
        # 取注册后的全局 schema
        after = self.global_registry.get_tools_schema()
        # 字节级一致（缓存硬约束 1）
        self.assertEqual(before, after)
        # 全局 schema 不含 cron_tool
        names = [t["name"] for t in after]
        self.assertNotIn("new_cron_tool", names)
        self.assertNotIn("cron_echo", names)

    def test_global_registry_byte_stable_after_cron_tool_unregister(self):
        """cron_tool 注销后全局 ToolRegistry schema 字节级不变。"""
        self.sandbox.make_tool("temp_tool", tool_md=_ECHO_TOOL_MD.replace(
            "echo_text", "temp_tool"))
        self.cron_tool_registry.register("temp_tool")
        before = self.global_registry.get_tools_schema()
        self.cron_tool_registry.unregister("temp_tool")
        after = self.global_registry.get_tools_schema()
        self.assertEqual(before, after)

    def test_global_registry_byte_stable_after_cron_tool_reload(self):
        """cron_tool reload 后全局 ToolRegistry schema 字节级不变。"""
        self.cron_tool_registry.register("cron_echo")
        before = self.global_registry.get_tools_schema()
        # 修改 TOOL.md 后 reload
        updated_md = _ECHO_TOOL_MD.replace("1.0.0", "2.0.0")
        _make_tool(self.sandbox.base_dir, "cron_echo", tool_md=updated_md,
                   run_script=_ECHO_RUN_PY)
        self.cron_tool_registry.reload("cron_echo")
        after = self.global_registry.get_tools_schema()
        self.assertEqual(before, after)

    async def test_react_loop_receives_tools_override(self):
        """ReactLoop.run 收到 tools_override 参数（非 None）。"""
        sched = {"active_tools_snapshot": ["builtin_a"]}
        self.orch.cron_scheduler = _MockCronSchedulerForCache({"sched4": sched})
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=self.cron_tool_registry,
        )
        self.cron_tool_registry.register("cron_echo")

        # 模拟 chat() 路径：构建 context + 调用 react_loop.run
        # 注：_build_enhanced_context 已 async（Phase 10），需 await。
        # _MockReactLoopForCache.run 保持同步（测试直接调用，不经生产代码）。
        system_text, history, tools_override = await self.orch.enhanced_context_builder.build(
            session_id="cron:sched4", user_input="hi", history=[]
        )
        self.assertIsNotNone(tools_override)
        self.orch.react_loop.run(
            user_input="hi",
            history=history,
            system=system_text,
            session_id="cron:sched4",
            tools_override=tools_override,
        )
        # ReactLoop 收到了非 None 的 tools_override
        self.assertIsNotNone(self.react_loop.last_tools_override)
        self.assertIsInstance(self.react_loop.last_tools_override, list)

    async def test_react_loop_user_session_receives_none(self):
        """用户会话 ReactLoop.run 收到 tools_override=None。"""
        system_text, history, tools_override = await self.orch.enhanced_context_builder.build(
            session_id="user_xxx", user_input="hi", history=[]
        )
        self.orch.react_loop.run(
            user_input="hi",
            history=history,
            system=system_text,
            session_id="user_xxx",
            tools_override=tools_override,
        )
        self.assertIsNone(self.react_loop.last_tools_override)

    def test_cron_tool_registry_injected_to_react_loop(self):
        """set_cron_dependencies 注入 cron_tool_registry 到 react_loop。"""
        self.orch.cron_scheduler = _MockCronSchedulerForCache({})
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=self.cron_tool_registry,
        )
        self.assertIs(self.orch.react_loop.cron_tool_registry, self.cron_tool_registry)

    def test_build_cron_tools_injects_registry_on_first_call(self):
        """_build_cron_tools 首次调用时懒注入 cron_tool_registry 到 react_loop。"""
        sched = {"active_tools_snapshot": []}
        self.orch.cron_scheduler = _MockCronSchedulerForCache({"sched5": sched})
        self.orch.cron_isolator.set_dependencies(
            cron_scheduler=self.orch.cron_scheduler,
            cron_tool_registry=None,  # 先不注入
        )
        # 手动设置后调用 _build_cron_tools
        self.orch.cron_tool_registry = self.cron_tool_registry
        # 重置 react_loop.cron_tool_registry 为 None 模拟未注入
        self.react_loop.cron_tool_registry = None
        self.orch.cron_isolator.build_cron_tools("cron:sched5")
        # 首次调用后已注入
        self.assertIs(self.orch.react_loop.cron_tool_registry, self.cron_tool_registry)


# ---------------------------------------------------------------------------
# SubTask 5.7: ReactLoop._execute_tool_with_dispatch 派发
# ---------------------------------------------------------------------------


class TestReactLoopDispatch(unittest.TestCase):
    """ReactLoop._execute_tool_with_dispatch 优先派发 cron_tool_registry。"""

    def setUp(self):
        from src.agent.react_loop import ReactLoop
        self.loop = ReactLoop.__new__(ReactLoop)
        # 不设置 cron_tool_registry（模拟未注入）
        self.loop.cron_tool_registry = None
        self.loop.tool_registry = None

    def test_dispatch_no_registries_returns_error(self):
        """两个 registry 都为 None 时返回错误字符串。"""
        result = self.loop._execute_tool_with_dispatch("any_tool", {})
        self.assertIn("未注册", result)

    def test_dispatch_cron_tool_registry_priority(self):
        """cron_tool_registry 命中时优先派发（不调全局 registry）。"""
        class MockCronReg:
            def has_tool(self, name):
                return name == "cron_only"

            def execute_tool(self, name, tool_input):
                return f"cron_exec:{name}"

        class MockGlobalReg:
            def execute_tool(self, name, tool_input):
                return f"global_exec:{name}"

        self.loop.cron_tool_registry = MockCronReg()
        self.loop.tool_registry = MockGlobalReg()

        # cron_only 工具走 cron_tool_registry
        result = self.loop._execute_tool_with_dispatch("cron_only", {})
        self.assertEqual(result, "cron_exec:cron_only")
        # global_tool 工具回退到 tool_registry
        result = self.loop._execute_tool_with_dispatch("global_tool", {})
        self.assertEqual(result, "global_exec:global_tool")

    def test_dispatch_cron_registry_exception_falls_back(self):
        """cron_tool_registry 抛异常时回退到 tool_registry。"""
        class FlakyCronReg:
            def has_tool(self, name):
                return True

            def execute_tool(self, name, tool_input):
                raise RuntimeError("cron registry crashed")

        class MockGlobalReg:
            def execute_tool(self, name, tool_input):
                return f"global_exec:{name}"

        self.loop.cron_tool_registry = FlakyCronReg()
        self.loop.tool_registry = MockGlobalReg()
        result = self.loop._execute_tool_with_dispatch("flaky_tool", {})
        # 异常被捕获，回退到全局 registry
        self.assertEqual(result, "global_exec:flaky_tool")

    def test_dispatch_cron_has_tool_false_falls_back(self):
        """cron_tool_registry.has_tool 返回 False 时回退到 tool_registry。"""
        class EmptyCronReg:
            def has_tool(self, name):
                return False

            def execute_tool(self, name, tool_input):
                raise AssertionError("不应被调用")

        class MockGlobalReg:
            def execute_tool(self, name, tool_input):
                return f"global_exec:{name}"

        self.loop.cron_tool_registry = EmptyCronReg()
        self.loop.tool_registry = MockGlobalReg()
        result = self.loop._execute_tool_with_dispatch("any", {})
        self.assertEqual(result, "global_exec:any")


# ---------------------------------------------------------------------------
# SubTask 5.10: 端到端集成（echo_text 真实子进程）
# ---------------------------------------------------------------------------


class TestEndToEndIntegration(unittest.TestCase):
    """端到端：注册 → schema 合并 → 子进程执行 全链路。"""

    def setUp(self):
        self.sandbox = _CronToolSandbox()
        self.sandbox.make_tool("echo_text")

    def tearDown(self):
        self.sandbox.cleanup()

    def test_full_lifecycle_register_schema_execute(self):
        """完整生命周期：注册 → get_tools_schema → execute_tool。"""
        registry = CronToolRegistry(base_dir=self.sandbox.base_dir)
        # 1. 注册
        meta = registry.register("echo_text")
        self.assertEqual(meta.name, "echo_text")
        # 2. schema 可用
        schemas = registry.get_tools_schema()
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["name"], "echo_text")
        # 3. 子进程执行
        result = registry.execute_tool("echo_text", {"text": "e2e"})
        self.assertEqual(result, "echo: e2e")

    def test_full_lifecycle_with_orchestrator_filter(self):
        """端到端：Orchestrator 过滤 + cron_tool 合并 + 派发执行。"""
        registry = CronToolRegistry(base_dir=self.sandbox.base_dir)
        registry.register("echo_text")

        global_schemas = [
            {"name": "search", "description": "搜索", "input_schema": {"type": "object"}},
        ]
        global_reg = _MockToolRegistryForCache(global_schemas)
        sched = {"active_tools_snapshot": ["search"]}
        cron_sched = _MockCronSchedulerForCache({"e2e": sched})

        from src.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch.tool_registry = global_reg
        orch.cron_scheduler = cron_sched
        orch.cron_tool_registry = registry

        from src.agent.react_loop import ReactLoop
        react_loop = ReactLoop.__new__(ReactLoop)
        react_loop.cron_tool_registry = None
        react_loop.tool_registry = global_reg
        orch.react_loop = react_loop
        # 委托管理器（方法对象模式，持有 orch 引用）
        orch.context_builder = ContextBuilder()
        orch.cron_isolator = CronIsolator(orchestrator=orch)

        # _build_cron_tools 过滤 + 合并
        tools_override = orch.cron_isolator.build_cron_tools("cron:e2e")
        names = [t["name"] for t in tools_override]
        self.assertIn("search", names)  # snapshot 允许
        self.assertIn("echo_text", names)  # cron_tool 始终可见

        # react_loop 已注入 cron_tool_registry，可派发执行
        self.assertIs(react_loop.cron_tool_registry, registry)
        result = react_loop._execute_tool_with_dispatch(
            "echo_text", {"text": "dispatched"}
        )
        self.assertEqual(result, "echo: dispatched")


if __name__ == "__main__":
    unittest.main()
