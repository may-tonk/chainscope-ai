"""区分代币、地址、交易和区块；同形状的哈希必须由语义确定种类。

这些是 EVM 对象合同；格式合规不代表对象存在，也不代表用户有查询权限。
"""

from typing import Annotated, Literal

from pydantic import Field

from chainscope_research.contracts.common import ChainId, Contract, EvmAddress, EvmHash


class TokenTarget(Contract):
    """代币范围任务需要链和代币合约；代币符号不能替代合约定位。"""

    kind: Literal["token"]
    chain_id: ChainId
    address: EvmAddress


class AddressTarget(Contract):
    """地址对象只表示研究对象，不隐含全链或全部代币数据已经可用。"""

    kind: Literal["address"]
    chain_id: ChainId
    address: EvmAddress


class TransactionTarget(Contract):
    """交易对象无需另填时间；前后行为研究由对应工具再要求时间范围。"""

    kind: Literal["transaction"]
    chain_id: ChainId
    tx_hash: EvmHash


class BlockTarget(Contract):
    """区块哈希与交易哈希字段分开，不能根据长度自动混用。"""

    kind: Literal["block"]
    chain_id: ChainId
    block_hash: EvmHash


ResearchTarget = Annotated[
    TokenTarget | AddressTarget | TransactionTarget | BlockTarget,
    Field(discriminator="kind"),
]
