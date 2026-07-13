"""轻量 5 字段 cron 表达式解析器（Phase 6 Task 1）。

本模块提供 ``CronExpr`` 类，用于解析标准 5 字段 cron 表达式
（minute hour day-of-month month day-of-week），并判断给定时间是否命中、
计算下一次命中时间。供 Phase 6 任务编排系统调度周期性任务使用。

设计要点：
- 纯标准库实现，不依赖任何外部库。
- 仅支持 5 字段表达式，不支持秒级精度与年字段。
- 支持的语法：``*`` / 数字 / ``,`` 列表 / ``-`` 范围 / ``/`` 步长，
  及其组合（如 ``1-5,10``、``*/15``、``0-30/15``）。
- 不支持 ``L`` / ``W`` / ``#`` 等特殊字符（保持轻量化）。
- weekday 取值 0-6，其中 0 表示 Sunday（与 Python ``datetime.weekday()``
  的 0=Monday 不同，内部做转换）。
- day-of-month 与 day-of-week 采用标准 cron 的 OR 语义：
    - 两者均为 ``*``：匹配任意日；
    - 两者其一为 ``*``：仅按非 ``*`` 的字段匹配；
    - 两者均非 ``*``：任一字段命中即匹配（OR 关系）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Set, Tuple


class CronExpr:
    """5 字段 cron 表达式解析器。

    将表达式解析为 5 个字段的有效值集合，提供 ``matches`` 与 ``next_run``
    方法用于调度判断。构造时即完成全部解析，非法表达式抛 ``ValueError``。

    Attributes:
        _expr: 原始表达式字符串。
        _minute: minute 字段有效值集合（0-59）。
        _hour: hour 字段有效值集合（0-23）。
        _dom: day-of-month 字段有效值集合（1-31）。
        _month: month 字段有效值集合（1-12）。
        _dow: day-of-week 字段有效值集合（0-6，0=Sunday）。
        _dom_star: day-of-month 字段是否为纯 ``*``。
        _dow_star: day-of-week 字段是否为纯 ``*``。
    """

    # 各字段的 (min, max) 取值范围，顺序对应 minute/hour/dom/month/dow
    _FIELD_RANGES: Tuple[Tuple[int, int], ...] = (
        (0, 59),   # minute
        (0, 23),   # hour
        (1, 31),   # day-of-month
        (1, 12),   # month
        (0, 6),    # day-of-week (0=Sunday)
    )
    _FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

    def __init__(self, expr: str) -> None:
        """解析 5 字段 cron 表达式。

        参数:
            expr: 5 字段 cron 表达式，字段间以空白分隔，依次为
                minute / hour / day-of-month / month / day-of-week。

        Raises:
            ValueError: 表达式字段数不等于 5、存在空字段、字段值超出范围
                或字段语法非法时抛出。
        """
        if not isinstance(expr, str):
            raise ValueError(f"表达式必须为字符串: {expr!r}")
        self._expr = expr

        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(
                f"表达式必须有 5 个字段，实际 {len(fields)} 个: {expr!r}"
            )

        self._minute: Set[int] = set()
        self._hour: Set[int] = set()
        self._dom: Set[int] = set()
        self._month: Set[int] = set()
        self._dow: Set[int] = set()
        self._dom_star: bool = False
        self._dow_star: bool = False

        targets = (
            self._minute, self._hour, self._dom, self._month, self._dow,
        )
        for idx, (field, (lo, hi)) in enumerate(
            zip(fields, self._FIELD_RANGES)
        ):
            values, is_star = self._parse_field(
                field, lo, hi, self._FIELD_NAMES[idx]
            )
            targets[idx].update(values)
            if idx == 2:
                self._dom_star = is_star
            elif idx == 4:
                self._dow_star = is_star

    @staticmethod
    def _parse_field(
        field: str, lo: int, hi: int, name: str
    ) -> Tuple[Set[int], bool]:
        """解析单个 cron 字段。

        支持 ``*`` / 数字 / ``,`` 列表 / ``-`` 范围 / ``/`` 步长及其组合。
        其中 ``*/N`` 表示从字段最小值起每 N 步；``N-M/S`` 表示在 [N,M]
        范围内每 S 步；``N/S`` 表示从 N 到字段最大值每 S 步。

        参数:
            field: 单个字段字符串。
            lo: 该字段允许的最小值。
            hi: 该字段允许的最大值。
            name: 字段名（用于错误信息）。

        返回:
            (有效值集合, 是否为纯 ``*``) 二元组。

        Raises:
            ValueError: 字段为空、含空项、语法非法或值超出范围时抛出。
        """
        if not field:
            raise ValueError(f"{name} 字段为空")
        is_star = (field == "*")
        values: Set[int] = set()

        for item in field.split(","):
            if not item:
                raise ValueError(f"{name} 字段含空项: {field!r}")

            # 处理步长 / 部分
            step = 1
            range_part = item
            if "/" in item:
                range_part, step_str = item.split("/", 1)
                try:
                    step = int(step_str)
                except ValueError:
                    raise ValueError(f"{name} 字段步长非法: {field!r}")
                if step <= 0:
                    raise ValueError(
                        f"{name} 字段步长必须为正整数: {field!r}"
                    )

            # 解析 range_part 得到 [start, end]
            if range_part == "*":
                start, end = lo, hi
            elif "-" in range_part:
                parts = range_part.split("-")
                if len(parts) != 2:
                    raise ValueError(f"{name} 字段范围非法: {field!r}")
                try:
                    start = int(parts[0])
                    end = int(parts[1])
                except ValueError:
                    raise ValueError(f"{name} 字段范围非法: {field!r}")
            else:
                try:
                    v = int(range_part)
                except ValueError:
                    raise ValueError(f"{name} 字段值非法: {field!r}")
                if "/" in item:
                    # N/S 表示从 N 到字段最大值，每 S 步
                    start, end = v, hi
                else:
                    start, end = v, v

            # 范围校验
            if start < lo or end > hi:
                raise ValueError(
                    f"{name} 字段值超出范围 [{lo},{hi}]: {field!r}"
                )
            if start > end:
                raise ValueError(f"{name} 字段范围起止非法: {field!r}")

            for v in range(start, end + 1, step):
                values.add(v)

        return values, is_star

    def matches(self, dt: datetime) -> bool:
        """判断给定时间是否命中表达式。

        仅比较 minute / hour / day / month / weekday，忽略秒与微秒。
        day-of-month 与 day-of-week 按标准 cron 的 OR 语义处理
        （详见模块 docstring）。

        参数:
            dt: 待判断的时间。

        返回:
            命中返回 ``True``，否则 ``False``。
        """
        if dt.minute not in self._minute:
            return False
        if dt.hour not in self._hour:
            return False
        if dt.month not in self._month:
            return False

        # weekday 转换：Python weekday() 0=Monday → cron 0=Sunday
        cron_dow = (dt.weekday() + 1) % 7

        if self._dom_star and self._dow_star:
            # 两者均为 *：匹配任意日
            pass
        elif self._dom_star:
            # 仅 day-of-week 生效
            if cron_dow not in self._dow:
                return False
        elif self._dow_star:
            # 仅 day-of-month 生效
            if dt.day not in self._dom:
                return False
        else:
            # 两者均非 *：任一命中即匹配（OR 关系）
            if dt.day not in self._dom and cron_dow not in self._dow:
                return False
        return True

    def next_run(self, after: datetime) -> datetime:
        """返回 ``after`` 之后的下一次命中时间。

        简单实现：将 ``after`` 的秒与微秒清零，从严格大于 ``after`` 的
        下一分钟起逐分钟递增，最多检查 366*24*60 次以防死循环。返回的
        datetime 秒与微秒均为 0。

        参数:
            after: 起算时间（不含），返回值严格大于该时间。

        Returns:
            下一个命中该 cron 表达式的分钟整点时间。

        Raises:
            ValueError: 在最大迭代次数内未找到命中时间时抛出。
        """
        candidate = after.replace(second=0, microsecond=0)
        # 保证返回值严格大于 after：after 恰为整分钟时也跳到下一分钟
        if candidate <= after:
            candidate += timedelta(minutes=1)

        max_iters = 366 * 24 * 60
        for _ in range(max_iters):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)

        raise ValueError(
            f"在 {max_iters} 次迭代内未找到下一次命中时间: {self._expr!r}"
        )

    def __repr__(self) -> str:
        return self._expr

    def __str__(self) -> str:
        return self._expr
