"""时间选择与执行范围合同；相对时间锚定现有采集截止，不固定减三小时。

解析函数只做本地时间运算，不读取数据库，不探测链头，不增加采集验收。
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from chainscope_research.contracts.common import Contract, NonEmptyText, PositiveInt


class TimePeriod(Contract):
    """执行用的明确区间，统一为带时区的左闭右开 [start, end)。"""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def check_order(self) -> Self:
        """输入完整区间，返回合法合同；拒绝空区间和反向区间。"""
        if self.start >= self.end:
            raise ValueError("时间范围必须满足 start < end")
        return self


class RelativeTimeSelection(Contract):
    """用户选最近多少小时或天；这里不接收模型填写的截止时间。"""

    kind: Literal["relative"]
    amount: PositiveInt
    unit: Literal["hour", "day"]


class AbsoluteTimeSelection(Contract):
    """用户明确的历史区间，解析时保留其边界，不再次减去采集延迟。"""

    kind: Literal["absolute"]
    period: TimePeriod


TimeSelection = Annotated[
    RelativeTimeSelection | AbsoluteTimeSelection, Field(discriminator="kind")
]


class ContextPeriod(Contract):
    """分析 Agent 选择的比较或背景区间必须记录理由，便于总结解释。"""

    period: TimePeriod
    reason: NonEmptyText


class AnalysisPeriods(Contract):
    """重点范围必需；比较与背景按问题选择，不默认扫描全部历史。"""

    focus_period: TimePeriod
    comparison_periods: tuple[ContextPeriod, ...] = ()
    context_periods: tuple[ContextPeriod, ...] = ()


def resolve_time_selection(
    selection: TimeSelection, *, data_cutoff_time: datetime
) -> TimePeriod:
    """输入用户时间选择和服务端现有截止，返回固定区间；超范围交回主 Agent。

    调用方负责从已有采集记录取得 cutoff，本函数不信任模型提供的截止值。
    不静默裁剪历史区间，也不在重试时重新按当前时钟漂移。
    """
    if data_cutoff_time.utcoffset() is None:
        raise ValueError("数据截止时间必须带时区")
    if isinstance(selection, AbsoluteTimeSelection):
        if selection.period.end > data_cutoff_time:
            raise ValueError("所选结束时间超出现有采集截止，需要调整时间范围")
        return selection.period
    try:
        duration = timedelta(
            **{"hours" if selection.unit == "hour" else "days": selection.amount}
        )
        return TimePeriod(start=data_cutoff_time - duration, end=data_cutoff_time)
    except OverflowError as error:
        raise ValueError("相对时间超出可表示范围") from error
