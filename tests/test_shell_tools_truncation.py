"""bash_exec 输出截断测试（TDD）。

验证 _execute_command_inner 对 stdout/stderr 做长度截断，
避免大量输出注入 LLM 上下文导致 token 预算耗尽。

运行方式:
    python -m pytest tests/test_shell_tools_truncation.py -v
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from teage_liu.agent.tools import shell_tools
from teage_liu.agent.tools.shell_tools import execute_command


class TestOutputTruncation(unittest.TestCase):
    """验证 execute_command 的输出截断行为。"""

    def test_short_output_not_truncated(self):
        """短输出原样返回，不截断。"""
        # 用 python 命令避免 Windows echo 内置命令的兼容性问题
        result = execute_command(
            'python -c "print(\'hello world\')"', timeout=5
        )
        self.assertIn("hello world", result)
        self.assertNotIn("[truncated", result)

    def test_long_stdout_truncated(self):
        """超过阈值的 stdout 被截断并附原文长度提示。"""
        # 使用 sys.stdout.write 避免自动加 \n 导致字符数偏移
        cmd = 'python -c "import sys; sys.stdout.write(\'x\' * 30000)"'
        result = execute_command(cmd, timeout=10)
        # 截断标记存在
        self.assertIn("[truncated", result)
        # 提示包含原文长度 30000
        self.assertIn("30000", result)
        # 截断后总长度应明显小于原文（含提示串）
        self.assertLess(len(result), 25000)

    def test_threshold_boundary(self):
        """正好达到阈值时不截断，超过阈值时截断。"""
        # 读取当前阈值（默认 20000）
        threshold = getattr(shell_tools, "_MAX_OUTPUT_CHARS", 20000)

        # 阈值以内（不截断）：使用 sys.stdout.write 精确控制字符数
        cmd_within = (
            f'python -c "import sys; sys.stdout.write(\'y\' * {threshold})"'
        )
        result_within = execute_command(cmd_within, timeout=10)
        self.assertNotIn("[truncated", result_within)

        # 阈值 + 1（截断）
        cmd_over = (
            f'python -c "import sys; sys.stdout.write(\'z\' * {threshold + 1})"'
        )
        result_over = execute_command(cmd_over, timeout=10)
        self.assertIn("[truncated", result_over)

    def test_error_path_stderr_also_truncated(self):
        """错误路径下 stderr 大量输出也应截断。"""
        # 触发非零退出码并产生大量 stderr
        cmd = (
            'python -c "import sys; '
            "sys.stderr.write('e' * 30000); "
            'sys.exit(1)"'
        )
        result = execute_command(cmd, timeout=10)
        # 退出码会附在输出中
        self.assertIn("[退出码", result)
        # stderr 部分应被截断
        self.assertIn("[truncated", result)


if __name__ == "__main__":
    unittest.main()
