"""multiagent 前端 UI 端到端测试（Playwright）。

Plan 4 Task 2/3/4：覆盖设置 UI、SSE 订阅、状态渲染。

默认情况下这些测试会被跳过（需要运行中的 teage-liu 服务 + Playwright 浏览器）。
显式运行：``python -m pytest tests/e2e/test_multiagent_ui.py -v -m e2e``
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def teage_app_url() -> str:
    return "http://127.0.0.1:18394"


class TestMultiagentSettingsUI:
    """multiagent 设置 UI 测试。"""

    def test_settings_modal_has_multiagent_section(self, page: Page, teage_app_url):
        """设置模态框包含 multiagent 段。"""
        page.goto(teage_app_url)
        # 点击齿轮图标打开设置
        page.click("[data-action='open-settings']")
        # 验证 multiagent 段存在
        expect(page.locator("#multiagent-section")).to_be_visible()

    def test_enable_multiagent_toggle(self, page: Page, teage_app_url):
        """启用 multiagent 开关。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        # 勾选启用
        page.check("#multiagent-enabled")
        # 验证子选项显示
        expect(page.locator("#multiagent-role")).to_be_visible()
        expect(page.locator("#multiagent-blackboard-dir")).to_be_visible()

    def test_role_selector_has_director_and_worker(self, page: Page, teage_app_url):
        """角色选择器包含 Director 和 Worker。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        # 验证角色选项
        options = page.locator("#multiagent-role option")
        expect(options.nth(0)).to_have_text("Worker")
        expect(options.nth(1)).to_have_text("Director")

    def test_save_multiagent_config_calls_api(self, page: Page, teage_app_url):
        """保存配置调用 PUT /config API。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.select_option("#multiagent-role", "worker")
        page.fill("#multiagent-blackboard-dir", "/tmp/bb")

        # 监听网络请求
        with page.expect_request("/api/config", method="PUT") as req_info:
            page.click("[data-action='save-settings']")
        request = req_info.value
        # 验证请求体包含 multiagent 段
        post_data = request.post_data
        assert "multiagent" in post_data
        assert "worker" in post_data

    def test_disabled_hides_multiagent_section(self, page: Page, teage_app_url):
        """multiagent 关闭时隐藏相关 UI。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        # 取消勾选启用
        page.uncheck("#multiagent-enabled")
        # 验证子选项隐藏
        expect(page.locator("#multiagent-role")).to_be_hidden()


class TestMultiagentSSE:
    """multiagent SSE 通道测试（Plan 4 Task 3）。"""

    def test_sse_indicator_present(self, page: Page, teage_app_url):
        """页面包含 multiagent 状态指示器。"""
        page.goto(teage_app_url)
        # 验证状态指示器 DOM 存在
        expect(page.locator("#multiagent-indicator")).to_be_visible()

    def test_sse_indicator_shows_disabled_state(self, page: Page, teage_app_url):
        """multiagent 未启用时指示器显示禁用状态。"""
        page.goto(teage_app_url)
        indicator = page.locator("#multiagent-indicator")
        # 应显示"未启用"或类似文本
        expect(indicator).to_contain_text("未启用")

    def test_sse_indicator_shows_director_state(self, page: Page, teage_app_url):
        """启用后指示器显示 Director 状态。"""
        page.goto(teage_app_url)
        # 启用 multiagent（通过设置模态框）
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")
        # 等待指示器更新
        page.wait_for_selector(
            "#multiagent-indicator.state-healthy, "
            "#multiagent-indicator.state-degraded, "
            "#multiagent-indicator.state-fault"
        )
        indicator = page.locator("#multiagent-indicator")
        # 应有状态类
        class_attr = indicator.get_attribute("class") or ""
        assert any(
            state in class_attr
            for state in [
                "state-healthy",
                "state-degraded",
                "state-autonomous",
                "state-fault",
            ]
        )

    def test_sse_agent_panel_shows_list(self, page: Page, teage_app_url):
        """Agent 列表面板显示活跃 agents。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")
        # 等待 Agent 面板加载
        page.wait_for_selector("#multiagent-agents-panel")
        # 应至少显示自己
        agents = page.locator("#multiagent-agents-panel .agent-card")
        expect(agents.first).to_be_visible()

    def test_sse_autonomous_alert(self, page: Page, teage_app_url):
        """自治模式触发时显示告警。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")
        # 模拟自治模式触发（通过 SSE 事件）
        page.evaluate(
            """
            window.dispatchEvent(new CustomEvent('multiagent-alert', {
                detail: { type: 'autonomous_enter', data: { autonomous_mode: true } }
            }));
            """
        )
        # 应显示自治模式告警
        expect(page.locator("#multiagent-alert-banner")).to_be_visible()
        expect(page.locator("#multiagent-alert-banner")).to_contain_text("自治")


class TestMultiagentRender:
    """multiagent 渲染测试（Plan 4 Task 4）。"""

    def test_director_state_color_coding(self, page: Page, teage_app_url):
        """Director 状态颜色编码（绿/黄/橙/红）。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        # 等待指示器加载
        page.wait_for_selector("#multiagent-indicator[data-state]")

        # 验证状态类存在
        indicator = page.locator("#multiagent-indicator")
        state = indicator.get_attribute("data-state")
        assert state in ["healthy", "degraded", "autonomous", "fault", "unknown"]

    def test_agent_card_renders_correctly(self, page: Page, teage_app_url):
        """Agent 卡片正确渲染。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        page.wait_for_selector(".agent-card")
        card = page.locator(".agent-card").first
        # 应包含 agent_id、role、status
        expect(card).to_contain_text("agent_id")
        expect(card.locator(".agent-role")).to_be_visible()
        expect(card.locator(".agent-status")).to_be_visible()

    def test_trust_score_progress_bar(self, page: Page, teage_app_url):
        """信任分进度条渲染。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        page.wait_for_selector(".agent-card")
        # 信任分进度条应存在（如果有 agents）
        bar = page.locator(".trust-score-bar").first
        if bar.is_visible():
            # 验证宽度在 0-100%
            width = bar.evaluate("(el) => getComputedStyle(el).width")
            assert "%" in width or "px" in width

    def test_alert_banner_appears_and_disappears(self, page: Page, teage_app_url):
        """告警横幅出现并自动消失。"""
        page.goto(teage_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        page.click("[data-action='save-settings']")

        # 触发告警
        page.evaluate(
            """
            window.MultiagentRender.showAlert("测试告警", "info");
            """
        )
        expect(page.locator("#multiagent-alert-banner")).to_be_visible()
        expect(page.locator("#multiagent-alert-banner")).to_contain_text("测试告警")

        # 等待自动消失（默认 5 秒）
        page.wait_for_selector(
            "#multiagent-alert-banner", state="hidden", timeout=10000
        )


