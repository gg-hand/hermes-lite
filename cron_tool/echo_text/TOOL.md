---
name: echo_text
version: 1.0.0
description: 示例 cron_tool，原样回显输入文本（用于验证子进程执行链路）
author: teage-liu
timeout: 10
input_schema:
  type: object
  properties:
    text:
      type: string
      description: 待回显的文本
  required: [text]
---

# echo_text

示例 cron_tool，将传入的 `text` 字段原样回显，用于验证 cron_tool 子进程
执行链路（stdin JSON 输入 → stdout JSON 输出）。

## 接口约定

- 输入（stdin）：JSON 字符串，含 `input`（工具入参 dict）与可选 `context` 字段
- 输出（stdout）：JSON 字符串，形如 `{"result": "..."}`
- 超时：10 秒（由 TOOL.md frontmatter 的 `timeout` 字段配置）

## 注意事项

- 仅作示例，不调用任何外部依赖
