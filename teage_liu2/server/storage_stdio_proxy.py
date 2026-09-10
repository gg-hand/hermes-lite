"""stdio 存储代理:P-4 storage-stdio 线协议的宿主侧参考实现(设计 §5)。

定位:与具体后端无关的通用协议件——方法调用机械映射为 JSON 帧
(op = 方法名),不含任何后端知识;换/加后端本文件零改动。

并发模型(同 StorageWriter 线程思路):内嵌独立 IO 线程 + 专用事件循环,
asyncio 管理子进程与读写帧;同步方法经 run_coroutine_threadsafe 投递 +
超时等待(StorageWriter 写线程 / asyncio.to_thread 读路径直接兼容)。

错误处理(降级优先):请求超时/断连抛异常;写路径由 StorageWriter 现有
兜底吞掉记日志,主对话不中断;读路径异常向上冒泡由调用方降级。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from ..core.history import HistoryStore
from ..core.storage import MessageStore, StorageProvider, _validate_kind

logger = logging.getLogger(__name__)

PROTOCOL_NAME = "storage-stdio"
PROTOCOL_VERSION = "0.1.0"

#: 握手超时(秒)——进程 spawn + 首帧往返
_HANDSHAKE_TIMEOUT = 15.0
#: close 时 bye 后等待子进程自行退出的超时(秒)
_SHUTDOWN_TIMEOUT = 5.0


class _BackendConnection:
    """子进程连接:专用事件循环线程内管理 spawn/读帧/写帧/关闭。"""

    def __init__(self, command: List[str]) -> None:
        self._command = list(command)
        # Windows 3.11 默认 ProactorEventLoop 策略:任意线程 new_event_loop()
        # 均为 Proactor,create_subprocess_exec 在子线程可用
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stdio-storage-proxy"
        )
        #: 握手完成/失败信号(set 在 loop 线程,threading.Event 线程安全)
        self._handshake_done = threading.Event()
        self._handshake_error: Optional[BaseException] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._closed = False

    # -- 线程外接口(同步) ------------------------------------------------
    def start(self) -> None:
        """spawn 子进程并完成握手;失败抛异常(启动失败)。"""
        self._thread.start()
        if not self._handshake_done.wait(timeout=_HANDSHAKE_TIMEOUT + 5.0):
            self.abort()
            raise RuntimeError(
                f"存储后端握手超时({_HANDSHAKE_TIMEOUT}s): {self._command!r}"
            )
        if self._handshake_error is not None:
            self.abort()
            raise self._handshake_error

    def request(self, op: str, payload: dict,
                timeout: Optional[float] = None) -> Any:
        """同步请求-响应:投递到内部 loop 并等待结果(线程安全)。

        连接已断开(loop 停止)立即抛 RuntimeError——不悬挂到超时、不掩盖死因;
        超时抛 TimeoutError;关闭后调用抛 RuntimeError。
        """
        if self._closed:
            raise RuntimeError("存储后端连接已关闭")
        if not self._thread.is_alive() or not self.loop.is_running():
            raise RuntimeError("存储后端连接已断开(loop 已停止)")
        fut = asyncio.run_coroutine_threadsafe(
            self._request(op, payload), self.loop
        )
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError(f"存储后端 {op!r} 请求超时") from None

    def abort(self) -> None:
        """无条件的立即终止(启动失败/异常路径用,幂等)。"""
        self._closed = True
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stop)

    def close(self) -> None:
        """优雅关闭:bye → 等 IO 线程结束 → 终止兜底(幂等)。

        确定性时序:同步投递 bye 帧写入(不经协程等待)→ join IO 线程
        (后端收到 bye 退出 → stdout EOF → _serve 返回)→ 兜底杀进程。
        避免协程与 loop 停止的竞态(EOF 会让 run_until_complete 返回,
        挂在 loop 上的关闭协程将永不完成)。
        """
        if self._closed:
            return
        self._closed = True
        if self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self._send_bye)
            except RuntimeError:
                pass  # loop 正在关闭
        if self._thread.is_alive():
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT + 5.0)
        self._stop_sync()
        if self._thread.is_alive():
            logger.warning("存储后端 IO 线程未在期限内退出,跳过 loop.close")
        else:
            self.loop.close()

    def _send_bye(self) -> None:
        """loop 线程内:同步写 bye 帧(小帧,无需 drain 等待)。"""
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        try:
            self._proc.stdin.write(
                (json.dumps({"id": 0, "op": "bye", "p": {}}) + "\n").encode("utf-8")
            )
        except Exception as e:
            logger.warning("发送 bye 失败: %s", e)

    # -- loop 线程内 ------------------------------------------------------
    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001 —— 线程边界,必须捕获
            if not self._handshake_done.is_set():
                self._handshake_error = e
            else:
                logger.error("存储后端连接异常终止: %s", e)
            self._fail_pending(RuntimeError(f"存储后端连接已断开: {e}"))
        finally:
            self._handshake_done.set()
            self._fail_pending(RuntimeError("存储后端连接已关闭"))

    async def _serve(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pump = asyncio.ensure_future(self._pump_stderr())
        reader = asyncio.ensure_future(self._read_loop())
        try:
            # 握手:hello 与"进程退出(EOF)"竞速——后端秒退时立即失败,不等满超时;
            # _request 返回已解包的 r(ok:false 时经 future 直接抛后端错误)
            hello = asyncio.ensure_future(self._request("hello", {
                "protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION,
            }))
            done, _pending = await asyncio.wait(
                {hello, reader},
                timeout=_HANDSHAKE_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if hello not in done:
                if reader in done:
                    raise RuntimeError(
                        "存储后端进程在握手完成前退出(检查后端启动错误,见日志)"
                    )
                raise RuntimeError(
                    f"存储后端握手超时({_HANDSHAKE_TIMEOUT}s): {self._command!r}"
                )
            resp = hello.result()
            ver = str(resp.get("version", "")) if isinstance(resp, dict) else ""
            if not ver:
                raise RuntimeError(f"存储后端握手响应缺少 version: {resp!r}")
            if ver.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
                raise RuntimeError(
                    f"存储后端协议主版本不兼容: 后端 {ver!r} vs 宿主 {PROTOCOL_VERSION!r}"
                )
            logger.info("存储后端握手完成(protocol=%s, backend=%s)", PROTOCOL_NAME, ver)
            # 握手成功即时放行 start()(threading.Event 线程安全);
            # 失败路径仍由 _run 的 finally 兜底置位(携带 _handshake_error)
            self._handshake_done.set()
            await reader  # 常驻:进程退出/stdout EOF 才返回
        finally:
            # 握手失败/异常路径:取消常驻任务 + 杀进程等退出,防 pending 告警
            for t in (reader, pump, hello):
                if t.done():
                    try:
                        t.exception()  # 显式取走,防 "never retrieved" 告警
                    except asyncio.CancelledError:
                        pass
                else:
                    t.cancel()
            proc = self._proc
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass

    async def _request(self, op: str, payload: dict) -> Any:
        """发送一帧并等待响应(loop 线程内协程)。"""
        assert self._proc is not None and self._proc.stdin is not None
        req_id = next(self._ids)
        frame = json.dumps({"id": req_id, "op": op, "p": payload},
                           ensure_ascii=False) + "\n"
        fut: asyncio.Future = self.loop.create_future()
        self._pending[req_id] = fut
        try:
            self._proc.stdin.write(frame.encode("utf-8"))
            await self._proc.stdin.drain()
        except BaseException:
            self._pending.pop(req_id, None)
            raise
        return await fut

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break  # EOF:后端退出
            try:
                frame = json.loads(line.decode("utf-8"))
            except UnicodeDecodeError as e:
                # P-4 帧编码条款:后端 stdio 必须输出 UTF-8;违反 = 连接终止(快速失败)
                raise RuntimeError(
                    "存储后端返回非 UTF-8 数据(违反 P-4 帧编码条款,检查后端"
                    " stdout/stderr 编码配置)"
                ) from e
            except json.JSONDecodeError:
                logger.warning("存储后端返回非 JSON 行,忽略: %r", line[:200])
                continue
            fut = self._pending.pop(frame.get("id"), None)
            if fut is None or fut.done():
                continue
            if frame.get("ok"):
                fut.set_result(frame.get("r"))
            else:
                fut.set_exception(RuntimeError(str(frame.get("e"))))
        logger.warning("存储后端 stdout 已关闭(进程退出)")

    async def _pump_stderr(self) -> None:
        """后端 stderr 透传到宿主日志(排查用)。"""
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.info("[storage-backend] %s",
                        line.decode("utf-8", "replace").rstrip())

    def _stop(self) -> None:
        self.loop.create_task(self._kill())

    async def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()

    def _stop_sync(self) -> None:
        """极端路径:直接同步杀进程(幂等)。"""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _fail_pending(self, error: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()


class StdioStorageProxy(StorageProvider, HistoryStore, MessageStore):
    """通用 stdio 存储代理:方法调用机械映射为 P-4 帧,无后端知识。"""

    def __init__(self, command: List[str], request_timeout: float = 10.0) -> None:
        if not command or not all(isinstance(c, str) and c for c in command):
            raise ValueError(f"command 必须是非空字符串数组,实际 {command!r}")
        if request_timeout <= 0:
            raise ValueError(f"request_timeout 必须为正数,实际 {request_timeout!r}")
        self._request_timeout = request_timeout
        self._conn = _BackendConnection(command)
        self._close_lock = threading.Lock()

    def start(self) -> None:
        """spawn + 握手(装配点调用;失败抛 = 启动失败)。"""
        self._conn.start()

    # -- 内部:同步调用 → 内部 loop --------------------------------------
    def _call(self, op: str, payload: dict) -> Any:
        return self._conn.request(op, payload, timeout=self._request_timeout)

    # -- StorageProvider ---------------------------------------------------
    def write(self, kind: str, doc):
        _validate_kind(kind)
        if isinstance(doc, list):
            if not doc:
                raise ValueError("批量写入 docs 不能为空列表")
            return self._call("write", {"kind": kind, "docs": doc})
        return self._call("write", {"kind": kind, "docs": [doc]})[0]

    def read(self, kind: str, doc_id: str):
        _validate_kind(kind)
        return self._call("read", {"kind": kind, "doc_id": doc_id})

    def query(self, kind: str, limit: Optional[int] = None, **filters):
        _validate_kind(kind)
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError(f"limit 必须是正整数,实际 {limit!r}")
        return self._call("query", {
            "kind": kind, "limit": limit, "filters": filters,
        })

    def delete(self, kind: str, doc_id: str) -> None:
        _validate_kind(kind)
        self._call("delete", {"kind": kind, "doc_id": doc_id})

    # -- HistoryStore / MessageStore ---------------------------------------
    def ensure_session(self, session_id: str) -> None:
        self._call("ensure_session", {"session_id": session_id})

    def log_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        token_count: int = 0,
        is_error: bool = False,
        reasoning: Optional[str] = None,
        content_blocks: Optional[List[dict]] = None,
        message_type: Optional[str] = None,
    ) -> None:
        self._call("log_message", {
            "session_id": session_id, "role": role, "content": content,
            "tool_name": tool_name, "tool_call_id": tool_call_id,
            "token_count": token_count, "is_error": is_error,
            "reasoning": reasoning, "content_blocks": content_blocks,
            "message_type": message_type,
        })

    def get_session_messages(
        self, session_id: str, limit: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return self._call("get_session_messages", {
            "session_id": session_id, "limit": limit, "before_id": before_id,
        })

    def update_session_title(self, session_id: str, title: str) -> None:
        self._call("update_session_title", {"session_id": session_id, "title": title})

    def get_session_title(self, session_id: str) -> Optional[str]:
        return self._call("get_session_title", {"session_id": session_id})

    def search_messages(
        self, keyword: str, session_id: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        return self._call("search_messages", {
            "keyword": keyword, "session_id": session_id, "limit": limit,
        })

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        """优雅关闭(幂等;registry.shutdown 会对同一实例调两次)。"""
        with self._close_lock:
            if self._conn._closed:
                return
            self._conn.close()
