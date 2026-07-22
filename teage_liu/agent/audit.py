"""工具调用审计日志，JSONL 持久化 + 内存环形缓冲。

本模块提供 ``AuditLogger``，用于记录每次工具调用的完整轨迹，包括
时间戳、会话 ID、工具名称、输入参数、执行结果、是否出错以及耗时。

Phase 8 Task 4.4 增强：新增 ``decision_source`` / ``schedule_id`` /
``run_id`` 字段，用于 cron 预授权审计追踪。
- ``decision_source``：决策来源（``default_rule`` / ``schedule_grant`` /
  ``user_confirm``），默认 ``default_rule``，向后兼容旧记录。
- ``schedule_id``：cron 调度项 ID（冗余字段，从 ``session_id`` 的 ``cron:``
  前缀提取），用于按调度项加速查询。非 cron 会话为 ``None``。
- ``run_id``：执行批次 ID（可选），用于按执行批次筛选审计记录。

Phase 9 Task 5 增强：新增 ``log_guardrail_decision`` 方法，用于记录护栏
决策（注入扫描拦截/放行等），与工具调用审计区分。
- ``entry_type``：记录类型（``tool_call`` / ``guardrail``），旧记录缺少
  该字段时默认 ``tool_call``（向后兼容）。
- ``log_guardrail_decision`` 记录护栏层（``input_scan`` /
  ``tool_result_sanitize`` / ``output_filter``）的 allow/deny/warn 决策。
- ``get_recent`` 新增可选 ``entry_type`` 参数，支持按记录类型过滤查询。

存储策略：
- 内存环形缓冲（``collections.deque``）：保留最近 N 条记录，供快速查询。
  工具调用与护栏决策共用同一缓冲，通过 ``entry_type`` 字段区分。
- JSONL 文件持久化：以 append 模式追加写入，每行一条 JSON 记录，便于
  离线分析与长期归档。

降级策略：
- 文件写入失败时仅记录 warning 日志，不抛出异常，确保审计模块自身的
  故障不会影响主流程的工具调用。
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 结果字符串截断阈值：超过此长度的 result 将被截断，避免单条日志过大
_MAX_RESULT_LENGTH = 2000

# 默认轮转阈值：audit.jsonl 行数超过此值时轮转
# 可通过 AuditLogger(max_lines=...) 覆盖，或设为 0 禁用轮转
_DEFAULT_MAX_LINES = 50000
# 默认保留的轮转文件数（audit.{timestamp}.jsonl）
_DEFAULT_MAX_FILES = 5
# 触发轮转检查的频率（每 N 次写入检查一次，避免每次 IO 开销）
_ROTATION_CHECK_INTERVAL = 100


class AuditLogger:
    """工具调用审计日志记录器。

    线程安全：通过 ``threading.Lock`` 保护内存缓冲与文件写入，支持多线程
    并发调用 ``log_tool_call``。

    降级策略：文件写入失败时仅记录 warning 日志，不抛出异常，保证审计模块
    自身的故障不会影响主流程。

    存储策略：
    - 内存环形缓冲（``collections.deque(maxlen=buffer_size)``）：保留最近
      N 条记录，供 ``get_recent`` 快速查询。
    - JSONL 文件持久化：以 append 模式追加写入，每行一条 JSON 记录。

    Phase 8 Task 4.4 字段增强：
    - ``log_tool_call`` 新增可选参数 ``decision_source`` / ``schedule_id`` /
      ``run_id``，旧调用方不传时使用默认值（向后兼容）。
    - 旧 JSONL 记录缺少这些字段时，``get_recent`` / ``get_by_schedule`` /
      ``get_by_run_id`` 读取时用 ``.get()`` 填充默认值（``decision_source``
      = ``"default_rule"``，``schedule_id`` / ``run_id`` = ``None``）。

    Phase 9 Task 5 增强：
    - 新增 ``log_guardrail_decision`` 方法，记录护栏层（input_scan /
      tool_result_sanitize / output_filter）的 allow/deny/warn 决策。
    - 护栏记录含 ``entry_type="guardrail"`` 字段，与工具调用记录区分；
      旧工具调用记录无 ``entry_type`` 时默认 ``"tool_call"``（向后兼容）。
    - ``get_recent`` 新增可选 ``entry_type`` 参数，支持按记录类型过滤查询。
    - 护栏记录与工具调用记录共用同一内存环形缓冲与 JSONL 文件。
    """

    def __init__(
        self,
        log_path: str = "data/audit.jsonl",
        buffer_size: int = 1000,
        max_lines: int = _DEFAULT_MAX_LINES,
        max_files: int = _DEFAULT_MAX_FILES,
    ) -> None:
        """初始化审计日志记录器。

        参数:
            log_path: JSONL 日志文件路径，默认 ``data/audit.jsonl``。
                父目录不存在时会自动创建（dirname 为空时跳过）。
            buffer_size: 内存环形缓冲容量，保留最近 N 条记录，默认 1000。
            max_lines: JSONL 文件行数轮转阈值，默认 50000。达到阈值时
                当前文件重命名为 ``audit.{timestamp}.jsonl`` 并开新文件继续
                写入。设为 0 禁用轮转（保持旧行为）。
            max_files: 保留的轮转文件数，默认 5。超过时删除最旧的轮转文件。
        """
        # 确保日志文件父目录存在
        parent_dir = os.path.dirname(log_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # 以 append 模式打开文件句柄，便于多进程/多次运行追加写入
        self._file = open(log_path, "a", encoding="utf-8")
        # 内存环形缓冲：超出容量时自动丢弃最旧记录
        self._buffer: collections.deque = collections.deque(maxlen=buffer_size)
        # 写操作互斥锁，保证线程安全
        self._lock = threading.Lock()
        # 存储日志文件路径
        self._log_path = log_path
        # 轮转配置
        self._max_lines = max_lines
        self._max_files = max_files
        # 写入计数器：每 _ROTATION_CHECK_INTERVAL 次写入触发一次轮转检查
        self._write_count = 0

    def _rotate_if_needed(self) -> None:
        """检查当前 audit.jsonl 行数是否达到阈值，达到则轮转。

        轮转策略：
        1. 关闭当前文件句柄
        2. 重命名为 ``audit.{timestamp}.jsonl``（timestamp 为 UTC 秒级时间戳）
        3. 打开新的空文件继续写入
        4. 清理超过 ``_max_files`` 数量的旧轮转文件

        此方法在 ``self._lock`` 保护下调用，无需额外加锁。
        """
        if self._max_lines <= 0 or self._file is None:
            return
        try:
            # 先 flush 确保所有数据落盘
            self._file.flush()
            # 统计当前文件行数
            with open(self._log_path, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            if line_count < self._max_lines:
                return
            # 触发轮转：关闭当前句柄
            self._file.close()
            # 重命名为带时间戳的归档文件
            ts = int(datetime.now(timezone.utc).timestamp())
            rotated_path = f"{self._log_path}.{ts}.rotated"
            try:
                os.replace(self._log_path, rotated_path)
            except OSError as e:
                logger.warning("audit 轮转重命名失败: %s", e)
                # 回退：重新打开原文件继续追加
                self._file = open(self._log_path, "a", encoding="utf-8")
                return
            # 打开新文件
            self._file = open(self._log_path, "a", encoding="utf-8")
            # 清理超量的旧轮转文件
            self._cleanup_old_rotated_files()
            logger.info(
                "audit.jsonl 已轮转，归档至 %s（%d 行）",
                os.path.basename(rotated_path), line_count,
            )
        except Exception as e:
            logger.warning("audit 轮转检查失败: %s", e)
            # 确保文件句柄可用
            if self._file is None or self._file.closed:
                try:
                    self._file = open(self._log_path, "a", encoding="utf-8")
                except Exception:
                    pass

    def _cleanup_old_rotated_files(self) -> None:
        """清理超过 ``_max_files`` 数量的旧轮转文件。

        按文件名中的时间戳排序，删除最旧的轮转文件。
        文件名格式：``audit.jsonl.{timestamp}.rotated``
        """
        if self._max_files <= 0:
            return
        parent_dir = os.path.dirname(self._log_path) or "."
        base_name = os.path.basename(self._log_path)
        # 收集所有轮转文件
        rotated_files = []
        try:
            for fname in os.listdir(parent_dir):
                if fname.startswith(f"{base_name}.") and fname.endswith(".rotated"):
                    rotated_files.append(os.path.join(parent_dir, fname))
        except OSError:
            return
        # 按修改时间排序（最旧在前）
        rotated_files.sort(key=lambda p: os.path.getmtime(p))
        # 删除超量文件
        excess = len(rotated_files) - self._max_files
        for i in range(excess):
            try:
                os.remove(rotated_files[i])
                logger.info("清理旧 audit 轮转文件: %s", rotated_files[i])
            except OSError as e:
                logger.warning("清理 audit 轮转文件失败: %s", e)

    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict,
        result: str,
        is_error: bool,
        duration_ms: float,
        decision_source: str = "default_rule",
        schedule_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """记录一次工具调用。

        Phase 8 Task 4.4: 新增 ``decision_source`` / ``schedule_id`` /
        ``run_id`` 可选参数，旧调用方不传时使用默认值（向后兼容）。

        参数:
            session_id: 所属会话 ID。
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。
            result: 工具执行结果字符串。超过 ``_MAX_RESULT_LENGTH`` 时将被
                截断（取前 2000 字符并追加 ``...[truncated]``）。
            is_error: 该调用是否出错。
            duration_ms: 调用耗时（毫秒）。
            decision_source: 决策来源（``default_rule`` / ``schedule_grant``
                / ``user_confirm``），默认 ``default_rule``。用于审计追踪
                工具调用是走默认规则、cron 预授权还是用户确认。
            schedule_id: cron 调度项 ID（冗余字段，加速按调度项查询）。
                非 cron 会话为 ``None``。
            run_id: 执行批次 ID（可选），用于按执行批次筛选。cron 调度项
                每次触发可生成一个 run_id，关联该次触发的所有工具调用。
        """
        # 截断过长的结果字符串
        if len(result) > _MAX_RESULT_LENGTH:
            truncated_result = result[:_MAX_RESULT_LENGTH] + "...[truncated]"
        else:
            truncated_result = result

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "result": truncated_result,
            "is_error": is_error,
            "duration_ms": duration_ms,
            # Phase 8 Task 4.4: 新字段（向后兼容：旧调用方不传时使用默认值）
            "decision_source": decision_source,
            "schedule_id": schedule_id,
            "run_id": run_id,
        }

        with self._lock:
            # 将 entry 副本追加到内存缓冲（浅拷贝即可，entry 此后不再修改）
            self._buffer.append(dict(entry))
            # 尝试写入 JSONL 文件，失败时仅 warning 不抛异常
            try:
                self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._file.flush()
                # 轮转检查：每 _ROTATION_CHECK_INTERVAL 次写入检查一次
                self._write_count += 1
                if self._write_count % _ROTATION_CHECK_INTERVAL == 0:
                    self._rotate_if_needed()
            except Exception as e:
                logger.warning("审计日志写入文件失败: %s", e)

    def log_guardrail_decision(
        self,
        layer: str,
        action: str,
        reason: str,
        session_id: str,
        matched_patterns: Optional[List[str]] = None,
        risk_level: str = "medium",
    ) -> None:
        """记录一次护栏决策（Phase 9 Task 5）。

        与 ``log_tool_call`` 区分：护栏决策不是工具调用，而是护栏层
        （注入扫描 / 工具结果消毒 / 输出过滤）对内容做出的 allow/deny/warn
        判定。记录含 ``entry_type="guardrail"`` 字段，与工具调用记录区分。

        参数:
            layer: 护栏层名称，取值：
                - ``"input_scan"``：用户输入注入扫描
                - ``"tool_result_sanitize"``：工具结果消毒
                - ``"output_filter"``：LLM 输出过滤
            action: 决策动作，取值：
                - ``"allow"``：放行
                - ``"deny"``：拦截
                - ``"warn"``：警告但放行
            reason: 决策原因描述（人类可读）。
            session_id: 所属会话 ID。
            matched_patterns: 命中的护栏模式列表（可选）。例如注入扫描
                命中的敏感模式名称列表。``None`` 表示无命中模式。
            risk_level: 风险等级（``"low"`` / ``"medium"`` / ``"high"``），
                默认 ``"medium"``。用于后续按风险等级筛选审计记录。
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "layer": layer,
            "action": action,
            "reason": reason,
            "matched_patterns": matched_patterns,
            "risk_level": risk_level,
            # Phase 9 Task 5: 记录类型字段，与工具调用记录区分
            "entry_type": "guardrail",
        }

        with self._lock:
            # 将 entry 副本追加到内存缓冲（与工具调用共用同一缓冲）
            self._buffer.append(dict(entry))
            # 尝试写入 JSONL 文件，失败时仅 warning 不抛异常
            try:
                self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._file.flush()
                # 轮转检查：每 _ROTATION_CHECK_INTERVAL 次写入检查一次
                self._write_count += 1
                if self._write_count % _ROTATION_CHECK_INTERVAL == 0:
                    self._rotate_if_needed()
            except Exception as e:
                logger.warning("护栏审计日志写入文件失败: %s", e)

    def get_recent(
        self,
        limit: int = 50,
        entry_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取最近的审计记录（工具调用 + 护栏决策）。

        Phase 9 Task 5: 新增 ``entry_type`` 参数支持按记录类型过滤。
        - 不传 ``entry_type``（默认 ``None``）：返回所有类型记录（与旧行为一致）。
        - ``entry_type="tool_call"``：仅返回工具调用记录。
        - ``entry_type="guardrail"``：仅返回护栏决策记录。

        参数:
            limit: 返回的最大条数，默认 50。
            entry_type: 可选记录类型过滤（``"tool_call"`` / ``"guardrail"``）。
                默认 ``None`` 表示不过滤，返回所有记录。

        返回:
            按时间倒序（最新在前）排列的记录列表。返回深拷贝，避免外部
            修改影响内部缓冲。旧记录（缺少 Task 4.4 新字段）会用默认值
            填充：``decision_source="default_rule"`` / ``schedule_id=None``
            / ``run_id=None`` / ``entry_type="tool_call"``。
        """
        with self._lock:
            # 从 deque 末尾向前取（最新在前）
            items = list(reversed(self._buffer))
        # 先按 entry_type 过滤，再应用 limit
        if entry_type is not None:
            items = [
                item for item in items
                if self._entry_type_of(item) == entry_type
            ]
        items = items[:limit]
        # 返回深拷贝，避免外部修改污染内部缓冲
        # 同时为新字段填充默认值（向后兼容旧记录）
        return [self._normalize_entry(item) for item in items]

    @staticmethod
    def _entry_type_of(item: Dict[str, Any]) -> str:
        """获取记录的 ``entry_type``，缺失时默认 ``"tool_call"``。

        向后兼容：旧工具调用记录无 ``entry_type`` 字段，按 ``"tool_call"``
        处理；新护栏记录显式写入 ``entry_type="guardrail"``。
        """
        return item.get("entry_type", "tool_call")

    def get_by_schedule(
        self,
        schedule_id: str,
        limit: int = 50,
        run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按调度项 ID 查询审计记录（Phase 8 Task 4.5）。

        利用 ``schedule_id`` 冗余字段加速查询，从内存缓冲中筛选匹配记录。
        可选 ``run_id`` 参数进一步按执行批次筛选。

        参数:
            schedule_id: 调度项 ID。
            limit: 返回的最大条数，默认 50。
            run_id: 可选执行批次 ID，提供时仅返回该批次的记录。

        返回:
            按时间倒序（最新在前）排列的记录列表，深拷贝。
        """
        with self._lock:
            items = list(reversed(self._buffer))
        result = []
        for item in items:
            # 优先用 schedule_id 冗余字段匹配，回退到 session_id 前缀匹配
            # （兼容旧记录未填充 schedule_id 的场景）
            item_sid = item.get("schedule_id")
            if item_sid is None:
                # 回退：从 session_id 提取（cron: 前缀）
                sid = item.get("session_id", "")
                if sid.startswith("cron:"):
                    item_sid = sid[5:]
            if item_sid != schedule_id:
                continue
            if run_id is not None and item.get("run_id") != run_id:
                continue
            result.append(self._normalize_entry(item))
            if len(result) >= limit:
                break
        return result

    def get_by_run_id(
        self,
        schedule_id: str,
        run_id: str,
    ) -> List[Dict[str, Any]]:
        """按调度项 ID + run_id 查询审计记录（Phase 8 Task 4.5）。

        参数:
            schedule_id: 调度项 ID。
            run_id: 执行批次 ID。

        返回:
            按时间正序（最早在前）排列的记录列表，深拷贝。
        """
        with self._lock:
            items = list(self._buffer)  # 正序
        result = []
        for item in items:
            item_sid = item.get("schedule_id")
            if item_sid is None:
                sid = item.get("session_id", "")
                if sid.startswith("cron:"):
                    item_sid = sid[5:]
            if item_sid != schedule_id:
                continue
            if item.get("run_id") != run_id:
                continue
            result.append(self._normalize_entry(item))
        return result

    @staticmethod
    def _normalize_entry(item: Dict[str, Any]) -> Dict[str, Any]:
        """为旧记录填充新字段默认值（向后兼容）。

        旧 JSONL 记录 / 旧内存缓冲项可能缺少 ``decision_source`` /
        ``schedule_id`` / ``run_id`` 字段（Phase 8 Task 4.4）以及
        ``entry_type`` 字段（Phase 9 Task 5），此方法用默认值填充并返回深拷贝。

        参数:
            item: 原始记录 dict。

        返回:
            含所有字段的深拷贝记录。
        """
        copy = json.loads(json.dumps(item))
        if "decision_source" not in copy:
            copy["decision_source"] = "default_rule"
        if "schedule_id" not in copy:
            copy["schedule_id"] = None
        if "run_id" not in copy:
            copy["run_id"] = None
        # Phase 9 Task 5: 旧工具调用记录无 entry_type 时默认 "tool_call"
        if "entry_type" not in copy:
            copy["entry_type"] = "tool_call"
        return copy

    def _get_rotated_files(self) -> List[str]:
        """返回所有 .rotated 文件路径列表，按文件名时间戳升序（旧在前）。

        文件名格式：``audit.jsonl.{timestamp}.rotated``
        """
        parent_dir = os.path.dirname(self._log_path) or "."
        base_name = os.path.basename(self._log_path)
        rotated_files = []
        try:
            for fname in os.listdir(parent_dir):
                if fname.startswith(f"{base_name}.") and fname.endswith(".rotated"):
                    rotated_files.append(os.path.join(parent_dir, fname))
        except OSError:
            pass
        # 按文件名中的时间戳排序（旧在前）
        rotated_files.sort()
        return rotated_files

    def read_since(
        self,
        since: float,
        limit: int = 1000,
        entry_type: Optional[str] = None,
        include_rotated: bool = False,
    ) -> List[Dict[str, Any]]:
        """增量读取磁盘 audit.jsonl 中 ``timestamp >= since`` 的记录。

        用于按游标（cursor）增量消费审计日志：调用方先通过 ``get_cursor``
        拿到最新时间戳，下次调用 ``read_since(cursor)`` 仅获取新增条目。

        线程安全设计选择：本方法不加 ``self._lock``，用独立的文件句柄读取。
        原因：``log_tool_call`` 在 ``self._lock`` 内 write+flush，若本方法也
        加锁，会导致写操作阻塞等待读完成。由于每行是完整 JSON + ``\\n``，
        且 ``write`` 是单次原子调用，读取方最多读到半行 JSON，会被
        ``json.loads`` 跳过。这是可接受的降级——下次调用会读到完整行。

        参数:
            since: unix epoch seconds（float），过滤 timestamp >= since。
                传 0.0 表示读取全部。含等于以便幂等重试。
            limit: 返回的最大条数，默认 1000。达到即停止扫描。
            entry_type: 可选记录类型过滤（``"tool_call"`` / ``"guardrail"``）。
                默认 ``None`` 不过滤。
            include_rotated: 是否扫描 ``.rotated`` 轮转归档文件。``False``
                （默认）只读当前 ``audit.jsonl``（增量场景）。``True`` 时先
                扫描所有 ``.rotated`` 文件（旧在前），再扫描当前文件，合并
                结果后按时间正序返回。用于全量读取 ``read_since(0)`` 场景。

        返回:
            按时间正序（最早在前）排列的记录列表，便于增量消费。
            损坏的 JSON 行 / 缺失 timestamp 字段的行会被跳过（不抛异常）。
            返回深拷贝，并通过 ``_normalize_entry`` 填充新字段默认值。
        """
        result: List[Dict[str, Any]] = []
        # P1-1 修复：include_rotated=True 时先扫描 .rotated 文件
        files_to_scan = []
        if include_rotated:
            files_to_scan.extend(self._get_rotated_files())
        files_to_scan.append(self._log_path)

        for file_path in files_to_scan:
            if len(result) >= limit:
                break
            if not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if len(result) >= limit:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        ts_str = entry.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str).timestamp()
                        except (ValueError, TypeError):
                            continue
                        if ts < since:
                            continue
                        if entry_type is not None:
                            if self._entry_type_of(entry) != entry_type:
                                continue
                        result.append(self._normalize_entry(entry))
            except OSError as e:
                logger.warning("read_since 读取 %s 失败: %s", file_path, e)

        # include_rotated 时需要按 timestamp 排序（跨文件合并）
        if include_rotated and len(result) > 1:
            result.sort(key=lambda e: e.get("timestamp", ""))
        return result

    def get_cursor(self) -> float:
        """返回 audit.jsonl 中最新记录的 unix epoch seconds，作为下次增量起点。

        P1-1 修复：扫描当前文件 + 所有 ``.rotated`` 轮转归档文件取最大
        timestamp，确保跨轮转文件也能正确返回最新游标。
        空文件 / 文件不存在 / 无有效 timestamp 时返回 0.0。
        """
        max_ts = 0.0
        # 扫描当前文件 + 所有 .rotated 文件
        files_to_scan = self._get_rotated_files() + [self._log_path]
        for file_path in files_to_scan:
            if not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        ts_str = entry.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str).timestamp()
                        except (ValueError, TypeError):
                            continue
                        if ts > max_ts:
                            max_ts = ts
            except OSError as e:
                logger.warning("get_cursor 读取 %s 失败: %s", file_path, e)
        return max_ts

    def close(self) -> None:
        """关闭审计日志文件句柄。

        幂等：多次调用不会抛出异常。
        """
        with self._lock:
            if self._file is not None and not self._file.closed:
                self._file.close()
            self._file = None
