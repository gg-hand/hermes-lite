"""stdio 通道(§9/§18.3,阶段 3 落地):跨进程(异语言)扩展的唯一通道, LSP 模式 JSON 行协议。

- 每帧一行 JSON(TransportFrame,§transport.3);双向帧读写
- 握手:启动后互报 ``protocol_version``(heartbeat 帧承载,传输层版本标识;
  协商收敛逻辑 —— major 拒绝 / minor 降级 / 双版本共存 —— 归阶段 4 evolution 域,§10.2)
- 请求-响应匹配(v1.0.0 顺序约束):同一方向同一时间仅一个挂起请求,
  响应帧 payload 含 ``result``/``error`` 键(约定见 :mod:`transport`)
- 扩展→core 的请求(invoke_llm / storage_* / task_*)在读循环内转宿主处理器
  (TransportBus.handle)并回写响应
- 心跳:可插拔定时器(存活检测 + L3 丢弃计数上报);close 优雅关闭(shutdown 帧 + 终止兜底)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .transport import (
    DEFAULT_PROTOCOL_VERSION,
    MSG_EVENT,
    MSG_HEARTBEAT,
    MSG_SHUTDOWN,
    FRAME_MAX_BYTES,
    TransportFrame,
    negotiate_protocol_version,
)

logger = logging.getLogger(__name__)

#: 握手超时(扩展进程启动 + 首帧交互上限)
DEFAULT_HANDSHAKE_TIMEOUT: float = 10.0
#: 请求超时默认值
DEFAULT_REQUEST_TIMEOUT: float = 30.0
#: 心跳间隔默认值
DEFAULT_HEARTBEAT_INTERVAL: float = 30.0
#: 心跳连续失败阈值:超过则判定进程僵死,触发 on_dead(宿主自动重建)
DEFAULT_HEARTBEAT_FAILURES_BEFORE_DEAD: int = 3


class StdioError(Exception):
    """stdio 通道错误(启动失败 / 握手失败 / 请求失败 / 进程退出)。"""


# 宿主请求处理器签名: async (extension_name, frame) -> response payload
HostHandler = Callable[[str, TransportFrame], Awaitable[Dict[str, Any]]]


class StdioChannel:
    """宿主侧 stdio 子进程通道:spawn + 双向帧读写 + 握手 + 请求-响应。"""

    def __init__(
        self,
        name: str,
        command: List[str],
        host_handler: HostHandler,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        if not command or not isinstance(command, list) or not all(
            isinstance(c, str) for c in command
        ):
            raise ValueError(f"扩展 {name} 的 command 必须是字符串列表")
        self.name = name
        self._command = command
        self._host_handler = host_handler
        self.protocol_version = protocol_version
        self._handshake_timeout = handshake_timeout
        self._request_timeout = request_timeout
        self._heartbeat_interval = heartbeat_interval

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending: Optional[asyncio.Future] = None
        self._closed = False
        #: 读循环丢弃的异常帧累计计数(日志限速用,见 _read_loop)
        self._frame_drops = 0
        self._peer_protocol_version: Optional[str] = None
        self._peer_name: Optional[str] = None
        self._peer_lang: Optional[str] = None
        self._negotiation: Optional[Dict[str, Any]] = None
        self._heartbeat_failures = 0
        self._on_dead: Optional[Callable[[str, str], Awaitable[Any]]] = None

    def set_on_dead(self, callback: Callable[[str, str], Awaitable[Any]]) -> None:
        """注册进程僵死回调:心跳连续失败达阈值时调用 ``callback(name, reason)``。

        宿主(Supervisor)据此触发自动重建;回调失败仅告警,不阻断心跳循环。
        """
        self._on_dead = callback

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def peer_protocol_version(self) -> Optional[str]:
        """扩展侧协议版本(握手后可用)。"""
        return self._peer_protocol_version

    @property
    def negotiation(self) -> Optional[Dict[str, Any]]:
        """版本协商结果(握手后可用;major 拒绝会抛 StdioError 阻止启动)。"""
        return self._negotiation

    async def start(self) -> None:
        """spawn 子进程 + 握手(互报 protocol_version)。"""
        if self._proc is not None:
            raise StdioError(f"通道 {self.name} 已启动")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
            )
        except (OSError, ValueError) as e:
            raise StdioError(
                f"启动扩展进程 {self.name} 失败: {e} (command={self._command})"
            ) from e
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin
        self._read_task = asyncio.create_task(self._read_loop())
        try:
            await self._handshake()
        except Exception:
            await self.close()
            raise
        # 心跳:存活检测 + L3 丢弃计数上报
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "stdio 通道 %s 就绪: peer=%s/%s protocol_version=%s",
            self.name, self._peer_lang or "?", self._peer_name or "?",
            self._peer_protocol_version or "?",
        )

    async def _handshake(self) -> None:
        """握手:发送 heartbeat 帧(携带本端 protocol_version),等待扩展回 heartbeat,
        然后执行版本协商(§evolution V-2):major 不匹配 → 拒绝(启动失败,可读错误);
        minor 降级 → 记录并继续(downgrade 语义,§10.2)。
        """
        frame = await self.request(
            MSG_HEARTBEAT,
            {
                "handshake": True,
                "protocol_version": self.protocol_version,
                "extension_name": self.name,
                "role": "host",
            },
            timeout=self._handshake_timeout,
        )
        payload = frame.payload or {}
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise StdioError(f"扩展 {self.name} 握手响应非法: {payload!r}")
        self._peer_protocol_version = result.get("protocol_version")
        self._peer_name = result.get("name")
        self._peer_lang = result.get("lang")
        # 版本协商:major 拒绝 / minor 降级(evolution 域 V-2,§10.2)
        try:
            self._negotiation = negotiate_protocol_version(
                self.protocol_version, self._peer_protocol_version
            )
        except ValueError as e:
            raise StdioError(f"扩展 {self.name} 版本协商失败: {e}") from e
        if not self._negotiation["compatible"]:
            raise StdioError(
                f"扩展 {self.name} 协议版本不兼容(major 拒绝): "
                f"core={self.protocol_version}, ext={self._peer_protocol_version!r}"
            )
        if self._negotiation["action"] == "downgrade":
            logger.warning(
                "版本协商降级: 扩展 %s protocol_version=%s(> core %s),"
                "扩展按 core 能力子集降级运行(双版本共存过渡期,§10.2)",
                self.name, self._peer_protocol_version, self.protocol_version,
            )
        else:
            logger.info(
                "握手完成(版本协商 proceed): 扩展 %s protocol_version=%s",
                self.name, self._peer_protocol_version,
            )

    async def close(self, reason: str = "host_shutdown", shutdown_timeout: float = 1.0) -> None:
        """优雅关闭(幂等):shutdown 帧 → 终止进程 → 清理读/心跳任务。

        正常扩展收到 shutdown 帧立即响应(瞬发);僵死进程不响应 → shutdown_timeout
        (默认 1s)后终止兜底 —— 僵死重建场景下避免长等待。
        """
        if self._closed:
            return
        self._closed = True
        # 通知扩展优雅退出(尽力;失败走终止兜底)
        if self._writer is not None and self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(
                    self.request(MSG_SHUTDOWN, {"reason": reason}, timeout=shutdown_timeout),
                    timeout=shutdown_timeout,
                )
            except Exception as e:  # noqa: BLE001 - 优雅关闭为尽力而为,走终止兜底
                logger.debug("stdio 通道 %s 优雅关闭失败(走终止兜底): %s", self.name, e)
        current_task = asyncio.current_task()
        for task in (self._read_task, self._heartbeat_task):
            if task is not None and task is not current_task:
                task.cancel()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception as e:  # noqa: BLE001 - 关闭失败不影响后续终止兜底
                logger.debug("stdio 通道 %s 关闭 stdin 失败(已忽略): %s", self.name, e)
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except Exception as e:  # noqa: BLE001 - 进程可能已退出
                    logger.debug("stdio 通道 %s kill 失败(进程可能已退出): %s", self.name, e)
            except ProcessLookupError as e:  # noqa: BLE001 - 进程已退出,终止兜底无需处理
                logger.debug("stdio 通道 %s terminate 时进程已退出: %s", self.name, e)
        if self._proc is not None:
            # Windows 下 asyncio subprocess transport 需显式关闭,
            # 否则事件循环关闭后 __del__ 触发 ResourceWarning(良性噪音)
            transport = getattr(self._proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception as e:  # noqa: BLE001 - 关闭 transport 为清理动作
                    logger.debug("stdio 通道 %s 关闭 transport 失败(已忽略): %s", self.name, e)
        # 挂起请求失败送达(扩展退出)
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(StdioError(f"扩展进程 {self.name} 已关闭"))
            self._pending = None
        logger.info("stdio 通道 %s 已关闭", self.name)

    # ------------------------------------------------------------------
    # 帧读写
    # ------------------------------------------------------------------
    async def request(
        self, msg_type: str, payload: Dict[str, Any], timeout: Optional[float] = None
    ) -> TransportFrame:
        """发送请求帧并等待响应帧(同一方向同一时间仅一个挂起请求, v1.0.0 顺序约束)。"""
        if self._closed or self._writer is None or self._proc is None:
            raise StdioError(f"通道 {self.name} 已关闭,无法发送请求")
        if self._pending is not None:
            raise StdioError(
                f"通道 {self.name} 已有挂起请求(v1.0.0 顺序约束:同一方向仅一个挂起)"
            )
        frame = TransportFrame(msg_type, payload, protocol_version=self.protocol_version)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending = future
        try:
            await self._write_frame(frame)
            timeout = self._request_timeout if timeout is None else timeout
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending = None
            raise StdioError(
                f"通道 {self.name} 请求 {msg_type} 超时(>{timeout}s)"
            ) from e
        except StdioError:
            self._pending = None
            raise
        except Exception as e:
            self._pending = None
            raise StdioError(f"通道 {self.name} 请求 {msg_type} 失败: {e}") from e

    def notify(self, msg_type: str, payload: Dict[str, Any]) -> None:
        """fire-and-forget 通知(不等待响应):event / heartbeat / shutdown。"""
        if self._closed:
            return
        frame = TransportFrame(msg_type, payload, protocol_version=self.protocol_version)
        try:
            self._write_frame_nowait(frame)
        except Exception as e:
            logger.error("通道 %s 发送通知 %s 失败: %s", self.name, msg_type, e)

    async def _write_frame(self, frame: TransportFrame) -> None:
        if self._writer is None:
            raise StdioError(f"通道 {self.name} 无写端")
        line = frame.encode()
        self._writer.write(line.encode("utf-8") + b"\n")
        await self._writer.drain()

    def _write_frame_nowait(self, frame: TransportFrame) -> None:
        """同步写(通知路径,event 是异步旁路,绝不阻塞主对话流)。"""
        if self._writer is None:
            raise StdioError(f"通道 {self.name} 无写端")
        line = frame.encode()
        self._writer.write(line.encode("utf-8") + b"\n")

    # ------------------------------------------------------------------
    # 读循环(帧分发:响应 resolve / 请求转宿主)
    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        """逐行读 JSON 帧;非法帧拒绝(§15-A7)并告警,不中断通道。"""
        try:
            while True:
                if self._reader is None:
                    break
                line_bytes = await self._reader.readline()
                if not line_bytes:
                    break  # EOF:扩展进程退出
                if len(line_bytes) > FRAME_MAX_BYTES:
                    logger.error("通道 %s 收到超长帧(%d 字节),丢弃", self.name, len(line_bytes))
                    continue
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    frame = TransportFrame.decode(line, raw_bytes=len(line_bytes))
                    await self._dispatch(frame)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 单帧失败(非法 JSON / 超深 / 字段类型不可哈希 / 分发异常)只丢该帧
                    # 并继续 —— 绝不终止读循环。读循环终止 = 通道死亡(在途请求失败,
                    # 且需依赖心跳超时才重建)。
                    # 日志限速(2026-09-10 执行后审计 D-9):触发面从"仅 ValueError"扩大到
                    # 全部异常,故障/恶意 peer 可按行速率刷日志 —— 只在前 3 次与每第 100
                    # 次告警,其余降为 debug。
                    self._frame_drops += 1
                    log = logger.error if (
                        self._frame_drops <= 3 or self._frame_drops % 100 == 0
                    ) else logger.debug
                    log(
                        "通道 %s 丢弃异常帧并继续(累计 %d 次,本帧 %d 字节): %s",
                        self.name, self._frame_drops, len(line_bytes), e,
                    )
                    continue
        except asyncio.CancelledError:
            logger.debug("通道 %s 读循环被取消(正常关闭路径)", self.name)
        except Exception as e:
            logger.error("通道 %s 读循环异常: %s", self.name, e)
        finally:
            # 通道结束:通知挂起请求失败
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(
                    StdioError(f"扩展进程 {self.name} 退出(读通道 EOF)")
                )
                self._pending = None

    async def _dispatch(self, frame: TransportFrame) -> None:
        """分发入站帧:响应 → resolve 挂起;请求 → 转宿主处理器并回写响应。"""
        if frame.is_response():
            if self._pending is not None and not self._pending.done():
                self._pending.set_result(frame)
                self._pending = None
            else:
                logger.warning(
                    "通道 %s 收到无挂起请求的响应帧: type=%s, 忽略",
                    self.name, frame.type,
                )
            return
        # 扩展→core 请求:转宿主处理器(TransportBus.handle)
        try:
            response_payload = await self._host_handler(self.name, frame)
        except Exception as e:
            logger.error("通道 %s 宿主处理器异常(%s): %s", self.name, frame.type, e)
            response_payload = {"error": {"code": "internal_error", "message": str(e)}}
        response = TransportFrame(
            frame.type,
            response_payload,
            encoding=frame.encoding,
            protocol_version=frame.protocol_version,
        )
        try:
            await self._write_frame(response)
        except Exception as e:
            logger.error("通道 %s 回写响应失败(%s): %s", self.name, frame.type, e)

    # ------------------------------------------------------------------
    # 心跳(存活检测 + L3 丢弃计数上报)
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """周期心跳:发送 heartbeat 帧,连续失败达阈值 → 判定进程僵死 → on_dead 回调。

        只对"通道仍存活但心跳失败"计数(关闭中/已退出不触发,避免误判正常关闭);
        宿主(Supervisor)据此自动重建进程。
        """
        while not self._closed:
            await asyncio.sleep(self._heartbeat_interval)
            if self._closed:
                break
            try:
                await self.request(
                    MSG_HEARTBEAT,
                    {"heartbeat": True, "protocol_version": self.protocol_version},
                    timeout=min(self._heartbeat_interval, 10.0),
                )
                self._heartbeat_failures = 0
            except Exception as e:
                self._heartbeat_failures += 1
                still_alive = (
                    self._proc is not None
                    and self._proc.returncode is None
                    and not self._closed
                )
                if not still_alive:
                    logger.warning("扩展 %s 心跳失败(通道关闭中,不计僵死): %s", self.name, e)
                    continue
                if self._heartbeat_failures >= DEFAULT_HEARTBEAT_FAILURES_BEFORE_DEAD:
                    logger.error(
                        "扩展 %s 心跳连续失败 %d 次,判定进程僵死,触发自动重建",
                        self.name, self._heartbeat_failures,
                    )
                    if self._on_dead is not None:
                        try:
                            await self._on_dead(
                                self.name, f"heartbeat_failed_{self._heartbeat_failures}"
                            )
                        except Exception as cb_e:
                            logger.error("扩展 %s on_dead 回调失败: %s", self.name, cb_e)
                    self._heartbeat_failures = 0  # 重建后重新计数
                else:
                    logger.error(
                        "扩展 %s 心跳失败(%d/%d): %s",
                        self.name, self._heartbeat_failures,
                        DEFAULT_HEARTBEAT_FAILURES_BEFORE_DEAD, e,
                    )
