#!/bin/bash
# Hermes Lite 重启脚本（Linux 版）
# 用法: ./restart.sh
# =============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.server.pid"
LOG_DIR="$SCRIPT_DIR/data"
SERVER_LOG="$LOG_DIR/server.log"

# 确保日志目录存在
mkdir -p "$LOG_DIR"

echo "=== Hermes Lite 重启 ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# ---------- 1. 停止旧进程 ----------
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
echo "  检查端口 8000 占用..."
PORT_PID=$(ss -tlnp 2>/dev/null | grep ':8000' | grep -oP 'pid=\K[0-9]+' | head -1 || true)
if [ -z "$PORT_PID" ]; then
    PORT_PID=$(netstat -tlnp 2>/dev/null | grep ':8000' | awk '{print $NF}' | grep -oP '[0-9]+' | head -1 || true)
fi

if [ -n "$PORT_PID" ] && [ "$PORT_PID" != "0" ]; then
    echo "  端口 8000 被 PID $PORT_PID 占用，终止中..."
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
    REMAIN=$(ss -tlnp 2>/dev/null | grep ':8000' | wc -l || true)
    if [ "$REMAIN" -eq 0 ]; then
        echo "  端口 8000 已释放"
        break
    fi
    sleep 0.5
done

echo ""

# ---------- 2. 加载环境变量 ----------
echo "[2/3] 加载配置..."
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo "  已加载 .env 文件"
fi

# 默认配置
export TEAGE_CONFIG=${TEAGE_CONFIG:-config.yaml}
export TEAGE_SERVER_LOG=${TEAGE_SERVER_LOG:-data/server.log}

echo "  配置: $TEAGE_CONFIG"
echo "  日志: $TEAGE_SERVER_LOG"
echo ""

# ---------- 3. 启动服务 ----------
echo "[3/3] 启动 Hermes Lite 服务..."

# 后台启动并记录 PID
nohup python -m uvicorn teage_liu.app:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    >> "$SERVER_LOG" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

echo "  服务已启动 (PID: $NEW_PID)"

# 等待服务就绪（最多 10 秒）
echo "  等待服务就绪..."
for i in $(seq 1 20); do
    if curl -s -o /dev/null "http://127.0.0.1:8000/health" 2>/dev/null; then
        echo "  ✓ 服务已就绪"
        break
    fi
    sleep 0.5
done

echo ""
echo "=== 重启完成 ==="
echo "PID: $NEW_PID"
echo "地址: http://0.0.0.0:8000"
echo "健康: http://0.0.0.0:8000/health"
