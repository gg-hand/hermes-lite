"""黑板目录读写：原子写入 / 路径沙箱 / YAML safe_load / JSONL append。

设计原则：
- atomic_write：先写 .tmp 再 os.replace（同目录内原子 rename）
- append_jsonl：直接 append 模式打开（无 .tmp，因为 rename 会破坏 append-only 语义）
- validate_path_safety：拒绝绝对路径 / .. 穿越 / symlink 逃逸
- read_yaml_frontmatter：必须 safe_load，禁用 yaml.load
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

import aiofiles
import yaml

from teage_liu.multiagent.exceptions import PathSafetyError


async def atomic_write(path: Path, content: str) -> None:
    """原子写入：先写 .tmp 再 os.replace（同目录内原子 rename）。

    使用唯一 .tmp 文件名（含 PID + UUID）避免并发写入同一目标时的竞态条件。
    在 Windows 上，os.replace 要求目标文件未被其他句柄占用，唯一 .tmp 名
    确保两个并发的 atomic_write 不会互相干扰。

    Args:
        path: 目标文件路径（必须已通过 validate_path_safety）
        content: 写入内容
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 唯一 .tmp 名：避免并发写入同一 target 时 .tmp 名冲突
    unique_suffix = f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = path.parent / (path.name + unique_suffix)
    try:
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(content)
            await f.flush()
            os.fsync(f.fileno())
        # 同目录内 rename 原子（POSIX rename / Windows MoveFileExWithProgress）
        os.replace(tmp_path, path)
    except Exception:
        # 清理残留 .tmp 文件
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def validate_path_safety(bb_root: Path, target: Path) -> Path:
    """路径沙箱校验。

    规则：
    - 拒绝绝对路径（target 必须是相对路径或在 bb_root 内）
    - 拒绝 .. 穿越（resolved path 必须在 bb_root 内）
    - 拒绝 symlink 逃逸（target 及其父目录链不得含 symlink）

    Returns:
        规范化后的绝对路径（在 bb_root 内）

    Raises:
        PathSafetyError: 路径违规
    """
    bb_root_resolved = bb_root.resolve()
    target_str = str(target)

    # 1. 检查 symlink 逃逸（在 resolve 之前检查，保留 symlink 检测能力）
    if target.is_symlink():
        raise PathSafetyError(f"symlink escape: {target} is symlink")
    _check_symlink_in_path_chain(bb_root_resolved, target)

    # 2. 判断路径特征
    # Windows 上 Path("/etc/passwd").is_absolute() 返回 False（无盘符），
    # 但这种路径仍会逃逸 bb_root，所以需要额外检测 POSIX 风格根路径
    is_absolute_like = (
        target.is_absolute()
        or target_str.startswith("/")
        or target_str.startswith("\\")
        or (len(target_str) >= 2 and target_str[1] == ":" and target_str[0].isalpha())
    )
    has_traversal = ".." in target.parts

    # 3. 计算解析后的路径
    if target.is_absolute():
        target_resolved = target.resolve()
    else:
        target_resolved = (bb_root_resolved / target).resolve()

    # 4. 检查是否在 bb_root 内
    try:
        target_resolved.relative_to(bb_root_resolved)
    except ValueError:
        if has_traversal:
            raise PathSafetyError(f"path traversal outside bb_root: {target}")
        if is_absolute_like:
            raise PathSafetyError(f"absolute path outside bb_root: {target}")
        raise PathSafetyError(f"path outside bb_root: {target}")

    return target_resolved


def _check_symlink_in_path_chain(bb_root: Path, target: Path) -> None:
    """检查 target 路径链上是否含 symlink（防 symlink 逃逸）。

    检查 bb_root 到 target 之间的所有中间目录组件。
    """
    if target.is_absolute():
        target_abs = target
    else:
        target_abs = bb_root / target

    try:
        rel = target_abs.relative_to(bb_root)
    except ValueError:
        # target 不在 bb_root 下（可能是绝对路径或 .. 穿越），
        # 由后续 absolute/traversal 检查处理
        return

    current = bb_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise PathSafetyError(f"symlink in path chain: {current}")


def read_json(path: Path) -> dict:
    """读取 JSON 文件。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_yaml_frontmatter(path: Path) -> Tuple[dict, str]:
    """读取 Markdown 文件的 YAML frontmatter + body。

    Returns:
        (frontmatter_dict, body_str)

    Note:
        必须用 yaml.safe_load，禁用 yaml.load（防任意代码执行）
    """
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return {}, content

    # 分割 frontmatter 和 body
    parts = content.split("---\n", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_str = parts[1]
    body = parts[2]
    frontmatter = yaml.safe_load(frontmatter_str) or {}
    return frontmatter, body


async def append_jsonl(path: Path, record: dict) -> None:
    """直接 append 模式写入 JSONL 文件（无 .tmp）。

    注意：append-only 语义，不能写 .tmp 再 rename（rename 会覆盖已有内容）。
    串行化由调用方保证（如 audit_logger 通过 portalocker.Lock）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "a", encoding="utf-8") as f:
        await f.write(json.dumps(record, ensure_ascii=False) + "\n")
        await f.flush()
        os.fsync(f.fileno())


# =============================================================================
# Phase 2 新增：Blackboard 类 + 模块级 async 辅助函数
# =============================================================================


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式。"""
    return datetime.now(timezone.utc).isoformat()


class Blackboard:
    """黑板目录封装：init_blackboard 创建目录骨架 + 初始协议文件。

    Phase 2 新增：与 Plan 1 模块级函数共存，提供面向对象入口。
    所有 IO 仍走模块级 atomic_write / append_jsonl / read_yaml_frontmatter。
    """

    def __init__(self, bb_root: Path) -> None:
        self._bb_root = Path(bb_root)

    @property
    def root(self) -> Path:
        return self._bb_root

    async def init_blackboard(self) -> None:
        """初始化黑板目录骨架 + 协议文件。

        创建子目录：agents/ audit/ locks/ tasks/ schemas/ snapshots/
        初始化文件：
        - status.json（CAS version=0 + 所有协议字段）
        - messages.md / messages.pending.md / messages.replay_candidates.md（空）
        - audit/audit.jsonl（空）
        - director.md（初始 frontmatter）
        """
        bb = self._bb_root
        bb.mkdir(parents=True, exist_ok=True)
        for sub in ("agents", "audit", "locks", "tasks", "schemas", "snapshots"):
            (bb / sub).mkdir(exist_ok=True)

        # status.json（仅当不存在时初始化，避免覆盖已有状态）
        status_path = bb / "status.json"
        if not status_path.exists():
            initial_status = {
                "protocol_version": "1.0.0",
                "session_id": "default",
                "phase": "init",
                "version": 0,  # CAS version
                "epoch": 0,
                "current_turn": None,
                "turn_history": [],
                "active_agents": [],
                "locks": {},
                "last_message_seq": 0,
                "last_heartbeat": _now_iso(),
                "director_status": "offline",
                "director_signature": "",
                "last_fencing_token": 0,
                "recovery_started_at": None,
                "recovery_progress": {},
                "recovery_stage": "idle",
                "extensions": {},
            }
            await atomic_write(status_path, json.dumps(initial_status, ensure_ascii=False, indent=2))

        # 空派生文件（messages.md / messages.pending.md / messages.replay_candidates.md）
        for fname in ("messages.md", "messages.pending.md", "messages.replay_candidates.md"):
            fpath = bb / fname
            if not fpath.exists():
                await atomic_write(fpath, "")

        # 空 audit.jsonl
        audit_path = bb / "audit" / "audit.jsonl"
        if not audit_path.exists():
            await atomic_write(audit_path, "")

        # 初始 director.md（仅当不存在）
        director_path = bb / "director.md"
        if not director_path.exists():
            director_md = {
                "director_id": "",
                "director_implementation": "agent",  # agent / script 双形态
                "current_epoch": 0,
                "epoch_started_at": "",
                "last_director_tick": "",
                "heartbeat": {
                    "interval_seconds": 10,
                    "timeout_seconds": 30,
                },
                "turn_policy": {
                    "mode": "round_robin",
                    "order": [],
                },
            }
            yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
            content = f"---\n{yaml_str}---\n\n# Director Protocol\n"
            await atomic_write(director_path, content)

    async def read_messages(self) -> list[dict]:
        """读取 messages.md 中的所有消息（YAML frontmatter 形式）。"""
        return await read_messages(self._bb_root)


async def read_director_md(bb_root: Path) -> dict | None:
    """读取 director.md 的 YAML frontmatter（async 包装）。"""
    director_path = bb_root / "director.md"
    if not director_path.exists():
        return None
    frontmatter, _ = read_yaml_frontmatter(director_path)
    return frontmatter


async def append_message(bb_root: Path, message: dict) -> None:
    """追加消息到 messages.md（YAML frontmatter 格式）。

    每条消息写为独立 frontmatter 块，便于后续按 frontmatter 解析。
    """
    messages_path = bb_root / "messages.md"
    messages_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_str = yaml.safe_dump(message, sort_keys=False, allow_unicode=True)
    content = f"---\n{yaml_str}---\n\n{message.get('content', '')}\n\n"
    async with aiofiles.open(messages_path, "a", encoding="utf-8") as f:
        await f.write(content)
        await f.flush()
        os.fsync(f.fileno())


async def read_messages(bb_root: Path) -> list[dict]:
    """读取 messages.md 中所有消息 frontmatter。

    解析多个 YAML frontmatter 块（每个以 --- 分隔）。
    """
    messages_path = bb_root / "messages.md"
    if not messages_path.exists():
        return []
    content = messages_path.read_text(encoding="utf-8")
    if not content.strip():
        return []

    messages: list[dict] = []
    # 分割多个 frontmatter 块：每个块以 "---\n" 开头，下一个 "---" 结束
    parts = content.split("---\n")
    # 第 0 个为空（开头 "---\n" 之前），奇数索引为 frontmatter，偶数索引为 body
    for i in range(1, len(parts), 2):
        if i >= len(parts):
            break
        frontmatter_str = parts[i]
        if not frontmatter_str.strip():
            continue
        try:
            frontmatter = yaml.safe_load(frontmatter_str)
            if isinstance(frontmatter, dict):
                messages.append(frontmatter)
        except yaml.YAMLError:
            continue
    return messages


async def append_audit(bb_root: Path, record: dict) -> None:
    """追加审计记录到 audit/audit.jsonl（append-only，无 .tmp）。

    内部委托 MultiAgentAuditLogger 以复用 hash chain；若 logger 不可用则降级为直接 append_jsonl。
    """
    try:
        from teage_liu.multiagent.audit_logger import MultiAgentAuditLogger

        logger = MultiAgentAuditLogger(bb_root)
        await logger.append_audit(record)
    except Exception:
        # 降级：直接 append_jsonl（无 hash chain，但保证不阻塞调用方）
        await append_jsonl(bb_root / "audit" / "audit.jsonl", record)


async def read_audit_records(bb_root: Path, limit: int = 100) -> list[dict]:
    """读取最近 N 条审计记录（从 audit/audit.jsonl 倒序读取）。"""
    audit_path = bb_root / "audit" / "audit.jsonl"
    if not audit_path.exists():
        return []
    records: list[dict] = []
    try:
        content = audit_path.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in content.strip().split("\n"):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-limit:]


async def cas_write_status(
    bb_root: Path,
    expected_version: int,
    new_status: dict,
    writer_signature: str = "",
) -> None:
    """CAS 写入 status.json。

    Args:
        bb_root: 黑板根目录
        expected_version: 预期的 version 值
        new_status: 新的 status 字典（写入前会递增 version）
        writer_signature: 写入者签名（用于审计）

    Raises:
        CASVersionMismatchError: version 不匹配
    """
    from teage_liu.multiagent.exceptions import CASVersionMismatchError

    status_path = bb_root / "status.json"
    current = read_json(status_path)
    if current.get("version", 0) != expected_version:
        raise CASVersionMismatchError(
            expected=expected_version,
            actual=current.get("version", 0),
        )
    new_status["version"] = expected_version + 1
    await atomic_write(status_path, json.dumps(new_status, ensure_ascii=False, indent=2))
