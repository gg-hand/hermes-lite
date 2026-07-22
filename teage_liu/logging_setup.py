"""JSON 结构化日志配置。

从 server.py 提取，供 server.py / app.py 共享日志初始化逻辑。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime


SERVER_LOG_PATH = os.environ.get("HERMES_SERVER_LOG", "data/server.log")


class JSONLogFormatter(logging.Formatter):
    """JSON 结构化日志格式化器。

    每行输出 JSON，可直接被 Logstash / Grafana Loki / Datadog 消费。
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 异常堆栈
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "traceback": self.formatException(record.exc_info),
            }
        # 请求上下文（由中间件注入 extra）
        for attr in ("method", "path", "status_code", "duration_ms", "session_id"):
            if hasattr(record, attr):
                log_entry[attr] = getattr(record, attr)
        return json.dumps(log_entry, ensure_ascii=False)


def _setup_logging(log_file: str) -> logging.Logger:
    """配置根日志：同时输出到 stdout 与文件。

    若 root logger 已有 handler 则跳过配置，既避免 reload 时重复输出，
    也避免清掉早期模块（如 config.py）已注册的 handler。

    测试环境（unittest / pytest）下跳过文件 handler，避免测试日志污染生产
    server.log。测试用例使用 assertLogs / caplog 进行日志断言，不受此影响。

    使用 RotatingFileHandler（最大 10MB，保留 3 个备份）防止日志无限增长。
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 已有 handler 时跳过（防止重复配置，也防止清掉早期 handler）
    if root_logger.handlers:
        return logging.getLogger("teage_liu.server")

    # 确保日志所在目录存在
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = JSONLogFormatter()

    # stdout 输出
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # 测试环境下跳过文件 handler
    _in_test_env = (
        "pytest" in sys.modules
        or "unittest" in sys.modules
        or "PYTEST_CURRENT_TEST" in os.environ
    )
    if _in_test_env:
        return logging.getLogger("teage_liu.server")

    # 文件输出：RotatingFileHandler，10MB 轮转，保留 3 份备份
    from logging.handlers import RotatingFileHandler

    actual_log = log_file
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dirname = os.path.dirname(log_file)
        basename = os.path.basename(log_file)
        name, ext = os.path.splitext(basename)
        actual_log = os.path.join(dirname, f"{name}_{ts}{ext}") if dirname else f"{name}_{ts}{ext}"
        file_handler = RotatingFileHandler(
            actual_log, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        root_logger.warning(
            "日志文件 %s 被锁定，已切到 %s", log_file, actual_log
        )
    file_handler.setFormatter(formatter)

    # 加过滤器：只允许 hermes、mcp 命名空间的日志写入文件
    class HermesLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            name = record.name
            return name.startswith("teage_liu.") or name.startswith("mcp.")

    file_handler.addFilter(HermesLogFilter())
    root_logger.addHandler(file_handler)

    return logging.getLogger("teage_liu.server")


logger = _setup_logging(SERVER_LOG_PATH)
