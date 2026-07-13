"""会话管理:创建/获取会话、异步标题生成。

从 Orchestrator 提取的会话相关职责:
- ensure_session: 调用 session_logger.ensure_session 原子创建会话记录
- generate_title_async: 首次对话后异步生成 5-10 字标题（fire-and-forget）
- 标题缓存: _titled_sessions 避免每次 chat() 都查 DB

cron: 前缀的会话跳过标题生成（由 CronScheduler 直接设置 schedule.name）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

# 兼容相对导入与直接运行两种方式
from hermes.llm.prompts import TITLE_GENERATION_PROMPT
from hermes.llm.reasoning_profiles import ReasoningConfig
logger = logging.getLogger(__name__)


class SessionManager:
    """管理会话生命周期:会话创建、标题生成。

    会话标题缓存策略:
    - _titled_sessions: 记录已知已有标题的 session_id，避免每次 chat() 查 DB
    - 进程内 set，服务重启后从 DB 回填
    - 仅缓存"已有标题"状态，不缓存标题内容本身（避免与 DB 不一致）
    """

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        session_logger: Optional[Any] = None,
    ) -> None:
        self._llm_client = llm_client
        self._session_logger = session_logger
        # 会话标题缓存：记录已知已有标题的 session_id
        self._titled_sessions: set = set()
        # 异步生成标题任务强引用容器：asyncio.create_task 返回的 Task 仅被
        # 事件循环持弱引用，未保存会被 GC 回收导致任务从未执行。
        # 任务完成后由 add_done_callback 自动从 set 中移除，避免内存泄漏。
        self._pending_title_tasks: set = set()

    def ensure_session(self, session_id: str) -> None:
        """确保 session 存在，不存在则创建。

        使用 session_logger.ensure_session() 的 INSERT OR IGNORE 原子操作，
        替代先 list_sessions() 全表扫描再 create_session() 的 O(n) 方式。
        """
        if self._session_logger is None:
            return
        try:
            self._session_logger.ensure_session(session_id)
        except Exception as e:
            logger.warning("创建 session 失败: %s", e)

    def generate_title_async(
        self, session_id: str, user_input: str
    ) -> None:
        """异步生成会话标题（fire-and-forget）。

        首次对话后调用 LLM 生成 5-10 字标题。cron 会话跳过（由
        CronScheduler 直接设置 schedule.name）。已生成标题的会话跳过。

        参数:
            session_id: 会话 ID。
            user_input: 用户首条输入（用于生成标题）。
        """
        if not session_id or session_id.startswith("cron:"):
            return
        if self._session_logger is None or self._llm_client is None:
            return
        # 进程内缓存命中：已知有标题，直接返回，零 IO
        if session_id in self._titled_sessions:
            return
        try:
            existing = self._session_logger.get_session_title(session_id)
            if existing:
                # 缓存回填：服务重启后首次查到已有标题，加入 set 避免后续重复查 DB
                self._titled_sessions.add(session_id)
                return
        except Exception as e:
            logger.warning("查询会话标题失败: %s", e)
            return
        try:
            coro = self._generate_title_task(session_id, user_input)
            task = asyncio.create_task(coro)
            self._pending_title_tasks.add(task)
            task.add_done_callback(self._pending_title_tasks.discard)
        except RuntimeError as e:
            # create_task 失败时关闭 coroutine 避免未 await 警告
            coro.close()  # type: ignore[possibly-undefined]
            logger.warning("创建标题生成任务失败: %s", e)

    async def _generate_title_task(
        self, session_id: str, user_input: str
    ) -> None:
        """生成标题并写入 session_logger（内部 task 实现）。

        截取 user_input 前 500 字符避免 prompt 过长；max_tokens=50 限制
        输出长度。失败时仅记录 warning，不影响主流程。
        """
        try:
            prompt = TITLE_GENERATION_PROMPT.replace(
                "{user_message}", user_input[:500]
            )
            messages = [{"role": "user", "content": prompt}]
            try:
                response = await asyncio.wait_for(
                    self._llm_client.chat_consolidation(
                        messages=messages, system=None, max_tokens=50,
                        reasoning_cfg=ReasoningConfig(enabled=False),
                    ),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.warning("会话 %s 标题生成超时（15s），跳过", session_id)
                return
            text_parts = []
            for block in response.content or []:
                if block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        text_parts.append(t)
            title = "".join(text_parts).strip()
            # 清理可能的引号、换行、首尾空白
            title = title.split("\n")[0].strip('「」""\' \t')
            if title and self._session_logger is not None:
                self._session_logger.update_session_title(session_id, title)
                # 写入成功后缓存，后续该会话的 chat() 直接跳过，零 IO
                self._titled_sessions.add(session_id)
                logger.info("已为会话 %s 生成标题: %s", session_id, title)
            else:
                logger.info("会话 %s 标题生成返回空响应，未写入", session_id)
        except Exception as e:
            logger.warning("生成会话标题失败: %s", e)
