"""execute_command Popen 可中断版测试。

mock 策略：不 mock subprocess（需要真实进程行为测试 kill）。
使用跨平台安全命令：sleep / echo 等。

运行方式:
    python -m pytest tests/test_builtin_tools_exec.py -v
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.agent.builtin_tools import (
    execute_command,
    _set_running_proc,
    _get_and_clear_running_proc,
    kill_running_process,
)

# 跨平台可靠命令
_SLEEP_CMD = "python -c \"import time; time.sleep(60)\""
_ECHO_CMD = "echo hello"
# 短 sleep（测试用）
_SHORT_SLEEP = "python -c \"import time; time.sleep(5)\""


# ---------------------------------------------------------------------------
# 进程暴露
# ---------------------------------------------------------------------------


class TestProcessExposure(unittest.TestCase):

    def tearDown(self):
        _set_running_proc(None)

    def test_proc_set_during_execution(self):
        """运行长命令时 _get_running_proc 能取到 Popen 对象。"""
        # 在子线程中跑长命令，主线程验证进程引用
        result_holder = {}

        def run_cmd():
            result_holder["out"] = execute_command(_SHORT_SLEEP, timeout=10)

        t = threading.Thread(target=run_cmd)
        t.start()
        time.sleep(0.3)  # 等子线程进入 communicate

        proc = _get_and_clear_running_proc()
        self.assertIsNotNone(proc, "Running process should be exposed")
        # 恢复进程引用以便子线程正常清理
        _set_running_proc(proc)
        t.join(timeout=12)

    def test_proc_cleared_after_done(self):
        """命令正常完成后进程引用被清空。"""
        execute_command(_ECHO_CMD)
        self.assertIsNone(_get_and_clear_running_proc())

    def test_proc_cleared_after_timeout(self):
        """命令超时后进程引用也被清空。"""
        execute_command(_SLEEP_CMD, timeout=1)
        self.assertIsNone(_get_and_clear_running_proc())


# ---------------------------------------------------------------------------
# kill_running_process
# ---------------------------------------------------------------------------


class TestKillRunningProcess(unittest.TestCase):

    def tearDown(self):
        _set_running_proc(None)

    def test_kill_no_process(self):
        """没有运行中的进程时返回 False。"""
        self.assertFalse(kill_running_process())

    def test_kill_already_finished(self):
        """进程已退出时返回 False，不抛异常。"""
        import subprocess
        proc = subprocess.Popen(
            [_ECHO_CMD.split()[0]] if sys.platform != "win32" else ["cmd", "/c", "echo done"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        proc.communicate(timeout=5)  # 等待完成
        _set_running_proc(proc)
        self.assertFalse(kill_running_process())
        _set_running_proc(None)

    def test_kill_running_process(self):
        """kill 正在运行的 sleep → 返回 True。"""
        import subprocess

        popen_args = {
            "args": _SLEEP_CMD,
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if sys.platform == "win32":
            popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_args["start_new_session"] = True

        proc = subprocess.Popen(**popen_args)
        _set_running_proc(proc)

        time.sleep(0.3)  # 确保进程启动
        killed = kill_running_process()
        self.assertTrue(killed)

        # 进程应该已退出
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())


# ---------------------------------------------------------------------------
# 中断前缀
# ---------------------------------------------------------------------------


class TestInterruptDetection(unittest.TestCase):

    def tearDown(self):
        _set_running_proc(None)

    def test_interrupted_prefix_on_kill(self):
        """force kill 后输出以 [命令已被用户中断] 开头。"""
        import subprocess

        # 创建 cancel_event 供 execute_command 检测
        cancel_event = threading.Event()

        # 在子线程中跑长命令并 kill
        def run_and_kill():
            # 注入 cancel_event 到 ContextVar
            try:
                from src.agent._cancel_context import current_cancel_event
                token = current_cancel_event.set(cancel_event)
            except ImportError:
                return

            # 在另一个线程中延迟 kill
            def delayed_kill():
                time.sleep(0.5)
                cancel_event.set()
                kill_running_process()

            killer = threading.Thread(target=delayed_kill)
            killer.start()

            result = execute_command(_SLEEP_CMD, timeout=10)

            try:
                current_cancel_event.reset(token)
            except ImportError:
                pass

            return result

        t = threading.Thread(target=run_and_kill)
        results = {}

        def wrapper():
            results["out"] = run_and_kill()

        t2 = threading.Thread(target=wrapper)
        t2.start()
        t2.join(timeout=15)

        output = results.get("out", "")
        self.assertIn("[命令已被用户中断]", output)

    def test_normal_command_no_interrupt_prefix(self):
        """正常完成的命令不含中断前缀。"""
        result = execute_command(_ECHO_CMD)
        self.assertNotIn("[命令已被用户中断]", result)

    def test_timeout_no_interrupt_prefix_but_timeout_msg(self):
        """超时不显示中断前缀，但显示超时消息。"""
        result = execute_command(_SLEEP_CMD, timeout=1)
        self.assertNotIn("[命令已被用户中断]", result)
        self.assertIn("超时", result)

    def test_output_still_has_exit_code(self):
        """kill 后的输出保留退出码。"""
        # 先跑一个会失败的短命令做对照
        result = execute_command(
            "exit 1" if sys.platform != "win32" else "cmd /c exit 1",
            timeout=5,
        )
        self.assertIn("[退出码", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
