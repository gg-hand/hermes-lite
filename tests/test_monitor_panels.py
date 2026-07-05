"""Phase 2 反馈监控：验证 monitor.html / monitor.js / monitor.css 包含新增面板。

不依赖运行中的服务，仅做静态资源结构检查，确保前端 DOM ID、JS 渲染入口、
CSS 类名三者一致。

运行方式:
    python -m unittest tests.test_monitor_panels -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class TestMonitorPanelsStructure(unittest.TestCase):
    """验证 Phase 1/2 新增面板的 DOM/JS/CSS 三方一致性。"""

    @classmethod
    def setUpClass(cls):
        web_dir = Path(_PROJECT_ROOT) / "web"
        cls.html = (web_dir / "monitor.html").read_text(encoding="utf-8")
        cls.js = (web_dir / "static" / "js" / "monitor.js").read_text(encoding="utf-8")
        cls.css = (web_dir / "static" / "css" / "monitor.css").read_text(encoding="utf-8")

    def test_html_contains_termination_reasons_section(self):
        """Phase 1：HTML 包含终止原因分布 section。"""
        self.assertIn('id="terminationReasonsBody"', self.html)
        self.assertIn("终止原因分布", self.html)

    def test_html_contains_tool_error_classes_section(self):
        """Phase 1：HTML 包含工具错误分类 section。"""
        self.assertIn('id="toolErrorClassesBody"', self.html)
        self.assertIn("工具错误分类", self.html)

    def test_html_contains_approval_stats_section(self):
        """Phase 2：HTML 包含审批统计 section。"""
        self.assertIn('id="approvalStatsBody"', self.html)
        self.assertIn("审批统计", self.html)

    def test_html_contains_signal_pool_section(self):
        """Phase 2：HTML 包含信号池攻略进度条 section。"""
        self.assertIn('id="signalPoolSummary"', self.html)
        self.assertIn('id="signalPoolBody"', self.html)
        self.assertIn("信号池攻略进度条", self.html)

    def test_js_renders_termination_reasons(self):
        """Phase 1：JS 包含 renderTerminationReasons 函数并在 renderMetrics 中调用。"""
        self.assertIn("function renderTerminationReasons", self.js)
        self.assertIn("renderTerminationReasons(m.termination_reasons_total", self.js)

    def test_js_renders_tool_error_classes(self):
        """Phase 1：JS 包含 renderToolErrorClasses 函数并在 renderMetrics 中调用。"""
        self.assertIn("function renderToolErrorClasses", self.js)
        self.assertIn("renderToolErrorClasses(m.tool_error_classes_total", self.js)

    def test_js_renders_approval_stats(self):
        """Phase 2：JS 包含 renderApprovalStats 函数并在 renderMetrics 中调用。"""
        self.assertIn("function renderApprovalStats", self.js)
        self.assertIn("renderApprovalStats(m.approval_decisions_total", self.js)

    def test_js_loads_signal_pool(self):
        """Phase 2：JS 包含 loadSignalPool 函数并在 refreshAll 中调用。"""
        self.assertIn("async function loadSignalPool", self.js)
        self.assertIn("loadSignalPool()", self.js)
        # 必须调用 /metrics/signals 端点
        self.assertIn("/metrics/signals", self.js)

    def test_css_has_termination_bar_styles(self):
        """Phase 1：CSS 包含终止原因进度条样式。"""
        self.assertIn(".term-bar-fill", self.css)
        self.assertIn(".term-bar-fill.danger", self.css)

    def test_css_has_approval_card_styles(self):
        """Phase 2：CSS 包含审批统计卡片样式。"""
        self.assertIn(".approval-cards", self.css)
        self.assertIn(".approval-card.approve", self.css)
        self.assertIn(".approval-card.deny", self.css)
        self.assertIn(".approval-card.timeout", self.css)

    def test_css_has_signal_pool_styles(self):
        """Phase 2：CSS 包含信号池进度条样式（含 triggered 脉动动画）。"""
        self.assertIn(".signal-pool-body", self.css)
        self.assertIn(".signal-bar-fill", self.css)
        self.assertIn(".signal-bar-fill.triggered", self.css)
        self.assertIn(".signal-bar-fill.written", self.css)
        self.assertIn("@keyframes signal-pulse", self.css)

    def test_js_signal_pool_renders_status_classes(self):
        """Phase 2：JS 渲染时区分 triggered/written 状态样式。"""
        # triggered 状态应使用绿色脉动动画
        self.assertIn("signal-bar-fill triggered", self.js)
        # written 状态应使用灰色
        self.assertIn("signal-bar-fill written", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
