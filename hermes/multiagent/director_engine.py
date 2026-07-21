"""Director 引擎：协议执行者 + 心跳监督 + 轮次推进 + 信任分管理。

Director 是 Plugin 而非中心化服务，读取 director.md 规则并周期执行。
支持 agent / script 双形态（director_implementation 字段），启动互斥锁防脑裂，
epoch 机制防旧 Director 写入。

核心机制：
- 启动互斥锁：locks/director.lock 独占锁，防止多 Director 同时运行
- Epoch 机制：每次启动递增 current_epoch，旧 Director 写入会被拒绝
- 硬超时强抢：原 Director tick age > 2×timeout 时，emergency_release 并接管
- 心跳三阶段：healthy / degraded / offline（基于 last_director_tick age）
- Worker 心跳监督：标记 degraded/offline + 强制释放锁
- 轮次推进：round_robin / priority / leader_follower / freeform
- 信任分管理：degraded_threshold / rejected_threshold / force_offline_threshold
- 派生文件 flush：messages.pending.md 幂等 flush（op_id 去重）
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import portalocker
import yaml

from hermes.multiagent.blackboard import (
    Blackboard,
    append_audit,
    append_message,
    atomic_write,
    cas_write_status,
    read_director_md,
    read_json,
)
from hermes.multiagent.exceptions import LockAcquisitionError

logger = logging.getLogger(__name__)


@dataclass
class DirectorHealthState:
    """Director 健康状态（三阶段渐进）。

    level:
        - healthy: age < degraded_threshold
        - degraded: degraded_threshold ≤ age < timeout
        - offline: age ≥ timeout
    """

    level: Literal["healthy", "degraded", "offline"]
    age: timedelta = field(default_factory=lambda: timedelta(0))
    timeout: int = 30


# 信任分阈值（对齐 spec §11 信任分管理）
TRUST_SCORE_INITIAL = 100
TRUST_SCORE_DEGRADED_THRESHOLD = 60
TRUST_SCORE_REJECTED_THRESHOLD = 30
TRUST_SCORE_FORCE_OFFLINE_THRESHOLD = 10
TRUST_SCORE_MAX_SINGLE_DELTA = 5


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式。"""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso_str: str) -> datetime:
    """解析 ISO 时间字符串（兼容 Z 后缀）。"""
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


class DirectorEngine:
    """Director 协议执行者。

    以协程方式周期运行：
    - 监督 Worker 心跳（标记 degraded/offline + 强制释放锁）
    - 推进超时轮次（round_robin / priority / leader_follower / freeform）
    - 仲裁违规（LLM 仲裁器，不可用时降级 priority）
    - 更新自身心跳（last_director_tick）
    - 管理信任分（audit 后更新）
    - flush messages.pending.md（幂等，op_id 去重）

    director_implementation 支持 agent / script 双形态：
    - agent: Director 作为 LLM agent 运行（默认）
    - script: Director 作为确定性脚本运行（无 LLM 调用）
    """

    def __init__(
        self,
        bb_root: Path,
        config: dict,
        agent_id: str = "director_001",
        signature_verifier=None,
    ):
        """初始化 Director 引擎。

        Args:
            bb_root: 黑板根目录
            config: 完整配置字典（含 multiagent.director 段）
            agent_id: Director 的 agent_id
            signature_verifier: 可选的 SignatureVerifier 实例
        """
        self._bb_root = bb_root
        self._config = config.get("multiagent", {}).get("director", {})
        self._agent_id = agent_id
        self._current_epoch = 0
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._mutex_lock_acquired = False
        self._mutex_lock_handle = None
        self._signature_verifier = signature_verifier
        self._tick_interval = 1  # 默认 1 秒
        self._blackboard = Blackboard(bb_root)
        # director.md 读写锁：防止主循环与外部调用并发修改 director.md 导致字段丢失
        self._director_md_lock = asyncio.Lock()

        # 信任分管理阈值
        self._trust_score_initial = TRUST_SCORE_INITIAL
        self._degraded_threshold = TRUST_SCORE_DEGRADED_THRESHOLD
        self._rejected_threshold = TRUST_SCORE_REJECTED_THRESHOLD
        self._force_offline_threshold = TRUST_SCORE_FORCE_OFFLINE_THRESHOLD
        self._max_single_delta = TRUST_SCORE_MAX_SINGLE_DELTA

        # director_implementation 双形态标记（agent / script）
        self._director_implementation = config.get("multiagent", {}).get(
            "director_implementation", "agent"
        )

        # flush 幂等：已处理的 op_id 集合
        self._flushed_op_ids: set[str] = set()

    async def start(self) -> None:
        """启动 Director 引擎。"""
        # 1. 获取启动互斥锁
        await self._acquire_mutex_lock()

        # 2. 递增 epoch
        await self._increment_epoch()

        # 3. 广播启动消息
        await self._broadcast_started()

        # 4. 启动主循环
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("Director 引擎启动，epoch=%d", self._current_epoch)

    async def stop(self) -> None:
        """停止 Director 引擎。"""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        # 释放互斥锁
        await self._release_mutex_lock()
        logger.info("Director 引擎停止")

    async def _acquire_mutex_lock(self) -> None:
        """获取 locks/director.lock 独占锁。

        使用 portalocker 的 LOCK_EX | LOCK_NB 非阻塞尝试。
        失败时进入 _try_hard_preempt 判定是否可强抢。
        """
        lock_path = self._bb_root / "locks" / "director.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 非阻塞尝试获取
            self._mutex_lock_handle = open(lock_path, "w")
            portalocker.lock(
                self._mutex_lock_handle,
                portalocker.LOCK_EX | portalocker.LOCK_NB,
            )
            self._mutex_lock_acquired = True
        except (portalocker.LockException, BlockingIOError, OSError):
            # 锁被持有，检查是否可强抢
            await self._try_hard_preempt(lock_path)

    async def _try_hard_preempt(self, lock_path: Path) -> None:
        """硬超时强抢分支。

        判定逻辑：
        - 无 director.md → 拒绝（无法判定 staleness）
        - last_director_tick 缺失 → 拒绝
        - tick age > 2×timeout → emergency_release 并接管
        - tick age ≤ 2×timeout → 拒绝（原 Director 仍活跃）
        """
        director_md = await read_director_md(self._bb_root)
        if not director_md:
            raise LockAcquisitionError(
                tool_name="director_engine",
                lock_name="director.lock",
                reason="director.lock held and no director.md to check staleness",
                suggestion="清理 locks/director.lock 或等待原 Director 退出",
            )

        last_tick = director_md.get("last_director_tick", "")
        if not last_tick:
            raise LockAcquisitionError(
                tool_name="director_engine",
                lock_name="director.lock",
                reason="director.lock held and last_director_tick missing",
                suggestion="清理 locks/director.lock",
            )

        tick_time = _parse_iso(last_tick)
        age = datetime.now(timezone.utc) - tick_time
        timeout = director_md.get("heartbeat", {}).get("timeout_seconds", 30)

        if age > timedelta(seconds=timeout * 2):
            # 视为原 Director 已死，emergency_release 并强抢
            logger.warning(
                "Director 硬超时强抢：tick age=%s > 2×timeout=%s", age, timeout * 2
            )
            try:
                # 强制获取锁（blocking LOCK_EX，因原持有者已死）
                self._mutex_lock_handle = open(lock_path, "w")
                portalocker.lock(self._mutex_lock_handle, portalocker.LOCK_EX)
                self._mutex_lock_acquired = True
                await append_audit(
                    self._bb_root,
                    {
                        "ts": _now_iso(),
                        "actor": self._agent_id,
                        "action": "emergency_release",
                        "target": "locks/director.lock",
                        "op_id": str(uuid.uuid4()),
                        "epoch": self._current_epoch,
                        "details": {"reason": "holder_presumed_dead", "age_seconds": age.total_seconds()},
                        "prev_hash": "",
                        "hash": "",
                        "signature": "",
                    },
                )
            except portalocker.LockException as e:
                raise LockAcquisitionError(
                    tool_name="director_engine",
                    lock_name="director.lock",
                    reason=f"hard preempt failed: {e}",
                    suggestion="手动清理 locks/director.lock",
                ) from e
        else:
            raise LockAcquisitionError(
                tool_name="director_engine",
                lock_name="director.lock",
                reason=f"director.lock held and tick fresh (age={age.total_seconds():.1f}s)",
                suggestion="等待原 Director 退出",
            )

    async def _release_mutex_lock(self) -> None:
        """释放启动互斥锁。"""
        if self._mutex_lock_handle and self._mutex_lock_acquired:
            try:
                portalocker.unlock(self._mutex_lock_handle)
                self._mutex_lock_handle.close()
            except Exception as e:
                logger.warning("释放 director.lock 失败: %s", e)
            finally:
                self._mutex_lock_acquired = False
                self._mutex_lock_handle = None

    async def _increment_epoch(self) -> None:
        """递增 current_epoch。"""
        async with self._director_md_lock:
            director_md = await read_director_md(self._bb_root)
            old_epoch = director_md.get("current_epoch", 0) if director_md else 0
            self._current_epoch = old_epoch + 1

            # 更新 director.md
            if director_md:
                director_md["current_epoch"] = self._current_epoch
                director_md["epoch_started_at"] = _now_iso()
                director_md["last_director_tick"] = _now_iso()
                director_md["director_id"] = self._agent_id
                director_md["director_implementation"] = self._director_implementation
                await self._write_director_md_unlocked(director_md)

    async def _broadcast_started(self) -> None:
        """广播 director_started 消息。"""
        await append_message(
            self._bb_root,
            {
                "from": self._agent_id,
                "to": "*",
                "type": "system",
                "content": f"director_started, epoch={self._current_epoch}",
                "timestamp": _now_iso(),
                "epoch": self._current_epoch,
                "op_id": str(uuid.uuid4()),
            },
        )

    async def _run_loop(self) -> None:
        """Director 主循环。

        首次迭代前先 sleep tick_interval，避免覆盖 _increment_epoch 刚写入的
        last_director_tick（测试需要在 start() 后立即写入自定义 tick）。
        """
        try:
            while self._running:
                await asyncio.sleep(self._tick_interval)
                await self._update_director_tick()
                await self._check_worker_heartbeats()
                await self._check_turn_timeout()
                await self._flush_pending_messages()
                await self._arbitrate_conflicts()
        except asyncio.CancelledError:
            logger.info("Director 主循环被取消")
            raise

    async def _update_director_tick(self) -> None:
        """更新 director.md.last_director_tick。"""
        async with self._director_md_lock:
            director_md = await read_director_md(self._bb_root)
            if director_md:
                director_md["last_director_tick"] = _now_iso()
                await self._write_director_md_unlocked(director_md)

    async def _write_director_tick(self, tick_iso: str) -> None:
        """写入指定时间的 tick（测试用）。"""
        async with self._director_md_lock:
            director_md = await read_director_md(self._bb_root)
            if director_md:
                director_md["last_director_tick"] = tick_iso
                await self._write_director_md_unlocked(director_md)

    async def _write_director_md(self, director_md: dict) -> None:
        """写入 director.md（加锁，供外部调用）。"""
        async with self._director_md_lock:
            await self._write_director_md_unlocked(director_md)

    async def _write_director_md_unlocked(self, director_md: dict) -> None:
        """写入 director.md（不加锁，供内部已持锁方法调用）。"""
        yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
        content = f"---\n{yaml_str}---\n\n# Director Protocol\n"
        await atomic_write(self._bb_root / "director.md", content)

    async def _check_self_health(self) -> DirectorHealthState:
        """检查自身心跳健康状态（用于 Worker 监督 Director）。"""
        async with self._director_md_lock:
            director_md = await read_director_md(self._bb_root)
            if not director_md:
                return DirectorHealthState(level="offline")

            last_tick = director_md.get("last_director_tick", "")
            if not last_tick:
                return DirectorHealthState(level="offline")

            tick_time = _parse_iso(last_tick)
            age = datetime.now(timezone.utc) - tick_time
            interval = director_md.get("heartbeat", {}).get("interval_seconds", 10)
            timeout = director_md.get("heartbeat", {}).get("timeout_seconds", 30)
            degraded_threshold = self._config.get(
                "degraded_threshold_seconds", interval * 2
            )

        if age < timedelta(seconds=degraded_threshold):
            return DirectorHealthState(level="healthy", age=age, timeout=timeout)
        elif age < timedelta(seconds=timeout):
            await self._audit_degraded(age, timeout)
            return DirectorHealthState(level="degraded", age=age, timeout=timeout)
        else:
            return DirectorHealthState(level="offline", age=age, timeout=timeout)

    async def _audit_degraded(self, age: timedelta, timeout: int) -> None:
        """audit 记录 degraded 状态。"""
        await append_audit(
            self._bb_root,
            {
                "ts": _now_iso(),
                "actor": "director",
                "action": "heartbeat",
                "target": "director.md",
                "op_id": str(uuid.uuid4()),
                "epoch": self._current_epoch,
                "details": {
                    "reason": "director_degraded",
                    "age_seconds": age.total_seconds(),
                    "timeout_seconds": timeout,
                },
                "prev_hash": "",
                "hash": "",
                "signature": "",
            },
        )

    async def _check_worker_heartbeats(self) -> None:
        """监督 Worker 心跳，标记 degraded/offline + 强制释放锁。"""
        from hermes.multiagent.agent_registry import AgentRegistry
        from hermes.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(self._bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()

        for agent in agents:
            if agent.get("agent_id") == self._agent_id:
                continue

            last_heartbeat = agent.get("last_heartbeat", "")
            if not last_heartbeat:
                continue

            heartbeat_time = _parse_iso(last_heartbeat)
            age = datetime.now(timezone.utc) - heartbeat_time
            interval = agent.get("heartbeat_interval_seconds", 10)
            degraded_threshold = interval * 2
            offline_threshold = self._config.get("heartbeat_timeout_seconds", 30)

            if age > timedelta(seconds=offline_threshold):
                await registry.update_agent_status(agent["agent_id"], "offline")
                await self._force_release_locks_for(agent["agent_id"])
                await self._update_trust_score(agent["agent_id"], delta=-2, reason="agent_offline")
                await append_audit(
                    self._bb_root,
                    {
                        "ts": _now_iso(),
                        "actor": self._agent_id,
                        "action": "heartbeat",
                        "target": f"agents/{agent['agent_id']}.md",
                        "op_id": str(uuid.uuid4()),
                        "epoch": self._current_epoch,
                        "details": {
                            "reason": "agent_offline",
                            "age_seconds": age.total_seconds(),
                        },
                        "prev_hash": "",
                        "hash": "",
                        "signature": "",
                    },
                )
            elif age > timedelta(seconds=degraded_threshold):
                await registry.update_agent_status(agent["agent_id"], "degraded")
                await self._update_trust_score(agent["agent_id"], delta=-1, reason="agent_degraded")
                await append_audit(
                    self._bb_root,
                    {
                        "ts": _now_iso(),
                        "actor": self._agent_id,
                        "action": "heartbeat",
                        "target": f"agents/{agent['agent_id']}.md",
                        "op_id": str(uuid.uuid4()),
                        "epoch": self._current_epoch,
                        "details": {
                            "reason": "agent_degraded",
                            "age_seconds": age.total_seconds(),
                        },
                        "prev_hash": "",
                        "hash": "",
                        "signature": "",
                    },
                )

    async def _force_release_locks_for(self, agent_id: str) -> None:
        """强制释放 agent 持有的所有锁。"""
        from hermes.multiagent.file_lock import LockManager

        lock_manager = LockManager(self._bb_root, agent_id=self._agent_id)
        status = read_json(self._bb_root / "status.json")
        locks = status.get("locks", {})

        for lock_name, lock_entry in locks.items():
            if lock_entry.get("holder") == agent_id:
                await lock_manager.release(
                    lock_name,
                    holder=agent_id,
                    fencing_token=lock_entry.get("fencing_token", 0),
                    force=True,
                )

    async def _update_trust_score(
        self, agent_id: str, delta: int, reason: str
    ) -> None:
        """更新 agent 信任分（带阈值检查）。

        阈值：
        - trust_score < degraded_threshold (60) → 标记 agent status=degraded
        - trust_score < rejected_threshold (30) → 拒绝该 agent 的写操作
        - trust_score < force_offline_threshold (10) → 强制下线
        - 单次 delta 不超过 max_single_delta (5)
        """
        from hermes.multiagent.agent_registry import AgentRegistry
        from hermes.multiagent.schema_validator import SchemaValidator

        # 限制单次 delta
        clamped_delta = max(-self._max_single_delta, min(self._max_single_delta, delta))

        registry = AgentRegistry(self._bb_root, SchemaValidator(enabled=False))
        agent = await registry.get_agent(agent_id)
        if not agent:
            return

        current_score = agent.get("trust_score", self._trust_score_initial)
        new_score = max(0, current_score + clamped_delta)

        # 更新 agent_card 中的 trust_score
        agent["trust_score"] = new_score
        agent_file = self._bb_root / "agents" / f"{agent_id}.md"
        if agent_file.exists():
            from hermes.multiagent.blackboard import read_yaml_frontmatter

            frontmatter, body = read_yaml_frontmatter(agent_file)
            frontmatter["trust_score"] = new_score
            yaml_str = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
            content = f"---\n{yaml_str}---\n{body}"
            await atomic_write(agent_file, content)

        # 阈值检查
        if new_score < self._force_offline_threshold:
            await registry.update_agent_status(agent_id, "offline")
        elif new_score < self._rejected_threshold:
            await registry.update_agent_status(agent_id, "rejected")
        elif new_score < self._degraded_threshold:
            await registry.update_agent_status(agent_id, "degraded")

        await append_audit(
            self._bb_root,
            {
                "ts": _now_iso(),
                "actor": self._agent_id,
                "action": "trust_score_update",
                "target": f"agents/{agent_id}.md",
                "op_id": str(uuid.uuid4()),
                "epoch": self._current_epoch,
                "details": {
                    "reason": reason,
                    "old_score": current_score,
                    "new_score": new_score,
                    "delta": clamped_delta,
                },
                "prev_hash": "",
                "hash": "",
                "signature": "",
            },
        )

    async def _check_turn_timeout(self) -> None:
        """检查轮次超时并推进。"""
        status = read_json(self._bb_root / "status.json")
        current_turn = status.get("current_turn")
        if not current_turn:
            return

        started_at = current_turn.get("started_at", "")
        if not started_at:
            return

        started_time = _parse_iso(started_at)
        age = datetime.now(timezone.utc) - started_time
        turn_timeout = self._config.get("turn_timeout_seconds", 30)

        if age > timedelta(seconds=turn_timeout):
            await self._advance_turn()

    async def _advance_turn(self) -> None:
        """推进到下一个 agent（round_robin 模式按 order 列表轮转）。"""
        status = read_json(self._bb_root / "status.json")
        async with self._director_md_lock:
            director_md = await read_director_md(self._bb_root)
            if not director_md:
                return

            turn_policy = director_md.get("turn_policy", {})
            mode = turn_policy.get("mode", "round_robin")
            order = turn_policy.get("order", [])

        if mode == "freeform":
            return  # freeform 不推进

        current_agent = status.get("current_turn", {}).get("agent_id", "")
        if current_agent in order:
            current_idx = order.index(current_agent)
            next_idx = (current_idx + 1) % len(order)
            next_agent = order[next_idx]
        elif order:
            next_agent = order[0]
        else:
            return

        await self._set_current_turn(next_agent)
        await append_audit(
            self._bb_root,
            {
                "ts": _now_iso(),
                "actor": self._agent_id,
                "action": "turn_advance",
                "target": "status.json",
                "op_id": str(uuid.uuid4()),
                "epoch": self._current_epoch,
                "details": {"from": current_agent, "to": next_agent},
                "prev_hash": "",
                "hash": "",
                "signature": "",
            },
        )

    async def _set_current_turn(
        self, agent_id: str, started_at: str | None = None
    ) -> None:
        """设置当前轮次。"""
        status = read_json(self._bb_root / "status.json")
        turn_timeout = self._config.get("turn_timeout_seconds", 30)
        now = _now_iso()
        started = started_at or now
        deadline = (
            _parse_iso(started) + timedelta(seconds=turn_timeout)
        ).isoformat()

        status["current_turn"] = {
            "agent_id": agent_id,
            "started_at": started,
            "deadline_at": deadline,
            "epoch": self._current_epoch,
        }

        # CAS 写入
        await cas_write_status(
            self._bb_root,
            status.get("version", 0),
            status,
            writer_signature="director",
        )

    async def _flush_pending_messages(self) -> None:
        """flush messages.pending.md 到 messages.md（幂等，op_id 去重）。

        流程：
        1. 读取 messages.pending.md 中所有 pending 消息
        2. 过滤已处理的 op_id（幂等）
        3. 将未处理的消息 append 到 messages.md
        4. 清空 messages.pending.md
        5. 记录已处理的 op_id

        幂等性：通过 op_id 集合去重，重复 flush 不会重复写入。
        """
        pending_path = self._bb_root / "messages.pending.md"
        if not pending_path.exists():
            return

        content = pending_path.read_text(encoding="utf-8")
        if not content.strip():
            return

        # 解析 pending 消息（YAML frontmatter 块）
        from hermes.multiagent.blackboard import read_yaml_frontmatter

        # 按 "---\n" 分割多个 frontmatter 块
        parts = content.split("---\n")
        flushed_any = False
        for i in range(1, len(parts), 2):
            if i >= len(parts):
                break
            frontmatter_str = parts[i]
            if not frontmatter_str.strip():
                continue
            try:
                msg = yaml.safe_load(frontmatter_str)
                if not isinstance(msg, dict):
                    continue
                op_id = msg.get("op_id", "")
                if op_id and op_id in self._flushed_op_ids:
                    continue  # 幂等：跳过已处理
                # append 到 messages.md
                await append_message(self._bb_root, msg)
                if op_id:
                    self._flushed_op_ids.add(op_id)
                flushed_any = True
            except yaml.YAMLError:
                continue

        if flushed_any:
            # 清空 pending 文件
            await atomic_write(pending_path, "")
            await append_audit(
                self._bb_root,
                {
                    "ts": _now_iso(),
                    "actor": self._agent_id,
                    "action": "flush_pending",
                    "target": "messages.pending.md",
                    "op_id": str(uuid.uuid4()),
                    "epoch": self._current_epoch,
                    "details": {"reason": "flush_completed"},
                    "prev_hash": "",
                    "hash": "",
                    "signature": "",
                },
            )

    async def _arbitrate_conflicts(self) -> None:
        """仲裁违规（LLM 仲裁器，不可用时降级 priority）。

        Phase 2 基础实现：扫描 messages.replay_candidates.md 中
        arbiter_decision=pending 的记录，调用 LLM 仲裁。
        LLM 不可用时降级为 priority 策略。
        """
        # TODO: Phase 2 后续 Task 实现 LLM 仲裁器
        pass

    async def _read_status(self) -> dict:
        """读取 status.json。"""
        return read_json(self._bb_root / "status.json")

    async def _read_messages(self) -> list[dict]:
        """读取 messages.md。"""
        return await self._blackboard.read_messages()

    async def _read_current_epoch(self) -> int:
        """读取当前 epoch。"""
        director_md = await read_director_md(self._bb_root)
        return director_md.get("current_epoch", 0) if director_md else 0
