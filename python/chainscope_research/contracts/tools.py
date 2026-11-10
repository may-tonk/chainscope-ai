"""四类工具的专用输入与数据载荷；本文件不实现查询或分析。

参数按任务分别必填。数值输出采用十进制字符串，模型不能传 SQL 或代码。
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from chainscope_research.contracts.common import (
    ChainId,
    Contract,
    EvmAddress,
    EvmHash,
    NonEmptyText,
    SignedDecimal,
    UnsignedDecimal,
)
from chainscope_research.contracts.periods import AnalysisPeriods, TimePeriod

ToolId = Literal[
    "lookup_transaction_events",
    "lookup_block_events",
    "analyze_address_activity",
    "analyze_token_activity",
]


class EventPageInput(Contract):
    """事件分页参数；cursor 由服务端签发并绑定条件，不接收任意 SQL。"""

    page_size: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: NonEmptyText | None = None


class LookupTransactionEventsInput(EventPageInput):
    """查询指定交易的已索引事件；不强制时间或代币，不承诺完整交易解析。"""

    chain_id: ChainId
    tx_hash: EvmHash
    token_address: EvmAddress | None = None


class LookupBlockEventsInput(EventPageInput):
    """按区块哈希查询已索引事件；交易哈希不得填入其他字段冒充区块语义。"""

    chain_id: ChainId
    block_hash: EvmHash
    token_address: EvmAddress | None = None


class AnalyzeTokenActivityInput(Contract):
    """代币分析必须明确链、合约及重点时间；比较和背景范围可按问题补充。"""

    chain_id: ChainId
    token_address: EvmAddress
    periods: AnalysisPeriods
    exclude_addresses: tuple[EvmAddress, ...] = ()


class AnalyzeAddressActivityInput(AnalyzeTokenActivityInput):
    """当前地址分析明确限定一个代币；不能把部分代币数据称作地址全链资产。"""

    address: EvmAddress


class EventReference(Contract):
    """定位已有原始事件；一笔交易可有多条日志，不把日志数当交易数。"""

    chain_id: ChainId
    tx_hash: EvmHash
    log_index: Annotated[int, Field(ge=0)]
    block_number: UnsignedDecimal
    # Swap 表本身不保存区块哈希，已有缓存缺失时保留未知，不编造哈希。
    block_hash: EvmHash | None = None
    event_kind: Literal["transfer", "swap"]


class TransferDetail(Contract):
    """查询返回的正式 Transfer 明细；金额是最小单位精确字符串。"""

    evidence: EventReference
    token_address: EvmAddress
    from_address: EvmAddress
    to_address: EvmAddress
    amount_raw: UnsignedDecimal
    decimals: Annotated[int, Field(ge=0, le=255)] | None


class SwapDetail(Contract):
    """已采集 Swap 的既有解析结果，不把它宣称为完整交易调用树。"""

    evidence: EventReference
    pool_address: EvmAddress
    trader: EvmAddress
    side: Literal["buy", "sell"]
    token_amount_raw: UnsignedDecimal
    quote_token: EvmAddress
    quote_amount_raw: UnsignedDecimal
    attribution: NonEmptyText


class EventLookupData(Contract):
    """返回有限事件引用；空页只表示当前索引查询结果，不证明链上交易不存在。

    next_cursor 非空表示后续仍有页；明细与引用一同限于当前页，不倾倒全量事件。
    """

    events: tuple[EventReference, ...]
    next_cursor: NonEmptyText | None = None
    transfers: tuple[TransferDetail, ...] = ()
    swaps: tuple[SwapDetail, ...] = ()


class AddressContribution(Contract):
    """地址贡献仅表示链上地址，不推断人数、身份或共同控制。"""

    address: EvmAddress
    direction: Literal[
        "sender", "receiver", "inbound_counterparty", "outbound_counterparty"
    ]
    amount_raw: UnsignedDecimal
    event_count: UnsignedDecimal


class MetricChange(Contract):
    """重点与比较时段的精确变化；不把分解贡献当成因果解释。"""

    comparison_period: TimePeriod
    metric_id: NonEmptyText
    focus_value: SignedDecimal
    comparison_value: SignedDecimal
    delta: SignedDecimal
    # 仅等长范围且基准为正时提供相对变化的精确分数，否则保留未知。
    relative_numerator: SignedDecimal | None
    relative_denominator: UnsignedDecimal | None
    note: NonEmptyText


class CountMetric(Contract):
    """计数指标使用稳定口径编号；零值是字符串 '0'，未知不填零。"""

    kind: Literal["count"]
    metric_id: NonEmptyText
    value: UnsignedDecimal


class AmountMetric(Contract):
    """金额采用最小单位精确字符串；净流量允许负数，精度未知时显式为 null。"""

    kind: Literal["token_amount"]
    metric_id: NonEmptyText
    value_raw: SignedDecimal
    decimals: Annotated[int, Field(ge=0, le=255)] | None


class RatioMetric(Contract):
    """比例以精确分数输出；分母为零时不能制造一个有效比例。"""

    kind: Literal["ratio"]
    metric_id: NonEmptyText
    numerator: UnsignedDecimal
    denominator: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]


MetricValue = Annotated[
    CountMetric | AmountMetric | RatioMetric, Field(discriminator="kind")
]


class ActivitySlice(Contract):
    """每个重点、比较或背景区间有独立指标，不把不同时间范围混为一个总数。"""

    role: Literal["focus", "comparison", "context"]
    period: TimePeriod
    metrics: Annotated[tuple[MetricValue, ...], Field(min_length=1)]
    evidence: tuple[EventReference, ...] = ()
    evidence_is_sampled: bool = False
    top_addresses: tuple[AddressContribution, ...] = ()


class TokenActivityData(Contract):
    """计算结果载荷；精确指标由 analysis/metrics 计算，模型另行解释证据。"""

    chain_id: ChainId
    token_address: EvmAddress
    slices: Annotated[tuple[ActivitySlice, ...], Field(min_length=1)]
    changes: tuple[MetricChange, ...] = ()
    observations: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def check_focus(self) -> Self:
        """返回围绕唯一重点区间的结果，避免只有背景数据却冒充回答了用户问题。"""
        if sum(item.role == "focus" for item in self.slices) != 1:
            raise ValueError("活动结果必须包含且仅包含一个 focus 区间")
        return self


class AddressActivityData(TokenActivityData):
    """地址活动沿用代币范围与分区结果，并明确研究的地址。"""

    address: EvmAddress
