"""SDK 消息适配层：统一多通道消息源 + 去重。

设计原则：
- 统一消息源：A2A 入站消息（推入队列）+ collaboration.md 广播轮询（REST API）
- 增量轮询：仅返回 seq 大于 last_seen_seq 的广播消息
- 自动去重：按 message_id 去重（A2A 转发场景下同一消息可能多通道到达）
- 不依赖 A2A Gateway：轮询走工作台标准 REST API
- 可选组件：TeageAgent 不强制依赖，用户可按需使用

消息来源通道：
1. A2A 入站：其他 agent 通过 A2A Server 发来的消息（通过 enqueue_inbound 推入）
2. 广播轮询：collaboration.md 中的 broadcast/announce/request 等非定向消息
   （通过 GET /api/multiagent/collab/messages?after_seq=N 拉取）

统一消息格式：
    source: "a2a" | "broadcast"    # 来源通道
    type: str                       # 消息类型
    from: str                       # 发送者
    to: str                         # 接收者
    content: str                    # 内容
    message_id: str                 # 去重用唯一 ID
    seq: int                        # 广播消息的黑板序号（A2A 入站无此字段）
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class MessageAdapter:
    """消息适配层。

    统一 A2A 入站消息 + collaboration.md 广播轮询，自动去重。
    """

    _MAX_SEEN_IDS = 10000

    def __init__(
        self,
        workbench_endpoint: str,
        collab_id: Optional[str] = None,
        poll_interval_seconds: int = 2,
    ) -> None:
        """初始化消息适配层。

        Args:
            workbench_endpoint: 工作台 REST API URL（如 http://localhost:18400）
            collab_id: 协作 ID（None 表示全局协作空间）
            poll_interval_seconds: 广播轮询间隔（秒）
        """
        self._workbench_endpoint = workbench_endpoint.rstrip("/")
        self._collab_id = collab_id
        self._poll_interval = poll_interval_seconds
        self._last_seen_seq: int = 0
        self._seen_message_ids: set[str] = set()
        self._seen_ids_queue: deque[str] = deque()
        self._inbound_queue: deque[dict] = deque()
        # 即时创建客户端，以便测试可通过 patch.object(adapter._http_client, "get", ...) 注入 mock。
        # _ensure_client() 仍保留以维持原计划 API 形态（此时恒返回此实例）。
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=10)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10)
        return self._http_client

    async def enqueue_inbound(self, message: dict) -> None:
        """推入 A2A 入站消息到内部队列。

        由 A2A Server 的消息回调调用，或用户手动推入。

        Args:
            message: A2A 入站消息 dict（含 from/to/type/content/message_id）
        """
        self._inbound_queue.append({**message, "source": "a2a"})

    async def _poll_broadcast_messages(self) -> list[dict]:
        """轮询 collaboration.md 广播消息（增量）。

        通过工作台 REST API GET /api/multiagent/collab/messages 拉取。

        Returns:
            新消息列表（seq 大于 last_seen_seq 的消息）
        """
        client = self._ensure_client()
        url = f"{self._workbench_endpoint}/api/multiagent/collab/messages"
        params: dict = {"limit": 100, "after_seq": self._last_seen_seq}
        if self._collab_id:
            params["collab_id"] = self._collab_id

        try:
            resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                logger.warning("广播轮询失败: HTTP %d", resp.status_code)
                return []
            data = resp.json()
        except httpx.HTTPError as e:
            logger.warning("广播轮询网络错误: %s", e)
            return []

        all_messages = data.get("messages", [])
        new_messages = [
            {**m, "source": "broadcast"}
            for m in all_messages
            if isinstance(m.get("seq"), int) and m["seq"] > self._last_seen_seq
        ]

        if new_messages:
            self._last_seen_seq = max(m["seq"] for m in new_messages)

        return new_messages

    async def poll_new_messages(self) -> list[dict]:
        """拉取所有通道的新消息（A2A 入站 + 广播轮询），自动去重。

        Returns:
            去重后的新消息列表
        """
        # 收集 A2A 入站消息
        inbound: list[dict] = []
        while self._inbound_queue:
            inbound.append(self._inbound_queue.popleft())

        # 收集广播消息
        broadcast = await self._poll_broadcast_messages()

        # 合并 + 去重
        all_messages = inbound + broadcast
        return self.deduplicate(all_messages)

    def deduplicate(self, messages: list[dict]) -> list[dict]:
        """按 message_id 去重。

        无 message_id 的消息不去重（直接通过）。
        有 message_id 的消息：首次出现保留，重复的丢弃。
        _seen_message_ids 有界（默认 10000），FIFO 淘汰最旧 ID，
        避免长时间运行内存无限增长。

        Args:
            messages: 待去重的消息列表

        Returns:
            去重后的消息列表
        """
        deduped: list[dict] = []
        for m in messages:
            msg_id = m.get("message_id")
            if msg_id:
                if msg_id in self._seen_message_ids:
                    continue
                self._track_seen_id(msg_id)
            deduped.append(m)
        return deduped

    def _track_seen_id(self, msg_id: str) -> None:
        """记录已见 message_id，超过上限时 FIFO 淘汰最旧 ID。"""
        if len(self._seen_ids_queue) >= self._MAX_SEEN_IDS:
            old = self._seen_ids_queue.popleft()
            self._seen_message_ids.discard(old)
        self._seen_message_ids.add(msg_id)
        self._seen_ids_queue.append(msg_id)

    def reset(self) -> None:
        """重置状态（last_seen_seq 归零，清空队列和已见 ID）。

        用于重新连接或切换协作空间时。
        """
        self._last_seen_seq = 0
        self._seen_message_ids.clear()
        self._seen_ids_queue.clear()
        self._inbound_queue.clear()

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
