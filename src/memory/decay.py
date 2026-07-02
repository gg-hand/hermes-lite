"""记忆衰减与强化计算模块（Phase 7 Task 1）。

提供 ``MemoryDecay`` 类，基于三因子加权计算「衰减后的 importance」：
importance × recency_factor × frequency_factor。

因子说明：
- ``recency_factor = exp(-days_since_last_access × decay_rate)``：
  时间衰减，越久没访问权重越低（exp 衰减）。
- ``frequency_factor = log(1 + access_count) × frequency_weight + 1``：
  访问频率强化，被频繁检索的记忆权重越高（log 平滑，避免高频记忆
  永不被衰减）。

向后兼容：旧记忆无 ``last_accessed`` / ``access_count`` 字段时，
分别回退到当前时间（即不衰减）与 0（frequency_factor = 1）。
参数解析异常时回退到静态 importance，保证排序稳定。

纯计算无副作用，不依赖外部状态，便于单元测试。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MemoryDecay:
    """三因子衰减计算器。

    通过 ``decayed_importance`` 方法将静态 importance 与 recency / frequency
    因子组合，输出衰减后的排序权重。供 ``MemoryRetriever._sort_key`` 使用，
    替代原静态 importance 排序。

    属性:
        decay_rate: recency 衰减率（每天），默认 0.01。可热更新即时生效。
        frequency_weight: frequency 权重系数，默认 0.5。可热更新即时生效。
    """

    def __init__(
        self, decay_rate: float = 0.01, frequency_weight: float = 0.5
    ) -> None:
        """初始化衰减计算器。

        参数:
            decay_rate: recency 衰减率（每天），默认 0.01。
                ``recency_factor = exp(-days × decay_rate)``。
            frequency_weight: frequency 权重系数，默认 0.5。
                ``frequency_factor = log(1 + access_count) × frequency_weight + 1``。
        """
        self.decay_rate = decay_rate
        self.frequency_weight = frequency_weight

    def decayed_importance(
        self,
        importance: float,
        last_accessed: Any,
        access_count: Any,
    ) -> float:
        """计算三因子加权后的 importance。

        公式::

            days = (now - last_accessed).days
            recency_factor = exp(-days × decay_rate)
            frequency_factor = log(1 + access_count) × frequency_weight + 1
            return importance × recency_factor × frequency_factor

        向后兼容与容错：
        - ``last_accessed`` 为 None / 空字符串 / 解析失败时，回退到当前时间
          （即 days=0，recency_factor=1，不衰减）。
        - ``access_count`` 为 None / 解析失败时，回退到 0
          （frequency_factor = log(1) × w + 1 = 1）。
        - ``importance`` 解析失败时，回退到 0.5。
        - 任何异常都回退到静态 importance，保证排序稳定。

        参数:
            importance: 静态重要性权重（0-1）。
            last_accessed: 最后访问时间戳（ISO 格式字符串或 datetime）。
            access_count: 访问次数（int 或可转 int 的值）。

        返回:
            衰减后的 importance（float）。异常时回退到静态 importance。
        """
        try:
            # 解析 importance
            try:
                imp_val = float(importance)
            except (TypeError, ValueError):
                imp_val = 0.5

            # 解析 access_count，回退到 0
            try:
                count_val = int(access_count)
                if count_val < 0:
                    count_val = 0
            except (TypeError, ValueError):
                count_val = 0

            # 解析 last_accessed，回退到当前时间（不衰减）
            now = datetime.now()
            last_dt = self._parse_datetime(last_accessed)
            if last_dt is None:
                # 解析失败，回退到当前时间（days=0，不衰减）
                last_dt = now

            # 计算时间差（天数，非负）
            delta = now - last_dt
            days = max(delta.days, 0)

            # 三因子计算
            recency_factor = math.exp(-days * self.decay_rate)
            frequency_factor = math.log(1 + count_val) * self.frequency_weight + 1.0
            result = imp_val * recency_factor * frequency_factor
            return float(result)
        except Exception as e:
            # 任何异常都回退到静态 importance，保证排序稳定
            logger.debug(
                "decayed_importance 计算异常，回退到静态 importance: %s", e
            )
            try:
                return float(importance)
            except (TypeError, ValueError):
                return 0.5

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        """解析时间戳为 datetime 对象。

        支持的类型：
        - ``datetime`` 对象：原样返回。
        - ISO 格式字符串：通过 ``datetime.fromisoformat`` 解析。
        - 空值 / 解析失败：返回 None。

        参数:
            value: 待解析的时间值。

        返回:
            datetime 对象，解析失败时返回 None。
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return None
        return None


if __name__ == "__main__":
    # 简单验证逻辑
    print("=== MemoryDecay 验证 ===\n")

    decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)

    # 1. 刚写入的记忆（last_accessed = now），不衰减
    now_iso = datetime.now().isoformat()
    fresh = decay.decayed_importance(0.8, now_iso, 0)
    print(f"[Fresh] importance=0.8, last_accessed=now, count=0 → {fresh:.4f}")
    assert abs(fresh - 0.8) < 0.01, "刚写入的记忆应接近原 importance"

    # 2. 100 天前的记忆，recency 衰减
    old_dt = datetime.now().replace(year=datetime.now().year - 1)
    old_iso = old_dt.isoformat()
    old = decay.decayed_importance(0.8, old_iso, 0)
    print(f"[Old] importance=0.8, last_accessed=1年前, count=0 → {old:.4f}")
    assert old < 0.8, "老记忆应被衰减"

    # 3. 高频记忆强化
    frequent = decay.decayed_importance(0.8, now_iso, 20)
    print(f"[Frequent] importance=0.8, last_accessed=now, count=20 → {frequent:.4f}")
    assert frequent > 0.8, "高频记忆应被强化"

    # 4. 缺失字段回退
    missing_la = decay.decayed_importance(0.8, None, 0)
    print(f"[Missing LA] → {missing_la:.4f}")
    assert abs(missing_la - 0.8) < 0.01, "last_accessed 缺失应回退到不衰减"

    missing_ac = decay.decayed_importance(0.8, now_iso, None)
    print(f"[Missing AC] → {missing_ac:.4f}")
    assert abs(missing_ac - 0.8) < 0.01, "access_count 缺失应回退到 0"

    print("\n=== 所有验证通过 ===")
