#!/bin/bash
# Hermes Lite 启动脚本（Linux）
# 用法: ./start.sh
set -e

cd "$(dirname "$0")"

# 尝试加载 .env 文件（如存在）
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# 校验必要的环境变量
if [ -z "${DEEPSEEK_API_KEY}" ] && [ -z "${ANTHROPIC_API_KEY}" ]; then
    echo "错误: 未设置 API Key。"
    echo "请在环境变量或 .env 文件中设置以下之一："
    echo "  DEEPSEEK_API_KEY   — DeepSeek（当前默认 provider）"
    echo "  ANTHROPIC_API_KEY  — Anthropic Claude"
    echo "详情见 .env.example"
    exit 1
fi

# 安全告警：HERMES_API_KEY 未设置时打印 WARNING（不阻断启动）
if [ -z "${HERMES_API_KEY}" ]; then
    echo "============================================================"
    echo "WARNING: HERMES_API_KEY 未设置，所有 API 端点无认证保护。"
    echo "         生产部署请在 .env 中设置 HERMES_API_KEY=<strong-password>"
    echo "         本地开发可忽略此告警。"
    echo "============================================================"
else
    echo "HERMES_API_KEY 已设置，API 认证将启用。"
fi

exec python -m uvicorn teage_liu.app:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1
