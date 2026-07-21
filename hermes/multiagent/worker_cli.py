"""Worker 启动入口：作为独立进程运行。

用法：
    python -m hermes.multiagent.worker_cli --bb-root <path> --agent-id <id>
        [--capabilities cap1 cap2 ...] [--heartbeat-timeout <seconds>]

Worker 职责：
- 注册 agent_card 到 agents/{id}.md
- 周期上报心跳（更新 last_heartbeat）
- 监测 Director 心跳（超时进入自治模式）
- 发言前轮次校验（非本机轮次写 messages.pending.md）
- 优雅退出（释放锁 + 更新状态 + audit）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from hermes.multiagent.blackboard import Blackboard
from hermes.multiagent.worker_adapter import WorkerAdapter

logger = logging.getLogger(__name__)


async def main_async(
    bb_root: Path,
    agent_id: str,
    capabilities: list[str],
    heartbeat_timeout: int = 30,
) -> None:
    """Worker 主协程。"""
    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    config = {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": str(bb_root),
            "worker": {
                "agent_id": agent_id,
                "heartbeat_interval_seconds": 10,
                "capabilities": capabilities,
                "dangerous_tools": ["execute_command", "write_file", "call_tool"],
            },
            "director": {
                "heartbeat_timeout_seconds": heartbeat_timeout,
                "degraded_threshold_seconds": max(heartbeat_timeout // 2, 1),
            },
        }
    }

    adapter = WorkerAdapter(bb_root, config, agent_id=agent_id)

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

    await adapter.start()
    try:
        await stop_event.wait()
    finally:
        await adapter.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Worker")
    parser.add_argument("--bb-root", required=True, help="Blackboard 根目录")
    parser.add_argument("--agent-id", required=True, help="Agent ID")
    parser.add_argument(
        "--capabilities",
        nargs="*",
        default=["file_read", "file_write", "web_search"],
        help="Worker 能力列表",
    )
    parser.add_argument(
        "--heartbeat-timeout",
        type=int,
        default=30,
        help="Director 心跳超时秒数（超时后进入自治模式）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(
        main_async(
            Path(args.bb_root),
            args.agent_id,
            args.capabilities,
            args.heartbeat_timeout,
        )
    )


if __name__ == "__main__":
    main()
