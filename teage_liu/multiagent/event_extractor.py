"""活动事件提取器。

从协作消息中提取关键事件，供工作台左栏「活动档案时间线」展示。

事件类型：
- agent_online       agent announce action=online
- agent_offline      agent announce action=offline
- director_broadcast from=director, type=request, to=*（Director 广播）
- directive_injected from=director, type=directive（Director 注入引导）
- a2a_relay          type=relay, via=a2a（A2A 通信归档）

过滤：response / status / result 等非事件类型不提取。
（relay 已纳入事件提取，体现 A2A 协作透明度）

排序：按 ts 时间倒序（最新在前）。
分页：支持 before_ts 游标（返回 ts 严格小于 before_ts 的事件）+ limit 上限。
"""
from __future__ import annotations

from typing import Optional


def _parse_ts(ts: str) -> float:
    """解析 ISO 时间戳为 epoch 秒。失败返回 0。"""
    if not ts:
        return 0.0
    try:
        from datetime import datetime, timezone
        # 处理带时区和不带时区两种格式
        ts_str = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _summarize(msg: dict, event_type: str) -> str:
    """根据事件类型生成摘要。"""
    content = msg.get("content", "")
    if event_type == "agent_online":
        name = msg.get("agent_name") or msg.get("from", "")
        caps = msg.get("capabilities") or []
        cap_text = ", ".join(caps[:2]) if caps else "无能力声明"
        return f"{name} 上线 · {cap_text}"
    if event_type == "agent_offline":
        return f"{msg.get('from', '')} 离线"
    if event_type == "user_broadcast":
        return content[:80] + ("…" if len(content) > 80 else "")
    if event_type == "director_broadcast":
        return content[:80] + ("…" if len(content) > 80 else "")
    if event_type == "directive_injected":
        rule = msg.get("rule_type", "ordering")
        pri = msg.get("priority", "normal")
        return f"{rule} / {pri} · {content[:60]}"
    if event_type == "a2a_relay":
        from_agent = msg.get("from", "?")
        to_agent = msg.get("to", "?")
        forwarded_by = msg.get("forwarded_by", "")
        suffix = f"（归档: {forwarded_by}）" if forwarded_by and forwarded_by != from_agent else ""
        return f"{from_agent} → {to_agent}{suffix} · {content[:60]}"
    return content[:80]


def _classify(msg: dict) -> Optional[str]:
    """根据消息字段分类事件类型。返回 None 表示非事件。"""
    msg_type = msg.get("type", "")
    action = msg.get("action", "")
    sender = msg.get("from", "")

    if msg_type == "announce":
        if action == "online":
            return "agent_online"
        if action == "offline":
            return "agent_offline"
        return None

    if msg_type == "request" and sender == "director":
        return "director_broadcast"

    # 兼容历史数据：旧消息 from=user 也归类为 director_broadcast
    if msg_type == "request" and sender == "user":
        return "director_broadcast"

    if msg_type == "directive" and sender == "director":
        return "directive_injected"

    if msg_type == "relay" and msg.get("via") == "a2a":
        return "a2a_relay"

    return None


def extract_events(
    messages: list[dict],
    before_ts: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """从协作消息列表提取活动事件。

    Args:
        messages: 协作消息列表（来自 read_collab_messages）
        before_ts: 游标分页，仅返回 ts 严格小于此值的事件；None 表示不限制
        limit: 返回数量上限，默认 50

    Returns:
        事件列表，按 ts 倒序排列。每条含：
        - event_type: str  事件类型
        - ts: str          原始时间戳
        - agent_id: str    发起方标识（agent_id / user / director）
        - summary: str     一句话摘要
        - seq: int         对应消息的 seq（用于跳转定位）
    """
    cursor_ts = _parse_ts(before_ts) if before_ts else None

    events: list[dict] = []
    for msg in messages:
        event_type = _classify(msg)
        if event_type is None:
            continue

        ts = msg.get("timestamp") or msg.get("ts") or ""
        ts_epoch = _parse_ts(ts)

        # before_ts 游标：仅保留严格小于游标的事件
        if cursor_ts is not None and ts_epoch >= cursor_ts:
            continue

        events.append({
            "event_type": event_type,
            "ts": ts,
            "agent_id": msg.get("from", ""),
            "summary": _summarize(msg, event_type),
            "seq": msg.get("seq", 0),
            "collab_id": msg.get("collab_id"),
            "message_id": msg.get("message_id"),
        })

    # 按 ts 倒序（最新在前）
    events.sort(key=lambda e: _parse_ts(e["ts"]), reverse=True)

    # limit 截断
    if limit > 0:
        events = events[:limit]

    return events
