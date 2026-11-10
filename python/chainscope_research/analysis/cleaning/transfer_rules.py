"""Transfer 分类与精确金额检查；规则版本明确，不把转账认定为实际买卖。"""

import re

from chainscope_research.core.errors import ResearchRuntimeError

ZERO = "0x" + "0" * 40
METHOD_VERSION = "transfer-activity:2026-11-10"


def parse_uint256(raw: str) -> int:
    """校验单条链上金额原值并返回整数；合计可超过 uint256，不受此单条限制。"""
    if (
        not isinstance(raw, str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,77}", raw)
        or int(raw) >= 2**256
    ):
        raise ResearchRuntimeError(
            "dependency_unavailable", "正式事件含非法金额，已停止该统计，未用零值替代。"
        )
    return int(raw)


def classify_transfer(row: dict, decimals: int | None) -> tuple[str, int]:
    """输入正式行和可信代币精度，返回互斥分类及 Python 整数金额。

    零地址到零地址单独标 unknown_zero，避免同时算作铸造与销毁；dead 地址
    仅是普通地址，不能据此认定 totalSupply 减少。零金额仍保留为真实事件。
    """
    amount = parse_uint256(row["amount_raw"])
    if (
        row["token_decimals"] is not None
        and decimals is not None
        and row["token_decimals"] != decimals
    ):
        raise ResearchRuntimeError(
            "dependency_unavailable", "正式事件与代币元信息的精度冲突。"
        )
    sender, receiver = row["from_address"].lower(), row["to_address"].lower()
    kind = (
        "unknown_zero"
        if sender == receiver == ZERO
        else "mint"
        if sender == ZERO
        else "burn"
        if receiver == ZERO
        else "self"
        if sender == receiver
        else "ordinary"
    )
    return kind, amount
