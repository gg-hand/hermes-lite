"""SMTP 配置环境变量提取测试（6.2）。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()


class TestBlogMonitorSmtpFromEnv(unittest.TestCase):
    """blog_monitor_joyehuang 的 SMTP 配置从环境变量读取。"""

    def test_get_smtp_config_exists(self):
        """get_smtp_config 函数存在。"""
        from cron_tool.blog_monitor_joyehuang import run as blog_run
        self.assertTrue(hasattr(blog_run, "get_smtp_config"))

    def test_smtp_host_from_env(self):
        os.environ["SMTP_HOST"] = "smtp.test.com"
        try:
            from cron_tool.blog_monitor_joyehuang import run as blog_run
            config = blog_run.get_smtp_config()
            self.assertEqual(config["smtp_host"], "smtp.test.com")
        finally:
            del os.environ["SMTP_HOST"]

    def test_smtp_password_from_env(self):
        os.environ["SMTP_PASSWORD"] = "test_password"
        try:
            from cron_tool.blog_monitor_joyehuang import run as blog_run
            config = blog_run.get_smtp_config()
            self.assertEqual(config["sender_password"], "test_password")
        finally:
            del os.environ["SMTP_PASSWORD"]

    def test_default_smtp_host_when_env_missing(self):
        """环境变量缺失时使用默认值（不抛异常）。"""
        os.environ.pop("SMTP_HOST", None)
        from cron_tool.blog_monitor_joyehuang import run as blog_run
        config = blog_run.get_smtp_config()
        self.assertTrue(config["smtp_host"])

    def test_smtp_port_from_env(self):
        """SMTP_PORT 从环境变量读取并转为 int。"""
        os.environ["SMTP_PORT"] = "587"
        try:
            from cron_tool.blog_monitor_joyehuang import run as blog_run
            config = blog_run.get_smtp_config()
            self.assertEqual(config["smtp_port"], 587)
            self.assertIsInstance(config["smtp_port"], int)
        finally:
            del os.environ["SMTP_PORT"]

    def test_receiver_email_falls_back_to_smtp_user(self):
        """NOTIFY_EMAIL 缺失时回退到 SMTP_USER。"""
        os.environ.pop("NOTIFY_EMAIL", None)
        os.environ["SMTP_USER"] = "user@example.com"
        try:
            from cron_tool.blog_monitor_joyehuang import run as blog_run
            config = blog_run.get_smtp_config()
            self.assertEqual(config["receiver_email"], "user@example.com")
        finally:
            del os.environ["SMTP_USER"]


class TestConfigYamlExampleHasSmtpSection(unittest.TestCase):
    """config.yaml.example 包含 cron.hooks.notify.channels.email 段。"""

    def test_config_example_has_smtp_placeholders(self):
        config_path = Path(_PROJECT_ROOT) / "config.yaml.example"
        if not config_path.exists():
            self.skipTest("config.yaml.example 不存在")
        content = config_path.read_text(encoding="utf-8")
        self.assertIn("cron:", content)
        self.assertIn("hooks:", content)
        self.assertIn("notify:", content)
        self.assertIn("email:", content)
        self.assertIn("${SMTP_HOST}", content)
        self.assertIn("${SMTP_PASSWORD}", content)


if __name__ == "__main__":
    unittest.main()
