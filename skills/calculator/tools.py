"""Calculator Skill 工具实现。

每个 Skill 的 tools.py 应导出 TOOLS 列表，每项含：
- name: 工具名称
- handler: 工具函数名（字符串，需在模块中可 getattr 到）
- description: 工具描述
- input_schema: JSON Schema（Anthropic tool use 格式）
"""

from __future__ import annotations


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


TOOLS = [
    {
        "name": "add",
        "handler": "add",
        "description": "返回两数之和（a + b）",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个数"},
                "b": {"type": "number", "description": "第二个数"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "subtract",
        "handler": "subtract",
        "description": "返回两数之差（a - b）",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个数"},
                "b": {"type": "number", "description": "第二个数"},
            },
            "required": ["a", "b"],
        },
    },
]
