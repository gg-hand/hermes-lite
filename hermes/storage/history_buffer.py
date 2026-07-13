"""短期对话历史管理。

在内存中为每个会话维护固定 N 轮对话历史，支持：
- FIFO 自动截断（超过 max_turns 时丢弃最早消息）
- tool_use/tool_result 配对原子淘汰（避免孤立 tool_result 触发 LLM 400）
- 可选的 JSONL 磁盘持久化与归档回调

token 控制由 Condenser 在 LLM 调用前单点负责，HistoryBuffer 不再
承担 token 溢出降级职责。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


logger = logging.getLogger(__name__)


class HistoryBuffer:
    """短期对话历史缓冲区。

    以 session_id 为键在内存中独立维护每个会话的对话历史，
    超过 max_turns（user 会话）或 cron_max_turns（cron 会话）时
    自动 FIFO 删除最早消息，并保证 tool_use/tool_result 配对原子
    淘汰以避免留下孤立 tool_result 触发 LLM 400 错误。

    token 控制由 Condenser 在 LLM 调用前单点负责，HistoryBuffer 不
    再承担 token 溢出降级职责。

    参数:
        max_turns: user 会话最大保留的消息条数。
        archive_callback: 可选的归档回调，签名 (session_id, evicted_message) -> None。
                          当 FIFO 淘汰最早消息时被调用，用于将淘汰消息
                          归档到长期记忆向量库（type=conversation_turn）。
                          为 None 时纯 FIFO 丢弃（向后兼容）。
                          回调内抛出的异常被捕获并记录 warning，不影响主流程。
        cron_max_turns: cron 会话专用 max_turns（默认 20 条 = 10 轮）。
        persistence_dir: 可选的 JSONL 持久化目录路径。非 None 时启用磁盘
                          持久化：add_message 时追加写入 ``{persistence_dir}/{session_id}.jsonl``，
                          get_history 在内存未命中时从磁盘加载，clear_session
                          同步删除磁盘文件。为 None 时纯内存模式（向后兼容，
                          所有磁盘操作跳过）。磁盘保留全量历史，内存仅保留
                          最近 N 条工作集（FIFO 截断不影响磁盘）。
    """

    def __init__(
        self,
        max_turns: int = 20,
        archive_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        cron_max_turns: int = 20,
        persistence_dir: Optional[str] = None,
    ) -> None:
        self.max_turns = max_turns

        # Phase 8 Task 2.11: cron 会话专用 max_turns（默认 20 条 = 10 轮）。
        # cron session（session_id 以 "cron:" 开头）超过此值时 FIFO 淘汰
        # 最早消息并归档到 ChromaMemoryStore cron namespace。
        # 与 user session 的 max_turns 解耦，避免 cron 高频触发挤压用户会话。
        self.cron_max_turns = cron_max_turns

        # FIFO 淘汰时的归档回调（可选）。回调签名：(session_id, evicted_message) -> None
        # 为 None 时直接丢弃最早消息（向后兼容）；非 None 时在淘汰前调用，
        # 用于将溢出消息归档到 ChromaMemoryStore（type=conversation_turn）。
        # 回调内异常被捕获并记录 warning，不影响 add_message 主流程。
        self.archive_callback: Optional[Callable[[str, Dict[str, Any]], None]] = (
            archive_callback
        )

        # 各会话历史：{session_id: [message, ...]}，最早的在列表头部
        self._histories: Dict[str, List[Dict[str, Any]]] = {}
        # 保护内部 dict/list 的互斥锁（服务端多线程场景）
        self._lock = threading.Lock()

        # JSONL 持久化目录：非 None 时启用磁盘持久化。
        # - add_message：追加写 ``{persistence_dir}/{session_id}.jsonl``
        # - get_history：内存未命中时从磁盘加载
        # - clear_session：删除磁盘文件
        # 为 None 时纯内存模式（向后兼容，所有磁盘操作跳过）。
        # 注意：磁盘保留全量历史，内存仅保留最近 N 条工作集（FIFO 截断不删磁盘）。
        self.persistence_dir: Optional[str] = persistence_dir
        if self.persistence_dir is not None:
            os.makedirs(self.persistence_dir, exist_ok=True)

    @staticmethod
    def _is_tool_use_msg(msg: Dict[str, Any]) -> bool:
        """判断消息是否为含 ``tool_use`` 块的 assistant 消息。

        Phase 9 Task 6：用于 FIFO 淘汰时识别 tool_use/tool_result 配对，
        保证配对原子删除，避免留下孤立 tool_result 触发 LLM 400 错误。

        判定条件：
        - ``role == "assistant"``；
        - ``content`` 为 list；
        - content 中存在 ``{"type": "tool_use", ...}`` 块。

        参数:
            msg: 消息字典。

        返回:
            符合条件返回 True，否则 False（含 content 为 str 的纯文本消息）。
        """
        if msg.get("role") != "assistant":
            return False
        content = msg.get("content")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )

    @staticmethod
    def _is_tool_result_msg(msg: Dict[str, Any]) -> bool:
        """判断消息是否为含 ``tool_result`` 块的 user 消息。

        Phase 9 Task 6：用于 FIFO 淘汰时识别 tool_use/tool_result 配对，
        保证配对原子删除，避免留下孤立 tool_result 触发 LLM 400 错误。

        判定条件：
        - ``role == "user"``；
        - ``content`` 为 list；
        - content 中存在 ``{"type": "tool_result", ...}`` 块。

        参数:
            msg: 消息字典。

        返回:
            符合条件返回 True，否则 False（含 content 为 str 的纯文本消息）。
        """
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )

    def _persist_path(self, session_id: str) -> Optional[Path]:
        """返回指定会话对应的 JSONL 持久化文件路径。

        - ``self.persistence_dir`` 为 None 时返回 None（纯内存模式）。
        - 否则返回 ``Path(persistence_dir) / f"{safe_session_id}.jsonl"``。
          session_id 可能含特殊字符（如 ``cron:abc``），冒号在 Windows
          文件名非法，统一替换为下划线。其他平台也替换以保持跨平台一致。

        参数:
            session_id: 会话 ID

        返回:
            持久化文件路径；纯内存模式返回 None。
        """
        if self.persistence_dir is None:
            return None
        safe_name = session_id.replace(":", "_")
        return Path(self.persistence_dir) / f"{safe_name}.jsonl"

    def _append_to_disk(self, session_id: str, message: Dict[str, Any]) -> None:
        """将单条消息以 JSONL 形式追加写入磁盘。

        - ``persistence_dir`` 为 None 或路径解析失败时直接 return（纯内存模式）。
        - 以 append 模式写入一行 ``json.dumps(message) + "\n"``，
          ``ensure_ascii=False`` 保留中文，``default=str`` 兜底非 JSON 类型
          （如 datetime）。
        - message 的 content 可能是 str 或 Anthropic content block 列表
          （含 tool_use / tool_result dict），json.dumps 能正常序列化 dict 列表。
        - IO 异常 try/except 记 ``logger.warning`` 不抛出，避免磁盘故障
          阻断 add_message 主流程（内存仍正常工作）。

        参数:
            session_id: 会话 ID
            message: 消息字典（含 role/content/timestamp + kwargs）
        """
        if self.persistence_dir is None:
            return
        path = self._persist_path(session_id)
        if path is None:
            return
        try:
            line = json.dumps(message, ensure_ascii=False, default=str) + "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            logger.warning(
                "追加写入磁盘历史失败 session_id=%s path=%s",
                session_id,
                str(path),
                exc_info=True,
            )

    def _load_from_disk(self, session_id: str) -> List[Dict[str, Any]]:
        """从磁盘加载指定会话的全部历史消息。

        - 路径为 None 或文件不存在时返回 ``[]``。
        - 逐行读取，每行 ``json.loads`` 成 message dict，按行序收集。
        - 遇到 ``json.JSONDecodeError``：将文件重命名为
          ``{session_id}.jsonl.corrupt``，记 ``logger.warning``，返回 ``[]``，
          避免损坏文件持续阻塞加载。
        - 其他 IO 异常同样记 warning 返回 ``[]``。

        参数:
            session_id: 会话 ID

        返回:
            消息列表（正序）；无文件或损坏时返回 ``[]``。
        """
        path = self._persist_path(session_id)
        if path is None or not path.exists():
            return []
        messages: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    messages.append(json.loads(line))
        except json.JSONDecodeError:
            # 损坏文件：重命名为 .corrupt 隔离，避免持续阻塞加载
            corrupt_path = path.with_suffix(".jsonl.corrupt")
            try:
                path.rename(corrupt_path)
            except Exception:
                logger.warning(
                    "重命名损坏历史文件失败 path=%s", str(path), exc_info=True
                )
            logger.warning(
                "历史文件损坏已隔离 session_id=%s path=%s -> %s",
                session_id,
                str(path),
                str(corrupt_path),
            )
            return []
        except Exception:
            logger.warning(
                "读取磁盘历史失败 session_id=%s path=%s",
                session_id,
                str(path),
                exc_info=True,
            )
            return []
        return messages

    def add_message(
        self,
        session_id: str,
        role: str,
        content: Union[str, List[Dict[str, Any]]],
        **kwargs: Any,
    ) -> None:
        """向指定会话追加一条消息。

        超过 max_turns 时自动从头部 FIFO 删除最早的消息。
        附加字段（如 tool_name、tool_call_id、timestamp）可通过 kwargs 传入。

        参数:
            session_id: 会话 ID
            role: 消息角色（user / assistant / tool）
            content: 消息内容，可为字符串（纯文本对话）或 Anthropic
                content block 列表（如 ``[{"type": "tool_use", ...}]`` /
                ``[{"type": "tool_result", ...}]``），用于持久化循环内
                完整工具调用消息以便 condenser 压缩。
            **kwargs: 附加字段，合并到消息字典中（可覆盖默认 timestamp）
        """
        message: Dict[str, Any] = {
            "role": role,
            "content": content,
            # 默认附带时间戳，便于排查与持久化；可被 kwargs 覆盖
            "timestamp": datetime.now().isoformat(),
        }
        message.update(kwargs)

        with self._lock:
            history = self._histories.setdefault(session_id, [])
            history.append(message)
            # 持久化：追加写磁盘（全量历史，不受 FIFO 截断影响）。
            # 在锁内调用以保证多线程下磁盘写入顺序与内存一致；
            # _append_to_disk 内部已 try/except，磁盘故障不影响主流程。
            self._append_to_disk(session_id, message)
            # FIFO：超过上限时从头部删除最早消息
            # Phase 8 Task 2.11: cron session 使用 cron_max_turns（默认 20 条 = 10 轮），
            # 与 user session 的 max_turns 解耦。超过部分归档到 ChromaMemoryStore
            # cron namespace（由 archive_callback 处理，回调内路由 namespace）。
            effective_max = (
                self.cron_max_turns
                if session_id.startswith("cron:")
                else self.max_turns
            )
            while len(history) > effective_max:
                # Phase 9 Task 6: 保证 tool_use/tool_result 配对完整性。
                # 头部是 assistant(tool_use) 且紧随 user(tool_result) 时，
                # 配对原子删除两条并归档（不合并摘要），避免留下孤立
                # tool_result 触发 LLM 400 错误：
                #   "Messages with role 'tool' must be a response to a
                #    preceding message with 'tool_calls'"
                # 头部是孤立 tool_result（前面的 tool_use 已被删）时，
                # 也直接清理（else 分支按单条淘汰处理）。
                # 普通文本消息仍按单条 FIFO 淘汰（不受影响）。
                evicted_msgs: List[Dict[str, Any]] = []
                if (
                    len(history) >= 2
                    and self._is_tool_use_msg(history[0])
                    and self._is_tool_result_msg(history[1])
                ):
                    # 配对原子删除：同时删 assistant(tool_use) 与 user(tool_result)
                    evicted_msgs.append(history.pop(0))
                    evicted_msgs.append(history.pop(0))
                else:
                    # 单条淘汰：普通文本消息 或 孤立 tool_result（兜底清理）
                    evicted_msgs.append(history.pop(0))
                # 若配置了归档回调，将淘汰消息交由回调处理
                # （通常写入 ChromaMemoryStore，type=conversation_turn），
                # 便于后续通过向量检索找回相关历史。
                # 回调异常被捕获并记录 warning，不影响 add_message 主流程。
                if self.archive_callback is not None:
                    for evicted in evicted_msgs:
                        try:
                            self.archive_callback(session_id, evicted)
                        except Exception:
                            logger.warning(
                                "归档淘汰消息失败", exc_info=True
                            )

    def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        """获取指定会话的完整历史（按时间正序）。

        返回列表的浅拷贝，避免外部直接修改内部状态。

        启用 persistence_dir 时，内存未命中（session_id 不在 self._histories）
        会先从磁盘加载全部历史，存入内存并按 max_turns/cron_max_turns 做 FIFO
        截断（仅截断内存工作集，磁盘保留全量）。截断掉的旧消息不再调
        archive_callback（它们已在磁盘全量里，且重启场景下归档重复无意义）。

        参数:
            session_id: 会话 ID

        返回:
            消息列表（正序），会话不存在时返回空列表。
        """
        # 快速路径：内存命中直接返回浅拷贝（锁内）
        with self._lock:
            if session_id in self._histories:
                return list(self._histories[session_id])

        # 内存未命中：从磁盘加载（锁外执行 IO，避免持锁阻塞）
        if self.persistence_dir is not None:
            loaded = self._load_from_disk(session_id)
        else:
            loaded = []

        with self._lock:
            # Double-check：并发场景下另一线程可能已加载，避免重复加载
            if session_id in self._histories:
                return list(self._histories[session_id])
            # 加载后内存列表可能超过 max_turns，需 FIFO 截断为工作集。
            # 注意：此处直接 pop 不调 archive_callback（磁盘已保留全量，
            # 且重启场景下 archive 重复无意义）。
            effective_max = (
                self.cron_max_turns
                if session_id.startswith("cron:")
                else self.max_turns
            )
            while len(loaded) > effective_max:
                loaded.pop(0)
            self._histories[session_id] = loaded
            return list(loaded)

    def clear_session(self, session_id: str) -> None:
        """清空指定会话的历史。

        同时删除磁盘 JSONL 文件（若启用持久化且文件存在）。

        参数:
            session_id: 会话 ID
        """
        with self._lock:
            self._histories.pop(session_id, None)
        # 删除磁盘文件（锁外执行 IO）。文件不存在时静默忽略。
        path = self._persist_path(session_id)
        if path is not None and path.exists():
            try:
                path.unlink()
            except FileNotFoundError:
                # 并发场景下文件可能已被删，静默忽略
                pass
            except Exception:
                logger.warning(
                    "删除磁盘历史文件失败 session_id=%s path=%s",
                    session_id,
                    str(path),
                    exc_info=True,
                )

if __name__ == "__main__":
    # —— 简单验证逻辑 ——
    # 1. FIFO 截断：添加 25 条消息后只保留 20 条
    buf = HistoryBuffer(max_turns=20)
    sid = "test-session"
    for i in range(25):
        buf.add_message(sid, "user" if i % 2 == 0 else "assistant", f"消息 {i}")
    history = buf.get_history(sid)
    print(f"[FIFO] 添加 25 条后保留: {len(history)} 条")
    assert len(history) == 20, f"期望 20 条，实际 {len(history)} 条"
    # 保留的应是最新的 20 条（索引 5~24）
    assert history[0]["content"] == "消息 5", f"首条应为消息 5，实际 {history[0]['content']}"
    assert history[-1]["content"] == "消息 24", f"末条应为消息 24，实际 {history[-1]['content']}"
    print("[FIFO] 截断验证通过")

    # 2. clear_session
    buf.clear_session(sid)
    assert buf.get_history(sid) == []
    print("[Clear] clear_session 验证通过")

    print("\n所有验证通过")
