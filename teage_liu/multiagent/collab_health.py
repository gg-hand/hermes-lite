"""协作健康监控(Phase2 A-1/E-1)。

每个 Worker 内一个独立 asyncio 后台任务,只监控本 worker 参与的 active 协作。
去中心化,无单点,无 LLM 调用开销。

判定:
- 停滞(双信号):now - last_msg_time > N(90s) 且 last_round == prev_last_round → warning;
  warning 后再等 W(60s) 仍无进展 → 归档。
  快速路径:若 age > N+W(150s) 直接归档(单次扫描即可完成,适配多 worker 并发)。
- error 死循环(独立):同一协作连续 K(3) 轮 error 消息 → 直接归档(不走宽限期)。

归档幂等:调 archive_collab,已归档返回 False(no-op)。仅本次「真正归档」时
广播 type=announce, action=collab_archived 通知参与者。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teage_liu.multiagent.worker_adapter import WorkerAdapter

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_to_epoch(s: str) -> float:
    """ISO 字符串 → epoch 秒(float)。解析失败返回当前时间。"""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()


class CollabHealthMonitor:
    """单 Worker 的协作健康监控。"""

    def __init__(self, worker: "WorkerAdapter") -> None:
        self._worker = worker
        self._task: asyncio.Task | None = None
        self._running = False
        # cid -> warning 进入时间(epoch 秒,float)
        self._warnings: dict[str, float] = {}
        # cid -> 上次 last_round 快照
        self._prev_last_round: dict[str, int] = {}

    def start(self) -> None:
        cfg = self._worker._config.get("collab_health", {})
        if not cfg.get("enabled", True):
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        cfg = self._worker._config.get("collab_health", {})
        interval = cfg.get("monitor_scan_seconds", 15)
        try:
            while self._running:
                try:
                    await self._scan_once()
                except Exception as e:
                    logger.warning("Worker %s 健康监控扫描异常: %s",
                                   self._worker._agent_id, e)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Worker %s 健康监控已取消", self._worker._agent_id)
            raise

    def _my_active_cids(self) -> set[str]:
        """本 worker 参与的 active 协作(自身 round 状态 ∩ index active)。"""
        participated = set(self._worker._collab_last_sent_round.keys()) \
            | set(self._worker._collab_max_rounds.keys())
        return {c for c in participated if c not in self._worker._archived_collabs}

    async def _scan_once(self) -> set[str]:
        """执行一次扫描,返回本次「真正归档」的 cid 集合。"""
        from teage_liu.multiagent.blackboard import (
            archive_collab, read_all_active_collab_messages,
        )
        cfg = self._worker._config.get("collab_health", {})
        n_warn = cfg.get("stall_warn_seconds", 90)
        w_grace = cfg.get("stall_grace_seconds", 60)
        k_errors = cfg.get("error_round_limit", 3)

        my_cids = self._my_active_cids()
        if not my_cids:
            return set()

        try:
            msgs = await read_all_active_collab_messages(
                self._worker._bb_root, include_archived=False)
        except Exception as e:
            logger.warning("健康监控读取消息失败: %s", e)
            return set()

        archived_now: set[str] = set()
        now = time.time()
        for cid in my_cids:
            cid_msgs = [m for m in msgs if m.get("collab_id") == cid]
            if not cid_msgs:
                continue
            cid_msgs.sort(key=lambda m: (str(m.get("timestamp", "")), m.get("seq", 0)))
            last_msg = cid_msgs[-1]
            last_round = max(
                (m.get("collab_round") for m in cid_msgs
                 if isinstance(m.get("collab_round"), int)),
                default=0,
            )
            # E-1:连续 error 轮数(按 round 去重,同一 round 多条 error 只算 1 轮)
            error_rounds = sorted({
                m.get("collab_round") for m in cid_msgs
                if m.get("error") is True
                and isinstance(m.get("collab_round"), int)
            })
            consecutive = _trailing_consecutive(error_rounds, last_round)
            if consecutive >= k_errors:
                if await self._archive(cid, reason="error_loop"):
                    archived_now.add(cid)
                self._warnings.pop(cid, None)
                continue

            # A-1:停滞双信号
            ts = last_msg.get("timestamp", "")
            last_time = _parse_iso_to_epoch(ts) if ts else now
            age = now - last_time
            prev_round = self._prev_last_round.get(cid, last_round)
            self._prev_last_round[cid] = last_round
            if age > n_warn and last_round == prev_round:
                if cid not in self._warnings:
                    # 快速路径:age 已超过 warn+grace,直接归档(单次扫描完成)
                    # 适配多 worker 并发检测场景(无需两次扫描)
                    if age > n_warn + w_grace:
                        if await self._archive(cid, reason="stall_timeout"):
                            archived_now.add(cid)
                        continue
                    # 进入 warning,等下次扫描越过宽限期再归档
                    self._warnings[cid] = now
                    logger.info("Worker %s 协作 %s 进入停滞 warning(age=%.0fs)",
                                self._worker._agent_id, cid, age)
                    continue
                # 已 warning,检查宽限期
                warn_at = self._warnings[cid]
                if now - warn_at > w_grace:
                    if await self._archive(cid, reason="stall_timeout"):
                        archived_now.add(cid)
                    self._warnings.pop(cid, None)
            else:
                # 有进展,清 warning
                self._warnings.pop(cid, None)
        return archived_now

    async def _archive(self, cid: str, reason: str) -> bool:
        """幂等归档 + 广播通知。返回是否本次真正归档。"""
        from teage_liu.multiagent.blackboard import (
            append_collab_message, archive_collab,
        )
        try:
            was = await archive_collab(self._worker._bb_root, cid)
        except Exception as e:
            logger.warning("Worker %s 归档协作 %s 失败: %s",
                           self._worker._agent_id, cid, e)
            return False
        if not was:
            return False  # 已被他人归档,no-op
        self._worker._archived_collabs.add(cid)
        self._warnings.pop(cid, None)
        self._prev_last_round.pop(cid, None)
        logger.info("Worker %s 归档协作 %s(reason=%s)", self._worker._agent_id, cid, reason)
        # 广播通知参与者(写 announce,其他 worker 轮询拾取)
        try:
            await append_collab_message(self._worker._bb_root, {
                "from": self._worker._agent_id,
                "type": "announce",
                "action": "collab_archived",
                "collab_id": cid,
                "content": f"协作 {cid} 已归档({reason}),停止回应",
                "timestamp": _now_iso(),
            }, collab_id=cid)
        except Exception as e:
            logger.warning("Worker %s 广播归档通知失败: %s", self._worker._agent_id, e)
        return True


def _trailing_consecutive(error_rounds: list[int], last_round: int) -> int:
    """从 last_round 起向前数连续出现 error 的 round 数。"""
    if not error_rounds:
        return 0
    count = 0
    r = last_round
    err_set = set(error_rounds)
    while r in err_set:
        count += 1
        r -= 1
    return count
