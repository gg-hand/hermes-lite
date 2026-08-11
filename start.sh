#!/bin/bash
# Teage Liu 启动脚本（Linux / macOS 本地开发，前台运行）
# 用法:
#   ./start.sh                    # 默认 0.0.0.0:8000
#   TEAGE_PORT=8080 ./start.sh    # 自定义端口
#   TEAGE_HOST=127.0.0.1 TEAGE_PORT=8000 ./start.sh
# ============================================================
set -e

cd "$(dirname "$0")"

# ---------- 1. 加载 .env ----------
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# ---------- 2. 默认值（环境变量优先） ----------
export TEAGE_HOST="${TEAGE_HOST:-0.0.0.0}"
export TEAGE_PORT="${TEAGE_PORT:-8000}"
export TEAGE_CONFIG="${TEAGE_CONFIG:-config.yaml}"
export TEAGE_SERVER_LOG="${TEAGE_SERVER_LOG:-data/server.log}"

# ---------- 3. API Key 校验 ----------
# config.yaml 优先使用 ${LLM_MAIN_API_KEY}，兼容 provider 命名（DEEPSEEK_API_KEY 等）
if [ -z "${LLM_MAIN_API_KEY:-}" ] \
   && [ -z "${DEEPSEEK_API_KEY:-}" ] \
   && [ -z "${ANTHROPIC_API_KEY:-}" ] \
   && [ -z "${OPENAI_API_KEY:-}" ] \
   && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
    echo "错误: 未设置 API Key。"
    echo "请在环境变量或 .env 文件中设置以下之一："
    echo "  LLM_MAIN_API_KEY  — 通用主 Key（推荐，config.yaml 默认占位符）"
    echo "  DEEPSEEK_API_KEY  — DeepSeek"
    echo "  ANTHROPIC_API_KEY — Anthropic Claude"
    echo "  OPENAI_API_KEY    — OpenAI"
    echo "  DASHSCOPE_API_KEY — 阿里通义千问"
    echo "详情见 .env.example"
    exit 1
fi

# ---------- 4. 安全告警 ----------
if [ -z "${TEAGE_API_KEY:-}" ]; then
    echo "============================================================"
    echo "WARNING: TEAGE_API_KEY 未设置，所有 API 端点无认证保护。"
    echo "         生产部署请在 .env 中设置 TEAGE_API_KEY=<strong-password>"
    echo "         本地开发可忽略此告警。"
    echo "============================================================"
else
    echo "TEAGE_API_KEY 已设置，API 认证将启用。"
fi

# ---------- 5. 启动 ----------
echo "------------------------------------------------------------"
echo " Teage Liu 本地开发模式（前台）"
echo "   Host:     $TEAGE_HOST"
echo "   Port:     $TEAGE_PORT"
echo "   Config:   $TEAGE_CONFIG"
echo "   日志:     $TEAGE_SERVER_LOG"
echo "   健康检查: http://127.0.0.1:$TEAGE_PORT/health"
echo "   API 文档: http://127.0.0.1:$TEAGE_PORT/docs"
echo "   退出:     Ctrl+C"
echo "------------------------------------------------------------"

exec python -m uvicorn teage_liu.app:app \
    --host "$TEAGE_HOST" \
    --port "$TEAGE_PORT" \
    --workers 1
