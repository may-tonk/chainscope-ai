"""研究合同的公共类型：拒绝未知字段，保留精确数值，统一生成 JSON Schema。

这里只检查数据格式，不进行用户认证、数据库访问或授权判断。
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Contract(BaseModel):
    """所有边界合同的基类；严格检查类型，防止拼错字段被静默忽略。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ChainId = Annotated[int, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
EvmAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
EvmHash = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]
# 事件计数和累计金额都可能超出浏览器安全整数范围；不能先转 float 再导出。
# 累计金额可大于单笔 uint256，因此这里不施加单笔金额的 256 位上限。
UnsignedDecimal = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$")]
SignedDecimal = Annotated[str, StringConstraints(pattern=r"^(0|-?[1-9][0-9]*)$")]


class CallIdentity(Contract):
    """所有调用状态都保留关联编号；失败未生成数据时也能定位任务与尝试。

    这些编号由程序生成并比对持久任务记录，不从模型工具参数中取得。
    """

    research_id: NonEmptyText
    request_revision: PositiveInt
    task_id: NonEmptyText
    attempt_id: NonEmptyText
    call_id: NonEmptyText
