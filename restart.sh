#!/bin/bash
# Teage Liu 重启脚本（Linux，后台运行 + PID 管理）
# 用法:
#   ./restart.sh                  # 默认 0.0.0.0:8000
#   TEAGE_PORT=7007 ./restart.sh  # 自定义端口（生产环境）
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.server.pid"
LOG_DIR="${TEAGE_LOG_DIR:-$SCRIPT_DIR/data}"
SERVER_LOG="${TEAGE_SERVER_LOG:-$LOG_DIR/server.log}"

# ---------- 0. 加载 .env ----------
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# ---------- 1. 默认值（环境变量优先） ----------
export TEAGE_HOST="${TEAGE_HOST:-0.0.0.0}"
export TEAGE_PORT="${TEAGE_PORT:-8000}"
export TEAGE_CONFIG="${TEAGE_CONFIG:-config.yaml}"

mkdir -p "$LOG_DIR"

echo "=== Teage Liu 重启 ==="
echo "时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "Host:   $TEAGE_HOST"
echo "Port:   $TEAGE_PORT"
echo "Config: $TEAGE_CONFIG"
echo "Log:    $SERVER_LOG"
echo ""

# ---------- 2. 停止旧进程 ----------
echo "[1/3] 停止旧进程..."

# 方式 A: 通过 PID 文件
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  发送 SIGTERM 到 PID $OLD_PID ..."
        kill "$OLD_PID" 2>/dev/null || true
        # 等待最多 5 秒
        for i in $(seq 1 10); do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                echo "  进程 $OLD_PID 已退出"
                break
            fi
            sleep 0.5
        done
        # 若仍未退出则强杀
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "  强制终止 PID $OLD_PID ..."
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
    else
        echo "  PID 文件中的 $OLD_PID 已不存在"
    fi
    rm -f "$PID_FILE"
fi

# 方式 B: 通过端口占用查找并杀掉
echo "  检查端口 $TEAGE_PORT 占用..."
PORT_PID=$(ss -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | grep -oP 'pid=\K[0-9]+' | head -1 || true)
if [ -z "$PORT_PID" ]; then
    PORT_PID=$(netstat -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | awk '{print $NF}' | grep -oP '[0-9]+' | head -1 || true)
fi

if [ -n "$PORT_PID" ] && [ "$PORT_PID" != "0" ]; then
    echo "  端口 $TEAGE_PORT 被 PID $PORT_PID 占用，终止中..."
    kill -15 "$PORT_PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$PORT_PID" 2>/dev/null; then
        echo "  强制终止 PID $PORT_PID ..."
        kill -9 "$PORT_PID" 2>/dev/null || true
        sleep 1
    fi
fi

# 确认端口已释放
for i in $(seq 1 10); do
    REMAIN=$(ss -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | wc -l || true)
    if [ "$REMAIN" -eq 0 ]; then
        echo "  端口 $TEAGE_PORT 已释放"
        break
    fi
    sleep 0.5
done

echo ""

# ---------- 3. API Key 校验 ----------
# config.yaml 优先使用 ${LLM_MAIN_API_KEY}，兼容 provider 命名（DEEPSEEK_API_KEY 等）
if [ -z "${LLM_MAIN_API_KEY:-}" ] \
   && [ -z "${DEEPSEEK_API_KEY:-}" ] \
   && [ -z "${ANTHROPIC_API_KEY:-}" ] \
   && [ -z "${OPENAI_API_KEY:-}" ] \
   && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
    echo "错误: 未设置 API Key（LLM_MAIN_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY ...）。"
    echo "请在 .env 文件中配置，参考 .env.example"
    exit 1
fi

# 安全告警：TEAGE_API_KEY 未设置时所有 API 端点无认证保护
if [ -z "${TEAGE_API_KEY:-}" ]; then
    echo "============================================================"
    echo "WARNING: TEAGE_API_KEY 未设置，所有 API 端点无认证保护。"
    echo "         生产部署请在 .env 中设置 TEAGE_API_KEY=<strong-password>"
    echo "         本地开发可忽略此告警。"
    echo "============================================================"
else
    echo "TEAGE_API_KEY 已设置，API 认证将启用。"
fi

# ---------- 4. 启动服务 ----------
echo "[2/3] 启动 Teage Liu 服务..."

# 后台启动并记录 PID
nohup python -m uvicorn teage_liu.app:app \
    --host "$TEAGE_HOST" \
    --port "$TEAGE_PORT" \
    --workers 1 \
    >> "$SERVER_LOG" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

echo "  服务已启动 (PID: $NEW_PID)"

# 等待服务就绪（最多 10 秒）
echo "[3/3] 等待服务就绪..."
READY=0
for i in $(seq 1 20); do
    if curl -s -o /dev/null "http://127.0.0.1:$TEAGE_PORT/health" 2>/dev/null; then
        echo "  ✓ 服务已就绪"
        READY=1
        break
    fi
    sleep 0.5
done

if [ "$READY" -ne 1 ]; then
    echo "  [WARN] 健康检查超时，请检查日志: $SERVER_LOG"
fi

echo ""
echo "=== 重启完成 ==="
echo "PID:     $NEW_PID"
echo "地址:    http://$TEAGE_HOST:$TEAGE_PORT"
echo "健康:    http://127.0.0.1:$TEAGE_PORT/health"
echo "API 文档: http://127.0.0.1:$TEAGE_PORT/docs"
echo "PID 文件: $PID_FILE"
