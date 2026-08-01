"""SDK TeageAgent 核心类：外部 agent 接入工作台的主入口。

提供完整的 agent 生命周期管理：
- A2A Server 启动/停止（接收点对点消息）
- A2A Client 通信（发送消息给其他 agent）
- 消息归档到工作台（Forward API）
- Director 上下文注入（搭便车机制）
- 注册与心跳
- 优雅退出

使用示例：
    from teage_liu.sdk.agent import TeageAgent

    agent = TeageAgent(
        agent_id="my_agent",
        capabilities=["research"],
        private_key=private_key,
        bb_root="/path/to/blackboard",
        forward_endpoint="http://workbench:18400",
        remote_endpoints=[{"name": "agent_bob", "url": "http://agent_bob:18401"}],
        a2a_server_port=18401,
    )
    await agent.start()
    try:
        while True:
            # 处理入站消息（通过 on_message 回调）
            # 调用其他 agent：await agent.send_message("hi", to_agent="agent_bob")
            # 获取 Director 引导：context = await agent.get_directive_context()
            await asyncio.sleep(2)
    finally:
        await agent.stop()
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from teage_liu.multiagent.a2a_server import A2AServer
from teage_liu.multiagent.director_injection import DirectorInjector
from teage_liu.sdk.transport import A2ATransport

logger = logging.getLogger(__name__)


class TeageAgent:
    """Teage Agent SDK 核心类。

    封装 agent 侧完整能力：A2A 通信、消息归档、Director 注入、生命周期管理。
    不管理任务——任务协调由 agent 间通过 A2A 自主协商。
    """

    def __init__(
        self,
        agent_id: str,
        capabilities: list[str] | None = None,
        private_key=None,
        bb_root: str | None = None,
        forward_endpoint: str = "",
        remote_endpoints: list[dict] | None = None,
        a2a_server_host: str = "0.0.0.0",
        a2a_server_port: int = 18401,
        heartbeat_interval_seconds: int = 10,
        transport: A2ATransport | None = None,
        on_collab_archived: "Callable[[str], None] | None" = None,
        on_collab_error: "Callable[[str, int, str], None] | None" = None,
    ) -> None:
        """初始化 agent。

        Args:
            agent_id: 本 agent 的唯一 ID（3-32 字符，小写字母+数字+下划线）
            capabilities: 能力列表（如 ["research", "analysis"]）
            private_key: ed25519 私钥（写操作签名用）
            bb_root: 黑板根目录路径（本地 agent 用，DirectorInjector 轮询 directive）
            forward_endpoint: 工作台 Forward API URL（如 http://workbench:18400）
            remote_endpoints: 其他 agent 的 A2A Server 端点列表
                              [{"name": "agent_bob", "url": "http://bob:18401"}]
            a2a_server_host: 本 agent A2A Server 监听地址
            a2a_server_port: 本 agent A2A Server 监听端口
            heartbeat_interval_seconds: 心跳间隔（秒）
            transport: 可选注入的传输层（测试用）
            on_collab_archived: S5 G2 可选回调，拉取到 type=announce,
                                action=collab_archived 消息时触发（参数: collab_id）
            on_collab_error: S5 G4 可选回调，拉取到 type=error 消息时触发
                             （参数: collab_id, collab_round, content）
        """
        self._agent_id = agent_id
        self._capabilities = capabilities or []
        self._heartbeat_interval = heartbeat_interval_seconds
        self._a2a_server_host = a2a_server_host
        self._a2a_server_port = a2a_server_port
        self._bb_root = Path(bb_root) if bb_root else None
        self._forward_endpoint = forward_endpoint

        # 传输层（A2A Client + Forward API，允许注入便于测试）
        self._transport = transport or A2ATransport(
            agent_id=agent_id,
            private_key=private_key,
            bb_root=self._bb_root,
            forward_endpoint=forward_endpoint,
            remote_endpoints=remote_endpoints,
        )

        # A2A Server（接收其他 agent 的点对点消息）
        self._a2a_server = A2AServer(
            agent_id=agent_id,
            bb_root=self._bb_root or Path("."),
            capabilities=self._capabilities,
        )
        self._a2a_server.on_message_callback = self._on_message_received

        # Director 注入器（搭便车机制，轮询 directive 注入 LLM 上下文）
        self._director_injector = DirectorInjector(
            bb_root=self._bb_root or Path("."),
            agent_id=agent_id,
        )

        # 生命周期状态
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._a2a_server_task: asyncio.Task | None = None
        self._uvicorn_server: Optional["uvicorn.Server"] = None

        # 消息回调（留给用户实现，A2A Server 收到消息时触发）
        self.on_message: Optional[Callable[[dict], Awaitable[None]]] = None

        # S5 G2/G4: 协作事件可选回调（默认 None，不传时 _dispatch_collab_event no-op）
        self._on_collab_archived = on_collab_archived
        self._on_collab_error = on_collab_error

    async def _on_message_received(self, message: dict) -> None:
        """A2A Server 收到消息后的内部回调。

        仅触发用户的 on_message 回调。消息归档由发送方负责（send_message 后
        自动 forward），接收方不重复归档，避免同一消息被 collaboration.md 归档两次。

        S5 G2/G3/G4：在 on_message 之前先调 _dispatch_collab_event，识别协作事件
        （collab_archived / error）并触发对应回调。回调异常不阻断 on_message。
        """
        # S5: 先分发协作事件（不阻断 on_message）
        try:
            await self._dispatch_collab_event(message)
        except Exception as e:
            logger.warning("_dispatch_collab_event 异常: %s", e)
        if self.on_message:
            try:
                await self.on_message(message)
            except Exception as e:
                logger.warning("on_message 回调异常: %s", e)

    async def start(self) -> None:
        """启动 agent：A2A Server + 注册 + 心跳循环。"""
        if self._running:
            return
        self._running = True

        # 启动 A2A Server（接收其他 agent 的点对点消息）
        from fastapi import FastAPI
        import uvicorn

        app = FastAPI()
        app.include_router(self._a2a_server.create_router())
        config = uvicorn.Config(
            app,
            host=self._a2a_server_host,
            port=self._a2a_server_port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._a2a_server_task = asyncio.create_task(self._uvicorn_server.serve())

        # 注册到工作台 A2A Gateway
        await self.register()

        # 启动心跳循环
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info(
            "TeageAgent %s 已启动（A2A Server: %s:%d）",
            self._agent_id, self._a2a_server_host, self._a2a_server_port,
        )

    async def stop(self) -> None:
        """停止 agent：A2A Server + 心跳 + 传输层。"""
        if not self._running:
            return
        self._running = False

        # 优雅关闭 A2A Server（触发 serve() 自然退出）
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        if self._a2a_server_task:
            try:
                await asyncio.wait_for(self._a2a_server_task, timeout=5)
            except asyncio.TimeoutError:
                # 兜底：5秒未退出则强制 cancel
                self._a2a_server_task.cancel()
                try:
                    await self._a2a_server_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            self._a2a_server_task = None
        self._uvicorn_server = None

        # 停止心跳
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # 关闭传输层
        await self._transport.close()
        logger.info("TeageAgent %s 已停止", self._agent_id)

    async def register(self) -> dict:
        """注册到工作台 A2A Gateway。

        调用 A2A Gateway 的 register_remote_agent 方法，将本 agent 的
        ID、能力、A2A Server 端点注册到工作台。
        """
        return await self._transport.call_agent(
            "gateway",  # "gateway" 需在 remote_endpoints 中配置
            "register_remote_agent",
            {
                "agent_id": self._agent_id,
                "role": "worker",
                "capabilities": self._capabilities,
                "heartbeat_interval_seconds": self._heartbeat_interval,
                "a2a_endpoint": f"http://{self._a2a_server_host}:{self._a2a_server_port}",
            },
        )

    async def _heartbeat_loop(self) -> None:
        """心跳循环：向工作台 A2A Gateway 上报状态。"""
        try:
            while self._running:
                try:
                    await self._transport.call_agent(
                        "gateway",
                        "heartbeat",
                        {
                            "agent_id": self._agent_id,
                            "status": "active",
                            "current_task": "",
                        },
                    )
                except Exception as e:
                    logger.warning("心跳失败: %s", e)
                await asyncio.sleep(self._heartbeat_interval)
        except asyncio.CancelledError:
            logger.info("TeageAgent %s 心跳循环已取消", self._agent_id)
            raise

    async def send_message(self, content: str, to_agent: str) -> dict:
        """发送消息给目标 agent（A2A 点对点，不经工作台中转）。

        通过 A2A Client 直接调用目标 agent 的 A2A Server send_message 方法。
        发送后自动归档消息副本到工作台 collaboration.md（Forward API）。

        Args:
            content: 消息内容
            to_agent: 目标 agent 的 endpoint 名称（需在 remote_endpoints 中配置）

        Returns:
            {"ok": bool, "received_by": str}
        """
        message_id = str(uuid.uuid4())
        result = await self._transport.call_agent(
            to_agent,
            "send_message",
            {
                "from": self._agent_id,
                "to": to_agent,
                "content": content,
                "message_id": message_id,
            },
        )

        # 自动归档消息副本到工作台（失败不影响发送结果，仅记录日志）
        try:
            await self.forward_a2a_message(
                original_from=self._agent_id,
                original_to=to_agent,
                content=content,
                message_id=message_id,
            )
        except Exception as e:
            logger.warning(
                "消息归档失败（不影响发送结果）: %s", e
            )
        return result

    async def forward_a2a_message(
        self,
        original_from: str,
        original_to: str,
        content: str,
        message_id: str,
    ) -> dict:
        """归档 A2A 消息副本到工作台 collaboration.md（Forward API）。

        这是 agent 主动让工作台观察 A2A 交流过程的入口，不是通信通道。
        Agent 间通信走 A2A 点对点，forward 仅归档副本。

        Args:
            original_from: 原始发送者 agent_id
            original_to: 原始接收者 agent_id
            content: 消息内容
            message_id: 消息唯一 ID（去重用）

        Returns:
            {"ok": bool, "seq": int, "deduplicated": bool}
        """
        if not self._forward_endpoint:
            logger.warning("未配置 forward_endpoint，跳过消息归档")
            return {"ok": False, "reason": "forward_endpoint not configured"}
        return await self._transport.forward_to_workbench(
            original_from=original_from,
            original_to=original_to,
            content=content,
            message_id=message_id,
        )

    async def send_collab_response(
        self,
        content: str,
        collab_id: str,
        collab_round: int,
        to_agent: str | None = None,
        accept: bool = True,
        message_id: str | None = None,
    ) -> dict:
        """S4 G1: 发送协作轮次 response 消息到工作台协作黑板。

        与 send_message（A2A 点对点）不同,本方法写协作轮次消息(带 collab_id+
        collab_round+type=response),供 SDK agent 参与多轮协作。不调 A2A call_agent
        (协作 response 走黑板广播给参与者,非点对点)。

        Args:
            content: 消息内容
            collab_id: 所属协作 ID
            collab_round: 协作回合号
            to_agent: 目标 agent(可选,用于上下文)
            accept: 是否接受(默认 True)
            message_id: 消息 ID(不传则自动生成)

        Returns:
            工作台响应(含 ok/seq/deduplicated)
        """
        message_id = message_id or str(uuid.uuid4())
        message = {
            "from": self._agent_id,
            "to": to_agent,
            "type": "response",
            "content": content,
            "collab_id": collab_id,
            "collab_round": collab_round,
            "accept": accept,
            "message_id": message_id,
        }
        return await self._transport.post_collab_message(message)

    async def _dispatch_collab_event(self, msg: dict) -> None:
        """S5 G2/G3/G4: 识别协作事件消息并分发到对应回调。

        - type=announce, action=collab_archived → on_collab_archived(collab_id)
        - type=error (或 error=True 带 collab_round) → on_collab_error(collab_id, collab_round, content)
        回调未设置或抛异常均不阻断(catch + warn)。
        """
        mtype = msg.get("type")
        if mtype == "announce" and msg.get("action") == "collab_archived":
            if self._on_collab_archived is not None:
                cid = msg.get("collab_id") or ""
                try:
                    self._on_collab_archived(cid)
                except Exception as e:
                    logger.warning(
                        "TeageAgent %s on_collab_archived 回调异常: %s",
                        self._agent_id, e,
                    )
        elif mtype == "error" or msg.get("error") is True:
            if self._on_collab_error is not None:
                cid = msg.get("collab_id") or ""
                rnd = msg.get("collab_round")
                rnd = rnd if isinstance(rnd, int) else 0
                content = msg.get("content", "")
                try:
                    self._on_collab_error(cid, rnd, content)
                except Exception as e:
                    logger.warning(
                        "TeageAgent %s on_collab_error 回调异常: %s",
                        self._agent_id, e,
                    )

    async def query_agent(self, target_agent: str) -> dict:
        """查询目标 agent 的能力（A2A 点对点）。

        Args:
            target_agent: 目标 agent 的 endpoint 名称

        Returns:
            {"agent_id": str, "capabilities": list[str]}
        """
        return await self._transport.call_agent(
            target_agent,
            "query_capabilities",
            {},
        )

    async def get_directive_context(self) -> str:
        """获取待注入的 Director 上下文（搭便车机制）。

        1. 轮询 collaboration.md 发现新 directive 并入队
        2. drain 队列，返回格式化的 LLM 上下文片段

        Agent 在调用 LLM 前调用此方法，将返回值拼到 system prompt。
        注入后清空队列，不重复注入。这是软约束，agent 自主决定遵循程度。

        Returns:
            格式化的 directive 上下文片段，空队列返回空字符串
        """
        await self._director_injector.poll_and_enqueue_new_directives()
        return self._director_injector.drain_pending_directives()
