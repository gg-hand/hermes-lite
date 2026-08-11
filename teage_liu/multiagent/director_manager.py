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
import time
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
        self._log_file = None  # stdout/stderr 重定向文件，stop 时关闭
        # director_v2_enabled：watchdog 自动重启字段（B3 修复）
        self._watchdog_task = None
        self._watchdog_running = False
        self._restart_count = 0
        self._max_restarts = config.get("max_restarts", 3)
        self._restart_window = config.get("restart_window_seconds", 300)
        self._restart_times: list[float] = []  # 最近重启时间列表（用于窗口限流）
        # 清理上次运行的 stale PID 文件
        if os.path.exists(self._pid_file):
            stale_pid = self._read_pid_file()
            if stale_pid:
                try:
                    pid_alive = False
                    if sys.platform == "win32":
                        result = subprocess.run(
                            ["tasklist", "/FI", f"PID eq {stale_pid}"],
                            capture_output=True, text=True, timeout=5,
                            creationflags=0x08000000,
                        )
                        pid_alive = str(stale_pid) in result.stdout
                    else:
                        os.kill(stale_pid, 0)
                        pid_alive = True

                    if not pid_alive:
                        os.remove(self._pid_file)
                        logger.info("清理 stale Director PID 文件 (PID=%s)", stale_pid)
                    else:
                        # PID 存活但可能为僵尸进程（心跳停滞）：检查心跳是否 >120s 未更新
                        # 僵尸 Director 会导致 status() 误报 "fault"，需在启动时清理
                        if self._is_director_heartbeat_stale(stale_pid, max_age=120):
                            logger.warning(
                                "检测到僵尸 Director 进程 (PID=%s, 心跳停滞>120s)，正在清理",
                                stale_pid,
                            )
                            self._kill_process(stale_pid)
                            try:
                                os.remove(self._pid_file)
                            except Exception:
                                pass
                except Exception:
                    try:
                        os.remove(self._pid_file)
                    except Exception:
                        pass

    async def start(self) -> dict:
        if await self._is_running():
            pid = self._process.pid if self._process else self._read_pid_file()
            return {"ok": True, "message": "Director 已在运行", "pid": pid}

        # 心跳新鲜度检查：即使本实例未跟踪 Director 进程（如服务器重启后），
        # 若 director.md 心跳在 30s 内，说明前一个 Director 仍在运行并持有锁，
        # 直接 spawn 新进程会因 LockAcquisitionError 崩溃。
        heartbeat = self._read_director_heartbeat()
        if heartbeat:
            try:
                hb_time = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                if age < 30:
                    pid = self._read_pid_file()
                    logger.info("Director 心跳新鲜 (age=%ss)，判定为已在运行，跳过 spawn", round(age, 1))
                    return {"ok": True, "message": "Director 已在运行", "pid": pid}
            except Exception:
                pass

        cmd = [sys.executable, "-m", "teage_liu.multiagent.director_cli", "--bb-root", self._bb_root]
        env = {**os.environ, "HERMES_BB_DIR": self._bb_root}

        # stdout/stderr 重定向到日志文件，避免 PIPE 缓冲区满导致子进程阻塞/崩溃
        # 同时保留日志供崩溃诊断
        log_path = os.path.join(self._bb_root, "director.log")
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")

        kwargs = {
            "env": env,
            "stdout": log_file,
            "stderr": log_file,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            log_file.close()
            logger.error("Director 启动失败: %s", e)
            return {"ok": False, "message": f"启动失败: {e}"}

        # 保存 log_file 引用，stop 时关闭
        self._log_file = log_file

        with open(self._pid_file, "w") as f:
            f.write(str(self._process.pid))

        logger.info("Director 已启动, PID=%s, 日志: %s", self._process.pid, log_path)

        # director_v2_enabled：启动 watchdog 自动重启（B3 修复）
        # 仅当尚无 watchdog 运行时启动，避免 _watchdog 内 restart 触发重复 watchdog
        if self._config.get("director_v2_enabled", True) and self._watchdog_task is None:
            self._watchdog_running = True
            self._watchdog_task = asyncio.create_task(self._watchdog())

        return {"ok": True, "message": "Director 已启动", "pid": self._process.pid}

    async def stop(self) -> dict:
        # director_v2_enabled：先关闭 watchdog，防止 stop 期间触发自动重启（B3 修复）
        self._watchdog_running = False
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

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
        # 关闭日志文件句柄
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

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

    async def _watchdog(self) -> None:
        """周期检查子进程，崩溃自动重启（B3 修复）。

        每 10s 检查 self._process.poll()，若返回非 None 表示子进程已退出：
        - 清理锁文件 / pid 文件（_cleanup_stale_state）
        - 滑动窗口限流：_restart_window 秒内最多重启 _max_restarts 次
        - 达到上限则停止 watchdog
        """
        while self._watchdog_running:
            await asyncio.sleep(10)
            if self._process is None:
                continue
            rc = self._process.poll()
            if rc is not None:
                logger.error("Director 子进程退出 rc=%s", rc)
                self._cleanup_stale_state()
                now = time.time()
                self._restart_times = [
                    t for t in self._restart_times if now - t < self._restart_window
                ]
                if len(self._restart_times) < self._max_restarts:
                    try:
                        await asyncio.sleep(5)  # 重启前等待，避免崩溃风暴
                        result = await self.start()
                        if result.get("ok"):
                            self._restart_times.append(now)
                            self._restart_count += 1
                            logger.info(
                                "Director 自动重启成功 (尝试 %s/%s)",
                                len(self._restart_times), self._max_restarts,
                            )
                        else:
                            logger.warning("自动重启失败: %s", result.get("message"))
                    except Exception:
                        logger.exception("自动重启失败")
                else:
                    logger.error("已达最大重启次数 %s，停止 watchdog", self._max_restarts)
                    self._watchdog_running = False
                    break

    def _cleanup_stale_state(self) -> None:
        """清理上次崩溃留下的锁文件 / pid 文件（B3 修复）。

        Director 子进程异常退出后仍持有 director.lock，新实例无法获取。
        重启前先清理这些 stale 文件。audit.lock 同理（portalocker 文件锁
        在进程崩溃后可能残留）。
        """
        for lock_name in ("director.lock", "audit.lock"):
            lock_path = os.path.join(self._bb_root, "locks", lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except PermissionError:
                    logger.warning("无法删除 %s，可能仍被持有", lock_path)
        if self._pid_file and os.path.exists(self._pid_file):
            try:
                os.remove(self._pid_file)
            except OSError:
                pass

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

    def _is_director_heartbeat_stale(self, pid: int, max_age: int = 120) -> bool:
        """检查 Director 心跳是否停滞（用于判定僵尸进程）。

        Args:
            pid: Director 进程 PID（仅用于日志）
            max_age: 心跳最大允许年龄（秒），默认 120s

        Returns:
            True 表示心跳停滞（僵尸进程）；False 表示心跳正常或无法判定（不杀进程）
        """
        heartbeat = self._read_director_heartbeat()
        if not heartbeat:
            # 无心跳记录：可能是首次启动未写心跳，不判定为僵尸（保守策略）
            return False
        try:
            hb_str = heartbeat.replace("Z", "+00:00") if isinstance(heartbeat, str) else ""
            hb_dt = datetime.fromisoformat(hb_str)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            return age > max_age
        except (ValueError, TypeError):
            # 心跳时间戳解析失败：不判定为僵尸（保守策略，避免误杀）
            return False

    def _kill_process(self, pid: int) -> None:
        """跨平台终止进程（best-effort）。"""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                    creationflags=0x08000000,
                )
            else:
                import signal as _signal
                os.kill(pid, _signal.SIGTERM)
        except Exception as e:
            logger.warning("终止进程 PID=%s 失败: %s", pid, e)


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
