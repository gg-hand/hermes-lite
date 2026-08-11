"""ScriptDirector：脚本/规则引擎的 Director 实现（Task 5，含 D6 timestamp 修复）。

基于条件自动触发（超时/死锁/冲突）。detect_anomaly 基于消息的 timestamp
字段判断超时（D6 修复），不依赖 time.sleep。

D6 修复要点：
- CollabWriter.append 自动添加 timestamp（ISO 格式 UTC）
- detect_anomaly 读取消息的 timestamp，与当前时间比较判断超时
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_messages,
)
from teage_liu.multiagent.director_protocol import DirectorProtocol


def _parse_iso(iso_str: str) -> datetime:
    """解析 ISO 时间字符串（兼容 Z 后缀）。"""
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


class ScriptDirector(DirectorProtocol):
    """脚本/规则驱动的 Director。

    基于条件自动触发（超时/死锁/冲突）。detect_anomaly 检测即时型
    request 超时（基于 timestamp），无对应 result 则报异常。
    """

    def __init__(self, bb_root: Path, timeout_seconds: int = 300):
        self._bb_root = bb_root
        self._timeout_seconds = timeout_seconds

    async def inject_directive(
        self,
        content: str,
        rule_type: str,
        target: str = "*",
        priority: str = "normal",
        deadline: int | None = None,
    ) -> None:
        """注入 directive（issued_by=ScriptDirector）。"""
        message: dict = {
            "from": "director",
            "type": "directive",
            "content": content,
            "rule_type": rule_type,
            "target": target,
            "priority": priority,
            "issued_by": "ScriptDirector",
        }
        if deadline is not None:
            message["deadline"] = deadline
        await append_collab_message(self._bb_root, message)

    async def handle_arbitration(self, request: dict) -> dict:
        """脚本处理仲裁请求（简单规则：自动响应）。"""
        return {"status": "auto_resolved", "request": request}

    async def observe(self) -> dict:
        """观察协作状态。"""
        messages = await read_collab_messages(self._bb_root)
        return {"message_count": len(messages), "messages": messages}

    async def detect_anomaly(self) -> list[dict]:
        """检测异常（超时/死锁/冲突）。

        D6 修复：基于消息的 timestamp 字段判断超时，不用 time.sleep。
        检测即时型 request（collab_type=instant）无对应 result 且超过
        timeout_seconds 的情况。
        """
        anomalies: list[dict] = []
        messages = await read_collab_messages(self._bb_root)
        now = datetime.now(timezone.utc)

        for msg in messages:
            if (
                msg.get("type") != "request"
                or msg.get("collab_type") != "instant"
            ):
                continue

            # 查找是否有对应的 result
            has_result = any(
                m.get("type") == "result"
                and m.get("reply_to") == msg.get("seq")
                for m in messages
            )
            if has_result:
                continue

            # D6: 基于 timestamp 判断超时
            timestamp = msg.get("timestamp")
            if not timestamp:
                continue

            try:
                msg_time = _parse_iso(timestamp)
                age_seconds = (now - msg_time).total_seconds()
                if age_seconds > self._timeout_seconds:
                    anomalies.append({
                        "type": "timeout",
                        "message": msg,
                        "detail": (
                            f"request seq={msg.get('seq')} 超时"
                            f"（{age_seconds:.0f}s > {self._timeout_seconds}s）"
                        ),
                    })
            except (ValueError, TypeError):
                continue

        return anomalies
