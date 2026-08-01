"""黑板目录读写：原子写入 / 路径沙箱 / YAML safe_load / JSONL append。

设计原则：
- atomic_write：先写 .tmp 再 os.replace（同目录内原子 rename）
- append_jsonl：直接 append 模式打开（无 .tmp，因为 rename 会破坏 append-only 语义）
- validate_path_safety：拒绝绝对路径 / .. 穿越 / symlink 逃逸
- read_yaml_frontmatter：必须 safe_load，禁用 yaml.load
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# 跨进程文件锁平台原语（条件导入，保证跨平台可用）
try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None

try:
    import fcntl  # Unix
except ImportError:
    fcntl = None

import aiofiles
import yaml

from teage_liu.multiagent.collab_sanitize import sanitize_collab_content
from teage_liu.multiagent.exceptions import PathSafetyError

logger = logging.getLogger(__name__)


# =============================================================================
# P3-1 / P3-2 / P3-3 运行时开关
# 默认开启，可通过 set_collab_*_enabled(False) 关闭（供测试与回滚使用）。
# =============================================================================
_COLLAB_SANITIZE_ENABLED = True
_COLLAB_CONSENSUS_DEDUP_ENABLED = True
_COLLAB_SAME_ROUND_DEDUP_ENABLED = True


def set_collab_sanitize_enabled(enabled: bool) -> None:
    """设置协作消息内容净化开关（P3-1）。"""
    global _COLLAB_SANITIZE_ENABLED
    _COLLAB_SANITIZE_ENABLED = bool(enabled)


def set_collab_consensus_dedup_enabled(enabled: bool) -> None:
    """设置 consensus 熔断开关（P3-2）。"""
    global _COLLAB_CONSENSUS_DEDUP_ENABLED
    _COLLAB_CONSENSUS_DEDUP_ENABLED = bool(enabled)


def set_collab_same_round_dedup_enabled(enabled: bool) -> None:
    """设置同 round 同 from 写入层闸门开关（P3-3）。"""
    global _COLLAB_SAME_ROUND_DEDUP_ENABLED
    _COLLAB_SAME_ROUND_DEDUP_ENABLED = bool(enabled)


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


def read_json(path: Path, retries: int = 3, retry_delay: float = 0.05) -> dict:
    """读取 JSON 文件（带重试 + 异常保护）。

    并发写入（atomic_write 的 os.replace）的极小窗口内，同步读可能读到
    空内容或中间态。retries + retry_delay 覆盖该竞态窗口。

    Args:
        path: JSON 文件路径
        retries: 重试次数（默认 3）
        retry_delay: 重试间隔秒数（默认 50ms）

    Returns:
        解析后的 dict；文件不存在/损坏/空时返回空 dict（fail-open）
    """
    if not path.exists():
        return {}

    for attempt in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                # 空内容（可能读到 rename 中间态），重试
                if attempt < retries - 1:
                    import time as _time
                    _time.sleep(retry_delay)
                    continue
                return {}
            return json.loads(content)
        except json.JSONDecodeError:
            if attempt < retries - 1:
                import time as _time
                _time.sleep(retry_delay)
                continue
            logger.warning("read_json 解析失败（已重试 %d 次）: %s", retries, path)
            return {}
        except OSError as e:
            logger.warning("read_json 读取失败: %s, error=%s", path, e)
            return {}
    return {}


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


async def append_message(bb_root: Path, message: dict, validate: bool = False) -> None:
    """追加消息到 messages.md（YAML frontmatter 格式）。

    每条消息写为独立 frontmatter 块，便于后续按 frontmatter 解析。

    Args:
        bb_root: 黑板根目录
        message: 消息字典。若缺失 seq 字段，自动分配（last_seq + 1）
        validate: 是否调用 SchemaValidator 验证消息格式（默认 False 兼容现有）
    """
    # 自动分配 seq（如果缺失）
    if "seq" not in message or message.get("seq") is None:
        last_seq = await _read_last_message_seq(bb_root)
        message = {**message, "seq": last_seq + 1}

    # 可选 schema 验证
    if validate:
        from teage_liu.multiagent.schema_validator import SchemaValidator

        validator = SchemaValidator(enabled=True)
        validator.validate_messages_record(message)

    messages_path = bb_root / "messages.md"
    messages_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_str = yaml.safe_dump(message, sort_keys=False, allow_unicode=True)
    content = f"---\n{yaml_str}---\n\n{message.get('content', '')}\n\n"
    async with aiofiles.open(messages_path, "a", encoding="utf-8") as f:
        await f.write(content)
        await f.flush()
        os.fsync(f.fileno())


async def _read_last_message_seq(bb_root: Path) -> int:
    """读取 messages.md 最后一条消息的 seq。

    Returns:
        最后一条消息的 seq；若文件为空或不存在返回 0
    """
    messages_path = bb_root / "messages.md"
    if not messages_path.exists():
        return 0
    content = messages_path.read_text(encoding="utf-8")
    records = _parse_frontmatter_blocks(content)
    last_seq = 0
    for r in records:
        seq = r.get("seq", 0)
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
    return last_seq


def _parse_frontmatter_blocks(content: str) -> list[dict]:
    """解析多个 YAML frontmatter 块（每个以 --- 分隔）。

    供 read_messages 和 read_collab_messages 共用，避免逻辑重复（A1 修复）。
    遍历所有非空块，body 内容（非 dict）会被自动跳过。
    """
    if not content.strip():
        return []
    records: list[dict] = []
    parts = content.split("---\n")
    # 从1开始（跳过开头空块），遍历所有非空块
    # body 内容不是 dict，会被 isinstance 检查跳过
    for i in range(1, len(parts)):
        frontmatter_str = parts[i].strip()
        if not frontmatter_str:
            continue
        try:
            frontmatter = yaml.safe_load(frontmatter_str)
            if isinstance(frontmatter, dict):
                records.append(frontmatter)
        except yaml.YAMLError:
            continue
    return records


async def read_messages(bb_root: Path) -> list[dict]:
    """读取 messages.md 中所有消息 frontmatter。

    解析多个 YAML frontmatter 块（每个以 --- 分隔）。
    """
    messages_path = bb_root / "messages.md"
    if not messages_path.exists():
        return []
    content = messages_path.read_text(encoding="utf-8")
    return _parse_frontmatter_blocks(content)


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


# =============================================================================
# 协作消息（collaboration.md + collabs/{collab_id}.md）
# Task 2 新增：全局协作空间 + 单协作文件，asyncio.Lock 串行化写入
# =============================================================================

import asyncio


def _get_collab_file(bb_root: Path, collab_id: Optional[str] = None) -> Path:
    """获取协作消息文件路径。

    Args:
        bb_root: 黑板根目录
        collab_id: 协作 ID。None → 全局 collaboration.md；否则 → collabs/{collab_id}.md
    """
    if collab_id:
        collabs_dir = bb_root / "collabs"
        collabs_dir.mkdir(parents=True, exist_ok=True)
        return collabs_dir / f"{collab_id}.md"
    else:
        return bb_root / "collaboration.md"


async def read_collab_messages(
    bb_root: Path,
    collab_id: Optional[str] = None,
    before_seq: Optional[int] = None,
    after_seq: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """读取协作消息（全局或指定协作）。

    复用 _parse_frontmatter_blocks 解析逻辑（A1 修复）。

    Args:
        bb_root: 黑板根目录
        collab_id: 协作 ID。None → 全局 collaboration.md；否则 → collabs/{collab_id}.md
        before_seq: 游标分页，仅返回 seq 严格小于此值的消息；None 表示不限制
        after_seq: 增量游标，仅返回 seq 严格大于此值的消息；None 表示不限制
        limit: 返回数量上限，取最新的 N 条；None 或 0 表示不限制

    Returns:
        消息列表。before_seq/after_seq 过滤后再按 seq 升序返回；
        limit 截取尾部最新 N 条。
    """
    file_path = _get_collab_file(bb_root, collab_id)
    if not file_path.exists():
        return []
    content = file_path.read_text(encoding="utf-8")
    messages = _parse_frontmatter_blocks(content)

    # before_seq 过滤（严格小于）
    if before_seq is not None:
        messages = [m for m in messages
                    if isinstance(m.get("seq"), int) and m["seq"] < before_seq]

    # after_seq 过滤（严格大于）
    if after_seq is not None:
        messages = [m for m in messages
                    if isinstance(m.get("seq"), int) and m["seq"] > after_seq]

    # limit 截断（取最新 N 条）
    if limit is not None and limit > 0:
        messages = messages[-limit:] if len(messages) > limit else messages

    return messages


async def _read_last_collab_seq(
    bb_root: Path, collab_id: Optional[str] = None
) -> int:
    """读取最后一条协作消息的 seq。"""
    messages = await read_collab_messages(bb_root, collab_id)
    if not messages:
        return 0
    return max(
        (m.get("seq", 0) for m in messages if isinstance(m.get("seq"), int)),
        default=0,
    )


def _acquire_cross_process_lock(lock_path: Path) -> int:
    """获取跨进程文件锁（阻塞）。

    - Windows: msvcrt.locking(fd, LK_LOCK, 1)，重试 10 次/秒后抛 OSError。
    - Unix: fcntl.flock(fd, LOCK_EX) 阻塞获取。
    - 锁整个文件用 length=1、offset=0（锁文件仅为互斥令牌，非数据文件）。

    Args:
        lock_path: 锁文件路径，由调用方按 collab_id 命名。

    Returns:
        os 层文件描述符（int），用于释放锁。
    """
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    os.lseek(fd, 0, os.SEEK_SET)  # 确保从偏移 0 开始
    if msvcrt is not None:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    elif fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    # 无可用原语时退化为仅持 fd（极少数平台），仍保证接口可用
    return fd


def _release_cross_process_lock(lock_handle: int) -> None:
    """释放跨进程文件锁并关闭描述符。"""
    if lock_handle is None:
        return
    try:
        if msvcrt is not None:
            try:
                os.lseek(lock_handle, 0, os.SEEK_SET)
                msvcrt.locking(lock_handle, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        elif fcntl is not None:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(lock_handle)
        except OSError:
            pass


class CollabWriter:
    """协作消息写入器，使用 asyncio.Lock 串行化所有写入。

    所有写入通过全局单例 _get_global_writer 获取，确保锁互斥（A2 修复）。
    append 返回 (seq, deduplicated) 元组，deduplicated=True 表示 message_id 去重。
    """

    def __init__(self, bb_root: Path):
        self._bb_root = bb_root
        self._lock = asyncio.Lock()

    async def append(
        self, message: dict, collab_id: Optional[str] = None
    ) -> tuple[int, bool]:
        """串行化写入消息，返回 (seq, deduplicated)。

        - 如果 message 存在且 message_id 已存在，返回 (existing_seq, True)
        - 否则分配新 seq 并写入，返回 (new_seq, False)
        - D6 修复：自动添加 timestamp（ISO 格式），如果消息没有的话

        跨进程文件锁在外层（附录 C 第 6 条），asyncio.Lock 在内层，
        避免持锁顺序不一致导致死锁。
        """
        # 跨进程文件锁（外层）：按 collab_id 单独加锁，避免全局串行
        lock_path = self._bb_root / f".collab.{collab_id or 'global'}.lock"
        cp_lock = await asyncio.to_thread(_acquire_cross_process_lock, lock_path)
        try:
            async with self._lock:  # 进程内锁（内层）
                # 检查 message_id 去重
                message_id = message.get("message_id")
                if message_id:
                    existing = await self._find_by_message_id(message_id, collab_id)
                    if existing is not None:
                        return existing.get("seq", 0), True

                # P3-3：同 round 同 from 写入层闸门——若该协作已有
                # (from==本worker 且 collab_round==本次round 且 type in response/consensus)
                # 的消息，丢弃新的连发写入。修复"同一 worker 在同一 round 内多次 LLM
                # 调用各发1条"导致的连发（工具层 _collab_round_sent 是 per-LLM-call 重置，
                # 无法跨 LLM 调用去重，故在写入层兜底）。必须在跨进程文件锁内检查。
                if (_COLLAB_SAME_ROUND_DEDUP_ENABLED
                        and collab_id
                        and message.get("type") in ("response", "consensus")
                        and message.get("collab_round") is not None):
                    prior = await self._find_same_round_same_from(
                        collab_id,
                        message.get("from", ""),
                        message.get("collab_round"),
                    )
                    if prior is not None:
                        logger.info(
                            "同round闸门：collab %s 已有 %s round=%s seq=%s 的 %s，丢弃连发写入",
                            collab_id, message.get("from", ""),
                            message.get("collab_round"), prior.get("seq", 0),
                            prior.get("type", ""),
                        )
                        return prior.get("seq", 0), True

                # P3-2：consensus 熔断——若该协作已有 consensus/end 终止信号，
                # 丢弃所有后续 response/consensus 写入。原仅拦截 consensus 类型，
                # 但 LLM 常在文本说"终止协作"却不调工具，fallback 代写为 response，
                # 导致 consensus 后继续多轮 response 循环（协作 0b11ed517e1f seq15-18）。
                # 必须在跨进程文件锁内检查，确保跨 worker 互斥。
                if (_COLLAB_CONSENSUS_DEDUP_ENABLED
                        and message.get("type") in ("response", "consensus")
                        and collab_id):
                    prior = await self._find_terminator(collab_id)
                    if prior is not None:
                        logger.info(
                            "consensus 熔断：collab %s 已有终止信号 seq=%s，丢弃 %s 写入",
                            collab_id, prior.get("seq", 0),
                            message.get("type", ""),
                        )
                        return prior.get("seq", 0), True

                # D6 修复：自动添加 timestamp（如果没有）
                if "timestamp" not in message:
                    message = {
                        **message,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

                # 自动生成 message_id（如果缺失），保证每条消息有全局唯一标识。
                # 用于 worker 跨进程去重，避免 per-collab seq 碰撞导致消息被误跳过。
                if not message.get("message_id"):
                    import uuid as _uuid
                    sender = message.get("from", "unknown")
                    ts = message.get("timestamp", "")[:19].replace(":", "").replace("-", "")
                    message = {
                        **message,
                        "message_id": f"{sender}_{ts}_{_uuid.uuid4().hex[:8]}",
                    }

                # 分配 seq
                last_seq = await _read_last_collab_seq(self._bb_root, collab_id)
                message = {**message, "seq": last_seq + 1}

                # 写入文件（YAML frontmatter 块，格式与 messages.md 一致）
                file_path = _get_collab_file(self._bb_root, collab_id)
                yaml_str = yaml.safe_dump(
                    message, allow_unicode=True, default_flow_style=False, sort_keys=False
                )
                block = f"---\n{yaml_str}---\n\n"
                async with aiofiles.open(file_path, "a", encoding="utf-8") as f:
                    await f.write(block)
                    await f.flush()
                    os.fsync(f.fileno())

                # Task 12：带 collab_id 的 request 自动更新索引（best-effort，失败不影响消息写入）
                if collab_id and message.get("type") == "request":
                    try:
                        await _update_collab_index_locked(
                            self._bb_root, collab_id,
                            title=message.get("content", "")[:50],
                            status="initiated",
                            participants=[message.get("from", "")] if message.get("from") else [],
                        )
                    except Exception:
                        pass  # 索引更新失败不影响消息写入

                return message["seq"], False
        finally:
            await asyncio.to_thread(_release_cross_process_lock, cp_lock)

    async def _find_by_message_id(
        self, message_id: str, collab_id: Optional[str] = None
    ) -> Optional[dict]:
        """查找已存在的相同 message_id 消息。"""
        messages = await read_collab_messages(self._bb_root, collab_id)
        for m in messages:
            if m.get("message_id") == message_id:
                return m
        return None

    async def _find_terminator(
        self, collab_id: str
    ) -> Optional[dict]:
        """查找该协作已存在的终止信号消息（type=consensus 或 end）。

        供 P3-2 consensus 熔断使用：若已有终止信号，新的 consensus 写入被丢弃。
        """
        messages = await read_collab_messages(self._bb_root, collab_id)
        for m in messages:
            if m.get("type") in ("consensus", "end"):
                return m
        return None

    async def _find_same_round_same_from(
        self, collab_id: str, sender: str, round_num
    ) -> Optional[dict]:
        """查找该协作中已存在的、同一 sender 在同一 collab_round 发出的
        response/consensus 消息。

        供 P3-3 写入层闸门使用：若已存在，新的同 round 同 from 连发写入被丢弃，
        避免"同一 worker 在同一 round 内多次 LLM 调用各发1条"导致连发。
        """
        messages = await read_collab_messages(self._bb_root, collab_id)
        for m in messages:
            if (m.get("from") == sender
                    and m.get("collab_round") == round_num
                    and m.get("type") in ("response", "consensus")):
                return m
        return None


# 全局 CollabWriter 单例（按 bb_root 缓存）
_writers: dict[Path, "CollabWriter"] = {}


def _get_global_writer(bb_root: Path) -> "CollabWriter":
    """获取全局 CollabWriter 单例（按 bb_root 缓存）。

    所有写入必须通过此函数获取 writer，确保锁互斥（A2 修复）。
    """
    # 用 resolve() 规范化路径，避免不同字符串形式导致多个 writer
    key = bb_root.resolve()
    if key not in _writers:
        _writers[key] = CollabWriter(bb_root)
    return _writers[key]


async def append_collab_message(
    bb_root: Path, message: dict, collab_id: Optional[str] = None
) -> tuple[int, bool]:
    """写入协作消息（串行化，带 message_id 去重）。

    Returns:
        (seq, deduplicated) 元组。deduplicated=True 表示 message_id 已存在，返回旧 seq。

    P3-1：写入前对 type in (response, consensus, request) 的 content 净化，
    剥离 LLM 思考性开头段落与工具元语言污染（空结果兜底保留原文）。
    """
    # P3-1：内容净化（仅对含正文的协作类型生效）
    if (_COLLAB_SANITIZE_ENABLED
            and message.get("type") in ("response", "consensus", "request")):
        content = message.get("content")
        if isinstance(content, str) and content:
            sanitized = sanitize_collab_content(content)
            if sanitized != content:
                message = {**message, "content": sanitized}
    writer = _get_global_writer(bb_root)
    return await writer.append(message, collab_id)


# =============================================================================
# Task 12：collabs/index.md 索引维护
# 协作元数据（collab_id / title / status / participants）的 upsert 读写
# 复用全局 CollabWriter 的锁确保串行化
# =============================================================================


def _get_collab_index_file(bb_root: Path) -> Path:
    """获取协作索引文件路径：collabs/index.md"""
    index_path = bb_root / "collabs" / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    return index_path


async def read_collab_index(bb_root: Path) -> list[dict]:
    """读取 collabs/index.md 中的所有协作元数据。

    Returns:
        协作条目列表，每条含 collab_id / title / status / participants。
        文件不存在时返回空列表。
    """
    index_path = _get_collab_index_file(bb_root)
    if not index_path.exists():
        return []
    content = index_path.read_text(encoding="utf-8")
    return _parse_frontmatter_blocks(content)


async def _update_collab_index_locked(
    bb_root: Path,
    collab_id: str,
    title: str,
    status: str,
    participants: list[str],
) -> None:
    """更新协作索引（upsert）——内部函数，假设调用方已持有 CollabWriter 的锁。

    - 若 collab_id 已存在，更新该条目
    - 否则追加新条目
    - 整个 index.md 重写（原子写入）
    """
    index_path = _get_collab_index_file(bb_root)
    entries = await read_collab_index(bb_root)

    new_entry = {
        "collab_id": collab_id,
        "title": title,
        "status": status,
        "participants": participants,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    found = False
    for i, e in enumerate(entries):
        if e.get("collab_id") == collab_id:
            # 保留 created_at（若存在）
            if e.get("created_at"):
                new_entry["created_at"] = e["created_at"]
            entries[i] = new_entry
            found = True
            break
    if not found:
        new_entry["created_at"] = new_entry["updated_at"]
        entries.append(new_entry)

    # 重写整个 index.md（多 frontmatter 块格式）
    content = ""
    for e in entries:
        yaml_str = yaml.safe_dump(
            e, allow_unicode=True, default_flow_style=False, sort_keys=False
        )
        content += f"---\n{yaml_str}---\n\n"

    await atomic_write(index_path, content)


async def update_collab_index(
    bb_root: Path,
    collab_id: str,
    title: str,
    status: str,
    participants: list[str],
) -> None:
    """更新协作索引（upsert，公共接口）。

    通过全局 CollabWriter 的锁串行化，避免并发写入冲突。
    """
    writer = _get_global_writer(bb_root)
    async with writer._lock:
        await _update_collab_index_locked(
            bb_root, collab_id, title, status, participants
        )


async def archive_collab(bb_root: Path, collab_id: str) -> bool:
    """归档协作：将 index 中该 collab_id 的 status 置为 archived（幂等）。

    保留原 title / participants / created_at，仅更新 status 与 updated_at。
    协作结束（consensus/end）时由 worker_adapter 调用；Phase2 起也由
    CollabHealthMonitor 检测停滞/死循环时调用。文件留在 collabs/{collab_id}.md
    供历史查询（?collab_id=X），默认 active 聚合流（/messages 无 collab_id）
    不再包含归档协作。

    Returns:
        True 若本次「真正归档」(从非 archived → archived);
        False 若 collab_id 不在 index 中,或已被归档(no-op)。
        多 worker 并发检测停滞时,仅首个返回 True,其余 no-op 无冲突。
    """
    writer = _get_global_writer(bb_root)
    async with writer._lock:
        entries = await read_collab_index(bb_root)
        target = None
        for e in entries:
            if e.get("collab_id") == collab_id:
                target = e
                break
        if target is None:
            return False
        if target.get("status") == "archived":
            return False  # 已归档,no-op
        await _update_collab_index_locked(
            bb_root, collab_id,
            title=target.get("title", ""),
            status="archived",
            participants=target.get("participants", []),
        )
        return True


async def list_active_collab_ids(bb_root: Path) -> list[str]:
    """返回所有进行中的 collab_id 列表（非 archived）。

    包含 initiated（刚发起）和 active（进行中）状态，仅排除 archived。
    """
    entries = await read_collab_index(bb_root)
    return [
        e["collab_id"] for e in entries
        if e.get("status") != "archived" and e.get("collab_id")
    ]


async def list_all_collab_ids(bb_root: Path) -> list[str]:
    """返回所有 collab_id 列表（含 archived）。

    供工作台全局视图聚合使用，让归档协作的消息也能在工作台展示。
    """
    entries = await read_collab_index(bb_root)
    return [e["collab_id"] for e in entries if e.get("collab_id")]


async def read_all_active_collab_messages(
    bb_root: Path, include_archived: bool = False,
) -> list[dict]:
    """聚合读取所有协作的消息 + 全局消息，按 (timestamp, seq) 排序合并。

    供工作台 /messages /events /sse 聚合视图使用。

    Args:
        bb_root: 黑板根目录。
        include_archived: 是否包含归档协作的消息。
            - False（默认，Phase1 I-1）：仅聚合 active 协作，归档协作消息被隐藏，
              避免归档后误写入复活；契合 _handle_collab_message 入口硬阻断。
            - True：聚合 active + archived 协作，工作台全局视图可见历史归档。
            查询特定协作（含归档）请用 read_collab_messages(collab_id=X)。
    """
    all_msgs: list[dict] = []
    # 1. 全局消息（游离 announce / relay 等，未归属任何 collab）
    try:
        all_msgs.extend(await read_collab_messages(bb_root))
    except Exception:
        pass
    # 2. 各协作的消息（按 include_archived 决定是否含归档）
    cids = (
        await list_all_collab_ids(bb_root)
        if include_archived
        else await list_active_collab_ids(bb_root)
    )
    for cid in cids:
        try:
            all_msgs.extend(await read_collab_messages(bb_root, collab_id=cid))
        except Exception:
            continue
    # 3. 按 (timestamp, seq) 排序合并；timestamp 缺失时兜底空串排前
    all_msgs.sort(key=lambda m: (str(m.get("timestamp", "")), m.get("seq", 0)))
    return all_msgs

