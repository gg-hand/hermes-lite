"""Phase 4 Task 10: ToolRegistry Core/Deferred 分层测试。"""

from __future__ import annotations

import json
import unittest
import sys
from pathlib import Path

# 添加 src 到 sys.path（与既有测试一致）
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent.tool_registry import ToolRegistry, ToolDef


class TestToolRegistryLayered(unittest.TestCase):
    """ToolRegistry 分层结构测试。"""

    def setUp(self):
        """每个测试用例使用独立的 ToolRegistry 实例。"""
        self.registry = ToolRegistry()

    def _make_handler(self, result: str):
        """生成返回固定结果的 handler。"""
        def _handler(**kwargs):
            return result
        return _handler

    def test_register_core_and_deferred(self):
        """Core 全量 schema + Deferred stub。"""
        self.registry.register_core(
            "core_tool", "核心工具",
            {"type": "object", "properties": {}},
            self._make_handler("core"),
        )
        self.registry.register_deferred(
            "deferred_tool", "延迟工具",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            self._make_handler("deferred"),
        )
        schemas = self.registry.get_tools_schema()
        self.assertEqual(len(schemas), 2)

        # Core 工具应有 input_schema
        core = next(s for s in schemas if s["name"] == "core_tool")
        self.assertIn("input_schema", core)
        self.assertNotIn("defer_loading", core)

        # Deferred 工具应有 defer_loading，无 input_schema
        deferred = next(s for s in schemas if s["name"] == "deferred_tool")
        self.assertIn("defer_loading", deferred)
        self.assertTrue(deferred["defer_loading"])
        self.assertNotIn("input_schema", deferred)

    def test_get_tools_schema_stub_format(self):
        """Deferred stub 只含 name/description/defer_loading。"""
        self.registry.register_deferred(
            "tool1", "工具1描述",
            {"type": "object", "properties": {}},
            self._make_handler("r1"),
        )
        schemas = self.registry.get_tools_schema()
        self.assertEqual(len(schemas), 1)
        stub = schemas[0]
        # stub 应只含 name/description/defer_loading 三个键
        self.assertEqual(set(stub.keys()), {"name", "description", "defer_loading"})
        self.assertEqual(stub["name"], "tool1")
        self.assertEqual(stub["description"], "工具1描述")
        self.assertTrue(stub["defer_loading"])

    def test_search_and_load_select(self):
        """select:tool1,tool2 精确加载。"""
        self.registry.register_deferred(
            "tool1", "工具1", {"type": "object"}, self._make_handler("r1"),
        )
        self.registry.register_deferred(
            "tool2", "工具2", {"type": "object"}, self._make_handler("r2"),
        )
        self.registry.register_deferred(
            "tool3", "工具3", {"type": "object"}, self._make_handler("r3"),
        )

        loaded = self.registry.search_and_load("select:tool1,tool3")
        self.assertEqual(len(loaded), 2)
        loaded_names = {s["name"] for s in loaded}
        self.assertEqual(loaded_names, {"tool1", "tool3"})

        # 已加载到 _loaded_tools
        self.assertIn("tool1", self.registry._loaded_tools)
        self.assertIn("tool3", self.registry._loaded_tools)
        self.assertNotIn("tool2", self.registry._loaded_tools)

    def test_search_and_load_keyword(self):
        """关键词模糊匹配。"""
        self.registry.register_deferred(
            "web_search", "搜索网页内容", {"type": "object"}, self._make_handler("ws"),
        )
        self.registry.register_deferred(
            "fetch_page", "获取网页", {"type": "object"}, self._make_handler("fp"),
        )
        self.registry.register_deferred(
            "calculator", "计算器", {"type": "object"}, self._make_handler("calc"),
        )

        loaded = self.registry.search_and_load("搜索")
        # web_search 描述含"搜索"，fetch_page 描述含"获取"（不含"搜索"）
        # 但 "搜索" 关键词也拆分为单字？实际实现是子串匹配，所以 web_search 命中
        self.assertGreaterEqual(len(loaded), 1)
        loaded_names = {s["name"] for s in loaded}
        self.assertIn("web_search", loaded_names)

    def test_execute_tool_route_loaded_first(self):
        """已加载工具优先于未加载。"""
        self.registry.register_core(
            "shared", "核心版本", {"type": "object"}, self._make_handler("core"),
        )
        # 加载一个同名的 deferred 工具到 _loaded_tools
        self.registry.register_deferred(
            "shared", "延迟版本", {"type": "object"}, self._make_handler("deferred"),
        )
        self.registry.search_and_load("select:shared")
        # _loaded_tools 优先于 _core_tools
        result = self.registry.execute_tool("shared", {})
        self.assertEqual(result, "deferred")

    def test_execute_tool_unloaded_deferred(self):
        """未加载 Deferred 工具抛 ToolNotFoundError。"""
        from agent.tool_error import ToolNotFoundError
        self.registry.register_deferred(
            "unloaded", "未加载工具", {"type": "object"}, self._make_handler("x"),
        )
        with self.assertRaises(ToolNotFoundError) as ctx:
            self.registry.execute_tool("unloaded", {})
        self.assertIn("未加载", str(ctx.exception))
        self.assertIn("tool_list", ctx.exception.suggestion)

    def test_old_register_alias_to_core(self):
        """旧 register() 等价于 register_core()。"""
        self.registry.register(
            "old_tool", "旧 API 工具", {"type": "object"}, self._make_handler("old"),
        )
        # 应在 _core_tools 中
        self.assertIn("old_tool", self.registry._core_tools)
        # 不应在 _deferred_tools 中
        self.assertNotIn("old_tool", self.registry._deferred_tools)
        # get_tools_schema 返回完整 schema（含 input_schema）
        schemas = self.registry.get_tools_schema()
        self.assertEqual(len(schemas), 1)
        self.assertIn("input_schema", schemas[0])

    def test_old_search_tools_compat(self):
        """旧 search_tools() 仍可用。"""
        self.registry.register_deferred(
            "find_file", "查找文件", {"type": "object"}, self._make_handler("ff"),
        )
        # 旧 API 调用
        results = self.registry.search_tools("find")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "find_file")


class TestToolRegistryDisable(unittest.TestCase):
    """ToolRegistry disable/enable 相关功能测试。"""

    def setUp(self):
        self.registry = ToolRegistry()

    def _make_handler(self, result: str):
        """生成返回固定结果的 handler。"""
        def _handler(**kwargs):
            return result
        return _handler

    def test_disable_skill_marks_schema(self):
        """禁用 skill 后 schema 中应包含 enabled: False。"""
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        self.registry.disable_skill("weather")
        schemas = self.registry.get_tools_schema()
        tool = next(s for s in schemas if s["name"] == "skill__weather__get_weather")
        self.assertIn("enabled", tool)
        self.assertFalse(tool["enabled"])

    def test_enable_skill_restores_schema(self):
        """重新启用后 schema 中不应包含 enabled 字段。"""
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        self.registry.disable_skill("weather")
        self.registry.enable_skill("weather")
        schemas = self.registry.get_tools_schema()
        tool = next(s for s in schemas if s["name"] == "skill__weather__get_weather")
        self.assertNotIn("enabled", tool)

    def test_is_skill_disabled(self):
        """is_skill_disabled 返回正确的禁用/启用状态。"""
        self.assertFalse(self.registry.is_skill_disabled("weather"))
        self.registry.disable_skill("weather")
        self.assertTrue(self.registry.is_skill_disabled("weather"))
        self.registry.enable_skill("weather")
        self.assertFalse(self.registry.is_skill_disabled("weather"))

    def test_disable_nonexistent_skill(self):
        """禁用不存在的 skill 不应抛出异常。"""
        try:
            self.registry.disable_skill("nonexistent")
        except Exception:
            self.fail("disable_skill on nonexistent skill raised an exception")

    def test_disable_idempotent(self):
        """重复禁用同一 skill 应保持禁用状态。"""
        self.registry.disable_skill("weather")
        self.registry.disable_skill("weather")
        self.assertTrue(self.registry.is_skill_disabled("weather"))

    def test_enable_not_disabled(self):
        """对未禁用的 skill 执行 enable_skill 应为无操作。"""
        self.registry.enable_skill("weather")
        self.assertFalse(self.registry.is_skill_disabled("weather"))

    def test_disabled_tool_execute(self):
        """禁用状态下执行工具应抛 ToolNotFoundError，且 handler 不被调用。"""
        from agent.tool_error import ToolNotFoundError
        call_count = [0]

        def counting_handler(**kwargs):
            call_count[0] += 1
            return "result"

        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            counting_handler,
        )
        # 加载到 _loaded_tools 以允许执行
        self.registry.search_and_load("select:skill__weather__get_weather")
        self.registry.disable_skill("weather")
        with self.assertRaises(ToolNotFoundError) as ctx:
            self.registry.execute_tool("skill__weather__get_weather", {})
        self.assertIn("已被禁用", str(ctx.exception))
        self.assertEqual(call_count[0], 0)

    def test_enable_after_disable_allows_execution(self):
        """重新启用后，工具应能正常执行。"""
        call_count = [0]

        def counting_handler(**kwargs):
            call_count[0] += 1
            return "success"

        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            counting_handler,
        )
        self.registry.search_and_load("select:skill__weather__get_weather")
        self.registry.disable_skill("weather")
        self.registry.enable_skill("weather")
        result = self.registry.execute_tool("skill__weather__get_weather", {})
        self.assertEqual(result, "success")
        self.assertEqual(call_count[0], 1)


class TestToolRegistryUnregister(unittest.TestCase):
    """ToolRegistry unregister_by_prefix 相关功能测试。"""

    def setUp(self):
        self.registry = ToolRegistry()

    def _make_handler(self, result: str):
        """生成返回固定结果的 handler。"""
        def _handler(**kwargs):
            return result
        return _handler

    def test_unregister_by_prefix(self):
        """按前缀注销应移除匹配的 deferred 工具并保留不匹配的。"""
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        self.registry.register_deferred(
            "skill__calc__add", "加法",
            {"type": "object", "properties": {}},
            self._make_handler("sum"),
        )
        self.registry.register_deferred(
            "builtin__help", "帮助",
            {"type": "object", "properties": {}},
            self._make_handler("help"),
        )
        count = self.registry.unregister_by_prefix("skill__weather__")
        self.assertEqual(count, 1)
        self.assertNotIn("skill__weather__get_weather", self.registry._deferred_tools)
        self.assertIn("skill__calc__add", self.registry._deferred_tools)
        self.assertIn("builtin__help", self.registry._deferred_tools)

    def test_unregister_by_prefix_loaded(self):
        """已加载的工具按前缀注销后应从 deferred 和 loaded 两处移除。"""
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        self.registry.register_deferred(
            "skill__calc__add", "加法",
            {"type": "object", "properties": {}},
            self._make_handler("sum"),
        )
        # 预加载一个工具
        self.registry.search_and_load("select:skill__weather__get_weather")
        self.assertIn("skill__weather__get_weather", self.registry._loaded_tools)
        count = self.registry.unregister_by_prefix("skill__weather__")
        # 工具同时存在于 _deferred_tools 和 _loaded_tools 中，故计数为 2
        self.assertEqual(count, 2)
        self.assertNotIn("skill__weather__get_weather", self.registry._deferred_tools)
        self.assertNotIn("skill__weather__get_weather", self.registry._loaded_tools)
        self.assertIn("skill__calc__add", self.registry._deferred_tools)

    def test_unregister_by_prefix_returns_count(self):
        """unregister_by_prefix 应返回实际移除的工具数量。"""
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        self.registry.register_deferred(
            "skill__weather__get_forecast", "获取预报",
            {"type": "object", "properties": {}},
            self._make_handler("cloudy"),
        )
        self.registry.register_deferred(
            "skill__calc__add", "加法",
            {"type": "object", "properties": {}},
            self._make_handler("sum"),
        )
        count = self.registry.unregister_by_prefix("skill__weather__")
        self.assertEqual(count, 2)

    def test_unregister_by_prefix_empty(self):
        """不存在的后缀应返回 0。"""
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        count = self.registry.unregister_by_prefix("nonexistent__")
        self.assertEqual(count, 0)
        self.assertIn("skill__weather__get_weather", self.registry._deferred_tools)

    def test_unregister_by_prefix_core_untouched(self):
        """按前缀注销不应影响 core tier 中的工具。"""
        self.registry.register_core(
            "core__help", "帮助",
            {"type": "object", "properties": {}},
            self._make_handler("help"),
        )
        self.registry.register_deferred(
            "skill__weather__get_weather", "获取天气",
            {"type": "object", "properties": {}},
            self._make_handler("sunny"),
        )
        count = self.registry.unregister_by_prefix("core__")
        self.assertEqual(count, 0)
        self.assertIn("core__help", self.registry._core_tools)


if __name__ == "__main__":
    unittest.main()
