"""multiagent 前端 UI 端到端测试（Playwright）。

Plan 4 Task 2/3/4：覆盖设置 UI、SSE 订阅、状态渲染。

默认情况下这些测试会被跳过（需要运行中的 hermes-lite 服务 + Playwright 浏览器）。
显式运行：``python -m pytest tests/e2e/test_multiagent_ui.py -v -m e2e``
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def hermes_app_url() -> str:
    return "http://127.0.0.1:18394"


class TestMultiagentSettingsUI:
    """multiagent 设置 UI 测试。"""

    def test_settings_modal_has_multiagent_section(self, page: Page, hermes_app_url):
        """设置模态框包含 multiagent 段。"""
        page.goto(hermes_app_url)
        # 点击齿轮图标打开设置
        page.click("[data-action='open-settings']")
        # 验证 multiagent 段存在
        expect(page.locator("#multiagent-section")).to_be_visible()

    def test_enable_multiagent_toggle(self, page: Page, hermes_app_url):
        """启用 multiagent 开关。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        # 勾选启用
        page.check("#multiagent-enabled")
        # 验证子选项显示
        expect(page.locator("#multiagent-role")).to_be_visible()
        expect(page.locator("#multiagent-blackboard-dir")).to_be_visible()

    def test_role_selector_has_director_and_worker(self, page: Page, hermes_app_url):
        """角色选择器包含 Director 和 Worker。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        page.check("#multiagent-enabled")
        # 验证角色选项
        options = page.locator("#multiagent-role option")
        expect(options.nth(0)).to_have_text("Worker")
        expect(options.nth(1)).to_have_text("Director")

    def test_save_multiagent_config_calls_api(self, page: Page, hermes_app_url):
        """保存配置调用 PUT /config API。"""
        page.goto(hermes_app_url)
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

    def test_disabled_hides_multiagent_section(self, page: Page, hermes_app_url):
        """multiagent 关闭时隐藏相关 UI。"""
        page.goto(hermes_app_url)
        page.click("[data-action='open-settings']")
        # 取消勾选启用
        page.uncheck("#multiagent-enabled")
        # 验证子选项隐藏
        expect(page.locator("#multiagent-role")).to_be_hidden()
