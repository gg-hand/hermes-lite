"""Task 2: 验证 logging_setup 模块可独立导入并配置日志。

从 server.py 提取 JSONLogFormatter + _setup_logging，使日志配置可在
server.py 之外的模块（如 app.py）复用。
"""
from __future__ import annotations

import json
import logging
import sys

sys.path.insert(0, "src")


def test_json_log_formatter_outputs_valid_json():
    """JSONLogFormatter 输出合法 JSON 且包含必要字段。"""
    from logging_setup import JSONLogFormatter

    formatter = JSONLogFormatter()
    record = logging.LogRecord(
        name="hermes.server", level=logging.INFO, pathname=__file__,
        lineno=1, msg="test message", args=(), exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "test message"
    assert parsed["logger"] == "hermes.server"
    assert "timestamp" in parsed


def test_setup_logging_returns_hermes_logger():
    """_setup_logging 返回名为 hermes.server 的 logger。"""
    from logging_setup import _setup_logging
    logger = _setup_logging("data/test_server.log")
    assert logger.name == "hermes.server"


def test_logger_module_attribute_exists():
    """logging_setup 模块暴露 logger 属性。"""
    from logging_setup import logger
    assert isinstance(logger, logging.Logger)
    assert logger.name == "hermes.server"
