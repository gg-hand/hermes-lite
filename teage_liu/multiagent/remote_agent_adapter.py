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
        standard_mode: bool = False,
    ):
        self._local_bb_root = local_bb_root
        self._config = config
        self._agent_id = agent_id
        self._private_key = private_key
        # 标准 A2A 模式：发现走 Agent Card（取代 register_remote_agent 注册 + 心跳）
        self._standard_mode = standard_mode
        self._a2a_client = A2AClient(
            config,
            signer_id=agent_id if private_key else None,
            private_key=private_key,
        )
        self._std_client = None
        if standard_mode:
            try:
                from teage_liu.multiagent.a2a_std.client import StdA2AClient

                self._std_client = StdA2AClient(config)
            except Exception:
                self._std_client = None
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_interval = (
            config.get("multiagent", {}).get("heartbeat_interval_seconds", 10)
        )
        # 记录每个端点是否已成功注册（启动时若对端未就绪，心跳循环会重试）
        self._registered: set[str] = set()

    async def start(self) -> None:
        """启动适配器。"""
        if self._running:
            return
        self._running = True
        if self._standard_mode:
            # 标准模式：Agent Card 发现（幂等缓存），跳过注册/心跳
            for ep in self._a2a_client._endpoints:
                try:
                    if self._std_client is not None:
                        card = await self._std_client.fetch_agent_card(ep["name"])
                        self._registered.add(ep["name"])
                        logger.info(
                            "A2A 标准发现: endpoint=%s card=%s", ep["name"], card.get("name"),
                        )
                except Exception as e:
                    logger.warning("A2A 标准发现 %s 失败: %s", ep["name"], e)
            return
        # 注册到所有远程端点（若对端未就绪则失败，由心跳循环重试）
        for ep in self._a2a_client._endpoints:
            try:
                await self.register_to_remote(ep["name"])
                self._registered.add(ep["name"])
            except A2AClientError as e:
                logger.warning("注册到 %s 失败（心跳循环将重试）: %s", ep["name"], e)

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
        """心跳循环：定期向所有端点发送心跳。

        若某端点启动时注册失败（对端未就绪），每次循环尝试重新注册，
        直到 register_remote_agent 成功为止；之后只发心跳。
        """
        while self._running:
            try:
                for ep in self._a2a_client._endpoints:
                    ep_name = ep["name"]
                    try:
                        if ep_name not in self._registered:
                            # 尚未注册成功：先尝试注册（对端可能已就绪）
                            await self.register_to_remote(ep_name)
                            self._registered.add(ep_name)
                            logger.info("注册到 %s 成功（心跳循环重试）", ep_name)
                        # 注册成功或已注册：发心跳
                        await self._send_heartbeat(ep_name)
                    except A2AClientError as e:
                        logger.warning("心跳到 %s 失败: %s", ep_name, e)
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
