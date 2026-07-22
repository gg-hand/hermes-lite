"""Task 4: 验证 background_loops 模块可独立导入。"""
from __future__ import annotations

import inspect
import sys

sys.path.insert(0, "teage_liu")


def test_cleanup_loop_is_coroutine():
    """cleanup_loop 是 async 函数。"""
    from teage_liu.background_loops import cleanup_loop
    assert inspect.iscoroutinefunction(cleanup_loop)


def test_file_cleanup_loop_is_coroutine():
    """file_cleanup_loop 是 async 函数。"""
    from teage_liu.background_loops import file_cleanup_loop
    assert inspect.iscoroutinefunction(file_cleanup_loop)


def test_metrics_persist_loop_is_coroutine():
    """metrics_persist_loop 是 async 函数。"""
    from teage_liu.background_loops import metrics_persist_loop
    assert inspect.iscoroutinefunction(metrics_persist_loop)
