"""Calculator Skill 脚本资源。

支持两种调用方式：
1. CLI 入口：``python skills/calculator/scripts/calculator.py add 3 5``
2. Python import：``from skills.calculator.scripts.calculator import add, subtract``
"""

from __future__ import annotations

import sys


def add(a: float, b: float) -> str:
    """返回两数之和。

    Args:
        a: 第一个数
        b: 第二个数

    Returns:
        两数之和的字符串形式。
    """
    return str(float(a) + float(b))


def subtract(a: float, b: float) -> str:
    """返回两数之差。

    Args:
        a: 第一个数
        b: 第二个数

    Returns:
        两数之差的字符串形式。
    """
    return str(float(a) - float(b))


def _main(argv):
    if len(argv) < 3:
        print("用法: python skills/calculator/scripts/calculator.py <add|subtract> <a> <b>")
        return 1
    op = argv[1]
    try:
        a = float(argv[2])
        b = float(argv[3]) if len(argv) > 3 else 0.0
    except ValueError:
        print(f"参数错误：a 和 b 必须为数字，收到 {argv[2:]}")
        return 1
    if op == "add":
        print(add(a, b))
    elif op == "subtract":
        print(subtract(a, b))
    else:
        print(f"未知操作: {op}（支持: add, subtract）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
