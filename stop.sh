#!/bin/bash
# Teage Liu stop script (Linux)
# 停止服务及其相关后台线程任务（cleanup_loop, file_cleanup_loop,
# metrics_persist_loop, cron_scheduler, SSE 流等）
#
# 停止流程：
#   1. 通过 PID 文件发送 SIGTERM（触发 FastAPI lifespan 关闭 →
#      BackgroundTaskRegistry.cancel_all() → container.close()）
#   2. 通过端口占用查找并终止残留进程（fallback）
#   3. 等待确认端口已释放
#   4. 清理 PID 文件
#   5. 可选：同时停止根目录 server.py（端口 3000）
#
# Usage:
#   ./stop.sh                           # 停止默认端口 8000
#   TEAGE_PORT=7007 ./stop.sh           # 停止自定义端口
#   ./stop.sh --also-root-server        # 同时停止根目录 server.py（端口 3000）
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.server.pid"
LOG_DIR="${TEAGE_LOG_DIR:-$SCRIPT_DIR/data}"

# ---------- 0. 加载 .env ----------
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# ---------- 1. 默认值（环境变量优先） ----------
export TEAGE_PORT="${TEAGE_PORT:-8000}"
ALSO_ROOT_SERVER=false
for arg in "$@"; do
    case "$arg" in
        --also-root-server) ALSO_ROOT_SERVER=true ;;
    esac
done

echo "=== Teage Liu 停止服务 ==="
echo "时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "端口:   $TEAGE_PORT"
echo ""

# ---------- 2. 停止主服务 ----------
echo "[1/3] 停止主服务 (端口 ${TEAGE_PORT}) ..."

STOPPED_VIA_PID=false

# 方式 A: 通过 PID 文件（优先 — 能触发 lifespan 优雅关闭）
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  通过 PID 文件 → 发送 SIGTERM 到 PID $OLD_PID ..."
        # 先发 SIGTERM，让 uvicorn 触发 lifespan 关闭
        # FastAPI lifespan 关闭阶段自动执行：
        #   task_registry.cancel_all() — 取消后台 asyncio 任务
        #   close_container() — 关闭 DI 组件
        kill -15 "$OLD_PID" 2>/dev/null || true
        # 等待最长 5 秒让优雅关闭完成
        for i in $(seq 1 10); do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                echo "  进程 $OLD_PID 已优雅退出"
                STOPPED_VIA_PID=true
                break
            fi
            sleep 0.5
        done
        # 若仍未退出则强杀
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "  进程未退出，执行 SIGKILL PID $OLD_PID ..."
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
            STOPPED_VIA_PID=true
        fi
    else
        echo "  PID 文件中的 $OLD_PID 已不存在（可能已退出）"
    fi
    rm -f "$PID_FILE"
fi

# 方式 B: 通过端口占用查找（fallback）
if [ "$STOPPED_VIA_PID" = false ]; then
    echo "  通过端口 $TEAGE_PORT 查找残留进程..."
    PORT_PID=$(ss -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | grep -oP 'pid=\K[0-9]+' | head -1 || true)
    if [ -z "$PORT_PID" ]; then
        PORT_PID=$(netstat -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | awk '{print $NF}' | grep -oP '[0-9]+' | head -1 || true)
    fi

    if [ -n "$PORT_PID" ] && [ "$PORT_PID" != "0" ]; then
        echo "  端口 $TEAGE_PORT 被 PID $PORT_PID 占用，发送 SIGTERM..."
        kill -15 "$PORT_PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$PORT_PID" 2>/dev/null; then
            echo "  强杀 PID $PORT_PID ..."
            kill -9 "$PORT_PID" 2>/dev/null || true
            sleep 1
        fi
        STOPPED_VIA_PID=true
    fi
fi

if [ "$STOPPED_VIA_PID" = false ]; then
    echo "  未发现运行中的服务"
fi

# 确认端口已释放（最多等 5 秒）
echo "  确认端口 $TEAGE_PORT 释放..."
PORT_RELEASED=false
for i in $(seq 1 10); do
    REMAIN=$(ss -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | wc -l || true)
    # 若 ss 不可用，fallback 到 netstat
    if [ -z "$REMAIN" ] || [ "$REMAIN" -eq 0 ]; then
        if command -v netstat &>/dev/null; then
            REMAIN=$(netstat -tlnp 2>/dev/null | grep ":$TEAGE_PORT " | wc -l || true)
        fi
    fi
    if [ "${REMAIN:-0}" -eq 0 ]; then
        echo "  端口 $TEAGE_PORT 已释放"
        PORT_RELEASED=true
        break
    fi
    sleep 0.5
done
if [ "$PORT_RELEASED" = false ]; then
    echo "  [WARN] 端口 $TEAGE_PORT 仍被占用，可能需要手动处理"
fi

# 再次确保 PID 文件已清理
if [ -f "$PID_FILE" ]; then
    rm -f "$PID_FILE"
    echo "  已清理 PID 文件"
fi

echo ""

# ---------- 3.（可选）停止根目录 server.py ----------
if [ "$ALSO_ROOT_SERVER" = true ]; then
    echo "[2/3] 停止根目录 server.py (端口 3000) ..."

    ROOT_PORT_PID=$(ss -tlnp 2>/dev/null | grep ":3000 " | grep -oP 'pid=\K[0-9]+' | head -1 || true)
    if [ -z "$ROOT_PORT_PID" ]; then
        ROOT_PORT_PID=$(netstat -tlnp 2>/dev/null | grep ":3000 " | awk '{print $NF}' | grep -oP '[0-9]+' | head -1 || true)
    fi

    if [ -n "$ROOT_PORT_PID" ] && [ "$ROOT_PORT_PID" != "0" ]; then
        echo "  端口 3000 被 PID $ROOT_PORT_PID 占用，终止中..."
        kill -15 "$ROOT_PORT_PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$ROOT_PORT_PID" 2>/dev/null; then
            echo "  强杀 PID $ROOT_PORT_PID ..."
            kill -9 "$ROOT_PORT_PID" 2>/dev/null || true
            sleep 1
        fi
    else
        echo "  未发现 server.py 运行"
    fi

    # 确认端口 3000 释放
    for i in $(seq 1 10); do
        REMAIN=$(ss -tlnp 2>/dev/null | grep ":3000 " | wc -l || true)
        if [ "${REMAIN:-0}" -eq 0 ]; then
            echo "  端口 3000 已释放"
            break
        fi
        sleep 0.5
    done

    echo ""
    echo "[3/3] 所有服务已停止"
else
    echo "[2/2] 主服务已停止"
    echo ""
    echo "提示: 如需同时停止根目录 server.py（端口 3000），请使用:"
    echo "  ./stop.sh --also-root-server"
fi

echo ""
echo "=== 停止完成 ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
