"""Worker 状态持久化模块。

负责跨重启的"已处理消息"状态读写，支持幂等性保证。

设计原则：
- 原子写入：先写 .tmp 再 os.replace（同步实现，与 load 接口一致）
- 损坏降级：JSON 解析失败时返回默认空状态，记录 warning，不抛异常
- 路径隔离：每个 agent_id 独立 state 文件，避免并发写入冲突
- set <-> list 转换：JSON 不支持 set，序列化时转 list，反序列化时转 set
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teage_liu.multiagent.blackboard import validate_path_safety
from teage_liu.multiagent.exceptions import PathSafetyError, WorkerStateError

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerState:
    """Worker 跨重启状态。

    字段说明：
    - last_collab_seq: 已处理的协作消息最大 seq（兜底，主要靠下面的精确集合）
    - responded_request_seqs: 本 agent 已写过 response 的 request seq 集合
    - processed_msg_seqs: 本 agent 已处理过的协作消息 seq 集合（防止重复入队）
    - processed_urgent_seqs: 本 agent 已处理过的紧急消息 seq 集合（intervention directive）
    - executed_op_ids: 本 agent 已执行过的 A2A 任务 op_id 集合
    - last_updated_at: 最后更新时间（ISO 格式，便于排查）
    - sleep_state: 休眠状态机（"active" | "sleeping"），跨重启保留避免重启后立即全速轮询
    - empty_poll_count: 当前连续空轮询计数（达到阈值后进入休眠）
    - sleep_entered_at: 进入休眠的时间（ISO 格式）；非休眠状态为空字符串
    """
    last_collab_seq: int = 0
    responded_request_seqs: set[int] = field(default_factory=set)
    processed_msg_seqs: set[int] = field(default_factory=set)
    processed_urgent_seqs: set[int] = field(default_factory=set)
    executed_op_ids: set[str] = field(default_factory=set)
    last_updated_at: str = ""
    sleep_state: str = "active"
    empty_poll_count: int = 0
    sleep_entered_at: str = ""
    # Phase2 L-1：LLM 重试队列持久化（避免重启丢失正在重试的 request）。
    # 每项: {"cid": str, "seq": int, "prompt": str, "context_msg": dict, "retry_count": int}
    llm_retry_queue: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """转可 JSON 序列化的 dict（set → list）。"""
        d = asdict(self)
        d["responded_request_seqs"] = sorted(self.responded_request_seqs)
        d["processed_msg_seqs"] = sorted(self.processed_msg_seqs)
        d["processed_urgent_seqs"] = sorted(self.processed_urgent_seqs)
        d["executed_op_ids"] = sorted(self.executed_op_ids)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerState":
        """从 dict 构建（list → set，兼容字段缺失）。"""
        return cls(
            last_collab_seq=int(data.get("last_collab_seq", 0)),
            responded_request_seqs=set(data.get("responded_request_seqs", [])),
            processed_msg_seqs=set(data.get("processed_msg_seqs", [])),
            processed_urgent_seqs=set(data.get("processed_urgent_seqs", [])),
            executed_op_ids=set(data.get("executed_op_ids", [])),
            last_updated_at=str(data.get("last_updated_at", "")),
            sleep_state=str(data.get("sleep_state", "active")),
            empty_poll_count=int(data.get("empty_poll_count", 0)),
            sleep_entered_at=str(data.get("sleep_entered_at", "")),
            llm_retry_queue=list(data.get("llm_retry_queue", [])),
        )


class WorkerStateStore:
    """Worker 状态持久化（按 agent_id 隔离）。

    状态文件路径：{bb_root}/agents/{agent_id}.state.json
    """

    def __init__(self, bb_root: Path, agent_id: str) -> None:
        self._bb_root = Path(bb_root)
        self._agent_id = agent_id
        self._state_path = self._bb_root / "agents" / f"{agent_id}.state.json"

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> WorkerState:
        """加载状态。文件不存在或损坏时返回默认空状态。"""
        if not self._state_path.exists():
            return WorkerState()
        try:
            content = self._state_path.read_text(encoding="utf-8")
            if not content.strip():
                return WorkerState()
            data = json.loads(content)
            return WorkerState.from_dict(data)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(
                "Worker %s state.json 解析失败，使用默认空状态: %s (path=%s)",
                self._agent_id, e, self._state_path,
            )
            return WorkerState()

    def save(self, state: WorkerState) -> None:
        """原子写入状态（同步实现，与 load 接口一致）。

        复用 blackboard.atomic_write 的核心逻辑（先写 .tmp 再 os.replace），
        但用同步 IO（WorkerStateStore 是同步接口，load 也是同步的）。
        """
        state.last_updated_at = _now_iso()
        try:
            validate_path_safety(self._bb_root, self._state_path)
        except PathSafetyError as e:
            raise WorkerStateError(f"state path safety violation: {e}") from e

        content = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # 唯一 .tmp 名：避免并发写入同一 target 时 .tmp 名冲突
        unique_suffix = f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        tmp_path = path.parent / (path.name + unique_suffix)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            # 同目录内 rename 原子（POSIX rename / Windows MoveFileExWithProgress）
            os.replace(tmp_path, path)
        except Exception as e:
            # 清理残留 .tmp 文件
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise WorkerStateError(f"failed to write state: {e}") from e

    def update(self, updates: dict[str, Any]) -> WorkerState:
        """读取 → 合并 → 写入（事务性）。

        Args:
            updates: 字段名到新值的映射。set 类型字段会合并而非替换。
                     如 {"responded_request_seqs": {1, 2}} 会追加 1, 2 到现有集合。

        Returns:
            合并后的新状态
        """
        current = self.load()
        set_fields = {
            "responded_request_seqs",
            "processed_msg_seqs",
            "processed_urgent_seqs",
            "executed_op_ids",
        }
        for key, value in updates.items():
            if key in set_fields:
                getattr(current, key).update(value)
            elif hasattr(current, key):
                setattr(current, key, value)
        self.save(current)
        return current
