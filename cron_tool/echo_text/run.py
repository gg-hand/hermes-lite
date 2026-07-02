#!/usr/bin/env python3
"""echo_text cron_tool 运行脚本。

从 stdin 读取 JSON（形如 {"input": {"text": "..."}, "context": {...}}），
将 input.text 原样回显到 stdout（形如 {"result": "..."}）。
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"输入 JSON 解析失败: {exc}"}))
        sys.exit(1)

    tool_input = payload.get("input") or {}
    text = tool_input.get("text", "")
    print(json.dumps({"result": str(text)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
