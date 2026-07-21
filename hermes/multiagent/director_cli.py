"""Director 启动入口：作为独立进程运行。

用法：
    python -m hermes.multiagent.director_cli --bb-root <path> [--mode script|agent]

Director 职责：
- 获取启动互斥锁（locks/director.lock）
- 递增 epoch（写入 director.md）
- 广播 director_started 消息
- 周期更新 last_director_tick
- 监督 Worker 心跳
- 推进超时轮次
- flush messages.pending.md
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from hermes.multiagent.blackboard import Blackboard
from hermes.multiagent.director_engine import DirectorEngine

logger = logging.getLogger(__name__)


async def main_async(bb_root: Path, mode: str = "script") -> None:
    """Director 主协程。"""
    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    # 构造 config（DirectorEngine 读取 config["multiagent"]["director"]）
    config = {
        "multiagent": {
            "director_implementation": mode,
            "director": {
                "heartbeat_interval_seconds": 10,
                "heartbeat_timeout_seconds": 30,
                "degraded_threshold_seconds": 20,
                "turn_timeout_seconds": 30,
            },
        }
    }

    director = DirectorEngine(
        bb_root=bb_root,
        config=config,
        agent_id="director_001",
    )

    # 优雅关闭
    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    # Windows 不支持 loop.add_signal_handler，使用 signal.signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (OSError, ValueError):
            pass

    await director.start()
    try:
        await stop_event.wait()
    finally:
        await director.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Director")
    parser.add_argument("--bb-root", required=True, help="Blackboard 根目录")
    parser.add_argument(
        "--mode", default="script", choices=["agent", "script"],
        help="Director 形态（script=确定性脚本，agent=LLM 仲裁）"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(main_async(Path(args.bb_root), args.mode))


if __name__ == "__main__":
    main()
