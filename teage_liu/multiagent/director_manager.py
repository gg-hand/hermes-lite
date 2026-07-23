"""Director 生命周期管理抽象层。

当前实现 LocalDirectorManager（本地子进程）。
未来扩展 RemoteDirectorManager（远程 HTTP 连接）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class DirectorManager(ABC):
    """Director 生命周期管理抽象层。"""

    @abstractmethod
    async def start(self) -> dict:
        """启动 Director。"""

    @abstractmethod
    async def stop(self) -> dict:
        """停止 Director。"""

    @abstractmethod
    async def status(self) -> dict:
        """查询 Director 运行状态。"""

    @abstractmethod
    async def restart(self) -> dict:
        """重启 Director。"""


class LocalDirectorManager(DirectorManager):
    """本地子进程模式：spawn director_cli 作为子进程。"""

    def __init__(self, bb_root: str, config: dict):
        self._bb_root = bb_root
        self._config = config
        self._process: subprocess.Popen | None = None
        self._pid_file = os.path.join(bb_root, "director.pid")
        # 清理上次运行的 stale PID 文件
        if os.path.exists(self._pid_file):
            stale_pid = self._read_pid_file()
            if stale_pid:
                try:
                    if sys.platform == "win32":
                        result = subprocess.run(
                            ["tasklist", "/FI", f"PID eq {stale_pid}"],
                            capture_output=True, text=True, timeout=5,
                            creationflags=0x08000000,
                        )
                        if str(stale_pid) not in result.stdout:
                            os.remove(self._pid_file)
                            logger.info("清理 stale Director PID 文件 (PID=%s)", stale_pid)
                    else:
                        os.kill(stale_pid, 0)
                except Exception:
                    try:
                        os.remove(self._pid_file)
                    except Exception:
                        pass

    async def start(self) -> dict:
        if await self._is_running():
            pid = self._process.pid if self._process else self._read_pid_file()
            return {"ok": True, "message": "Director 已在运行", "pid": pid}

        cmd = [sys.executable, "-m", "teage_liu.multiagent.director_cli"]
        env = {**os.environ, "HERMES_BB_DIR": self._bb_root}

        kwargs = {
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            logger.error("Director 启动失败: %s", e)
            return {"ok": False, "message": f"启动失败: {e}"}

        with open(self._pid_file, "w") as f:
            f.write(str(self._process.pid))

        logger.info("Director 已启动, PID=%s", self._process.pid)
        return {"ok": True, "message": "Director 已启动", "pid": self._process.pid}

    async def stop(self) -> dict:
        if not await self._is_running():
            self._process = None
            if os.path.exists(self._pid_file):
                os.remove(self._pid_file)
            return {"ok": True, "message": "Director 未运行"}

        pid = self._process.pid if self._process else self._read_pid_file()
        if not pid:
            return {"ok": False, "message": "无法获取 PID"}

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=0x08000000,
                )
            else:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    if not await self._is_running():
                        break
                    await asyncio.sleep(0.1)
                if await self._is_running():
                    os.kill(pid, signal.SIGKILL)
        except Exception as e:
            logger.warning("停止 Director 异常: %s", e)

        self._process = None
        if os.path.exists(self._pid_file):
            os.remove(self._pid_file)

        logger.info("Director 已停止")
        return {"ok": True, "message": "Director 已停止"}

    async def status(self) -> dict:
        running = await self._is_running()
        heartbeat = self._read_director_heartbeat()
        pid = self._process.pid if self._process else self._read_pid_file()
        pid_file_exists = os.path.exists(self._pid_file)

        state = "stopped"
        if running and heartbeat:
            # 检查心跳是否在 30 秒内
            try:
                hb_time = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                state = "healthy" if age < 30 else "degraded"
            except Exception:
                state = "degraded"
        elif running:
            state = "starting"
        elif pid_file_exists:
            state = "crashed"

        return {
            "running": running,
            "state": state,
            "pid": pid,
            "last_heartbeat": heartbeat,
        }

    async def restart(self) -> dict:
        await self.stop()
        await asyncio.sleep(1)
        return await self.start()

    async def _is_running(self) -> bool:
        if self._process is not None:
            return self._process.poll() is None
        pid = self._read_pid_file()
        if not pid:
            return False
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=0x08000000,
                )
                return str(pid) in result.stdout
            else:
                os.kill(pid, 0)
                return True
        except Exception:
            return False

    def _read_pid_file(self) -> int | None:
        try:
            with open(self._pid_file, "r") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _read_director_heartbeat(self) -> str | None:
        """读 blackboard/director.md 的 last_director_tick 字段。"""
        director_md = os.path.join(self._bb_root, "director.md")
        try:
            if not os.path.exists(director_md):
                return None
            with open(director_md, "r", encoding="utf-8") as f:
                content = f.read()
            # 解析 YAML frontmatter
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    frontmatter = content[3:end]
                    for line in frontmatter.split("\n"):
                        if "last_director_tick" in line:
                            return line.split(":", 1)[1].strip().strip("'\"")
        except Exception:
            pass
        return None


def create_director_manager(config: dict) -> DirectorManager:
    """工厂函数：根据配置创建 Director 管理器。"""
    multiagent_cfg = config.get("multiagent", {}) or {}
    bb_dir = multiagent_cfg.get("blackboard_dir", "data/blackboard")
    bb_root = str(Path(bb_dir).resolve())
    mode = multiagent_cfg.get("director_mode", "local")

    if mode == "remote":
        # 未来扩展：RemoteDirectorManager
        # endpoint = multiagent_cfg.get("director_endpoint", "")
        # return RemoteDirectorManager(endpoint)
        logger.warning("remote 模式尚未实现，回退到 local")
        return LocalDirectorManager(bb_root, multiagent_cfg)

    return LocalDirectorManager(bb_root, multiagent_cfg)
