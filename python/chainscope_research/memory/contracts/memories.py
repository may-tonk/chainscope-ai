"""候选事实、记忆版本和 ADD/UPDATE/NOOP 裁决合同，不执行提取或写入。

模型输出不含用户身份。地址绑定具体链，避免多链地址数组产生错误交叉组合。
来源真实性、资源归属和最新版本需要服务端结合实际消息及存储记录再次检查。
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from chainscope_research.contracts.common import Contract, EvmAddress, PositiveInt
from chainscope_research.memory.contracts.common import (
    MemoryVersionReference,
    NonNegativeInt,
    OriginalText,
    ShortText,
)

ChainName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
CandidateId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
MemoryCategory = Literal["preference", "focus", "research_background"]
MemoryStatus = Literal["active", "superseded", "deleted_by_user"]


class EvmEntity(Contract):
    """将 EVM 地址与确定链绑定；格式合法不表示该链或合约身份已经核实。"""

    chain: ChainName
    address: EvmAddress

    @field_validator("address")
    @classmethod
    def normalize_address(cls, value: str) -> str:
        """输入已通过 EVM 格式校验的地址，返回检索用小写形式，无外部查询。"""
        return value.lower()


class MemoryMetadata(Contract):
    """结构化检索元数据；addresses 和联合键从绑定实体派生，避免冗余不一致。"""

    chains: tuple[ChainName, ...] = ()
    entities: tuple[EvmEntity, ...] = ()
    entity_tags: tuple[ShortText, ...] = ()

    @model_validator(mode="after")
    def check_entities(self) -> Self:
        """拒绝重复项及未声明链的实体；返回已规范化元数据，不猜测未知链。"""
        keys = [(entity.chain, entity.address) for entity in self.entities]
        if len(set(self.chains)) != len(self.chains) or len(set(keys)) != len(keys):
            raise ValueError("链或链地址实体不能重复")
        if len(set(self.entity_tags)) != len(self.entity_tags):
            raise ValueError("实体标签不能重复")
        if any(entity.chain not in self.chains for entity in self.entities):
            raise ValueError("地址实体的链必须包含在 chains 中")
        return self

    @property
    def addresses(self) -> tuple[str, ...]:
        """返回去重地址列表，供后续索引 Payload 使用；不混淆地址所属链。"""
        return tuple(dict.fromkeys(entity.address for entity in self.entities))

    @property
    def entity_keys(self) -> tuple[str, ...]:
        """返回链与地址联合键，支持精确召回，无数据库或向量库副作用。"""
        return tuple(f"{entity.chain}:{entity.address}" for entity in self.entities)


class MemoryContent(Contract):
    """一条原子记忆的正文、类别和实体信息；不携带执行身份或权限。"""

    text: ShortText
    category: MemoryCategory
    metadata: MemoryMetadata = Field(default_factory=MemoryMetadata)


class SourceEvidence(Contract):
    """模型引用的用户消息与原文；是否真实、是否属于最新轮由程序核对。"""

    message_id: UUID
    quote: OriginalText


class CandidateFact(Contract):
    """提取阶段候选；临时或不确定内容可表示出来，但不能直接固化。"""

    candidate_id: CandidateId
    content: MemoryContent
    scope: Literal["stable", "temporary", "uncertain"]
    evidence: Annotated[tuple[SourceEvidence, ...], Field(min_length=1)]
    # 这是模型分类提示，不构成解除删除抑制的授权，仍须核对当前用户原话。
    explicit_remember: bool = False


class CandidateBatch(Contract):
    """一次提取结果，允许没有可记忆事实；候选编号只能在本批次解释。"""

    facts: tuple[CandidateFact, ...]

    @model_validator(mode="after")
    def check_identifiers(self) -> Self:
        """返回候选批次，拒绝重复编号，避免裁决结果无法对应原事实。"""
        ids = [fact.candidate_id for fact in self.facts]
        if len(set(ids)) != len(ids):
            raise ValueError("候选事实编号不能重复")
        return self


class AddDecision(Contract):
    """新事实裁决；记忆 ID 由服务端生成，模型不能指定待写用户或全局 ID。"""

    action: Literal["ADD"]
    candidate_id: CandidateId
    content: MemoryContent
    reason: ShortText


class UpdateDecision(Contract):
    """更新裁决必须绑定旧版本；是否属于可更新目标由程序和事务再次检查。"""

    action: Literal["UPDATE"]
    candidate_id: CandidateId
    target: MemoryVersionReference
    content: MemoryContent
    reason: ShortText


class NoopDecision(Contract):
    """跳过裁决只说明原因，不携带新正文或可写目标。"""

    action: Literal["NOOP"]
    candidate_id: CandidateId
    reason: ShortText


MemoryDecision = Annotated[
    AddDecision | UpdateDecision | NoopDecision, Field(discriminator="action")
]


class DecisionBatch(Contract):
    """本批裁决，每个候选最多一个动作，同一旧记忆不能被同时更新两次。"""

    decisions: tuple[MemoryDecision, ...]

    @model_validator(mode="after")
    def check_targets(self) -> Self:
        """拒绝重复候选裁决或重复更新目标，防止批内相互覆盖。"""
        candidates = [decision.candidate_id for decision in self.decisions]
        targets = [
            decision.target.memory_id
            for decision in self.decisions
            if isinstance(decision, UpdateDecision)
        ]
        if len(set(candidates)) != len(candidates) or len(set(targets)) != len(targets):
            raise ValueError("同批不能重复裁决候选或重复更新一条记忆")
        return self


class MemoryVersionRecord(MemoryVersionReference):
    """服务端版本快照；正文保留历史，用户和代次不从模型输出继承。"""

    user_id: UUID
    memory_generation: NonNegativeInt
    status: MemoryStatus
    content: MemoryContent
    origin: Literal["automatic", "manual"]
    source_message_ids: tuple[UUID, ...] = ()
    supersedes_version: PositiveInt | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def check_history(self) -> Self:
        """检查版本连续性和自动记忆来源；真实来源归属由服务端验证。"""
        expected = None if self.version == 1 else self.version - 1
        if self.supersedes_version != expected:
            raise ValueError("记忆版本必须指向紧邻的上一版本，首版不填替代版本")
        if self.origin == "automatic" and not self.source_message_ids:
            raise ValueError("自动记忆必须有来源消息")
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("来源消息不能重复")
        return self
