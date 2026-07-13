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

exec python -m uvicorn hermes.app:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1
