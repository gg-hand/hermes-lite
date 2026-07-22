"""远程 agent 适配器：通过 A2A Gateway 与远程 blackboard 交互。

与本地 WorkerAdapter 的差异：
- 通过 HTTP/JSON-RPC 调用远程 Gateway，而非直接读写文件
- 自动签名所有写操作（ed25519）
- 心跳通过 call_method("heartbeat") 实现
- 锁通过 call_method("acquire_lock") 获取
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teage_liu.multiagent.a2a_client import A2AClient, A2AClientError

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RemoteAgentAdapter:
    """远程 agent 适配器。"""

    def __init__(
        self,
        local_bb_root: Path,
        config: dict,
        agent_id: str,
        private_key=None,
    ):
        self._local_bb_root = local_bb_root
        self._config = config
        self._agent_id = agent_id
        self._private_key = private_key
        self._a2a_client = A2AClient(
            config,
            signer_id=agent_id if private_key else None,
            private_key=private_key,
        )
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_interval = (
            config.get("multiagent", {}).get("heartbeat_interval_seconds", 10)
        )

    async def start(self) -> None:
        """启动适配器。"""
        if self._running:
            return
        self._running = True
        # 注册到所有远程端点
        for ep in self._a2a_client._endpoints:
            try:
                await self.register_to_remote(ep["name"])
            except A2AClientError as e:
                logger.warning("注册到 %s 失败: %s", ep["name"], e)

        # 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("RemoteAgentAdapter 启动 (agent_id=%s)", self._agent_id)

    async def stop(self) -> None:
        """停止适配器。"""
        if not self._running:
            return
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        await self._a2a_client.close()
        logger.info("RemoteAgentAdapter 停止")

    async def register_to_remote(self, endpoint_name: str) -> dict:
        """注册到远程 blackboard。"""
        return await self._a2a_client.call_method(
            endpoint_name,
            "register_remote_agent",
            {
                "agent_id": self._agent_id,
                "role": "worker",
                "capabilities": self._config.get("multiagent", {}).get("capabilities", []),
                "heartbeat_interval_seconds": self._heartbeat_interval,
            },
        )

    async def _send_heartbeat(self, endpoint_name: str) -> None:
        """发送心跳到远程端点。"""
        await self._a2a_client.call_method(
            endpoint_name, "heartbeat",
            {"agent_id": self._agent_id, "ts": _now_iso()},
        )

    async def _heartbeat_loop(self) -> None:
        """心跳循环：定期向所有端点发送心跳。"""
        while self._running:
            try:
                for ep in self._a2a_client._endpoints:
                    try:
                        await self._send_heartbeat(ep["name"])
                    except A2AClientError as e:
                        logger.warning("心跳到 %s 失败: %s", ep["name"], e)
            except Exception as e:
                logger.error("心跳循环异常: %s", e)
            await asyncio.sleep(self._heartbeat_interval)

    async def read_remote_messages(
        self, endpoint_name: str, limit: int = 100
    ) -> list[dict]:
        """读取远程 messages.md。"""
        result = await self._a2a_client.call_method(
            endpoint_name, "read_messages", {"limit": limit}
        )
        return result.get("messages", [])

    async def append_remote_message(
        self, endpoint_name: str, message: dict
    ) -> dict:
        """向远程 blackboard 追加消息（带签名）。"""
        params: dict[str, Any] = {"message": message}
        # 显式签名：使签名在 params 中可见（即使 call_method 被 mock）
        if self._private_key is not None:
            import json as _json
            canonical = _json.dumps({"message": message}, sort_keys=True, ensure_ascii=False).encode("utf-8")
            params["signature"] = self._private_key.sign(canonical).hex()
        return await self._a2a_client.call_method(
            endpoint_name, "append_message", params
        )

    async def acquire_remote_lock(
        self,
        endpoint_name: str,
        lock_name: str,
        fencing_token: int,
        ttl_seconds: int = 30,
    ) -> dict:
        """获取远程锁。"""
        return await self._a2a_client.call_method(
            endpoint_name, "acquire_lock",
            {
                "lock_name": lock_name,
                "agent_id": self._agent_id,
                "fencing_token": fencing_token,
                "ttl_seconds": ttl_seconds,
            },
        )

    async def release_remote_lock(
        self, endpoint_name: str, lock_name: str, fencing_token: int = 0
    ) -> dict:
        """释放远程锁。"""
        return await self._a2a_client.call_method(
            endpoint_name, "release_lock",
            {
                "lock_name": lock_name,
                "agent_id": self._agent_id,
                "fencing_token": fencing_token,
            },
        )
