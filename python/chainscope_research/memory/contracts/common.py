"""Memory 公共合同：可信用户范围、版本引用和保留原文的文本类型。

复用研究包严格、不可变、拒绝额外字段的 Contract；本文件不提供登录或授权能力。
UUID 由服务端或数据库生成。调用方必须从已认证会话和持久记录构造可信上下文。
"""

from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StringConstraints

from chainscope_research.contracts.common import Contract, PositiveInt

NonNegativeInt = Annotated[int, Field(ge=0)]
# 不 strip，保留用户原文，方便后续核对来源引文；全空白内容仍会被拒绝。
OriginalText = Annotated[
    str, StringConstraints(min_length=1, max_length=100000, pattern=r"\S")
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
# 错误字段只接收稳定代码，避免将 SQL、URL、堆栈或供应商原始异常放入业务快照。
SafeErrorCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class MemoryUserContext(Contract):
    """服务端用户范围；代次用于识别清空记忆之前创建的迟到作业。

    解析成功不代表身份通过。此合同不作为浏览器请求或模型输出的 schema。
    """

    user_id: UUID
    memory_generation: NonNegativeInt


class MemoryVersionReference(Contract):
    """引用一条记忆的确定版本；实际归属和当前版本仍由存储层核对。"""

    memory_id: UUID
    version: PositiveInt

    @property
    def point_id(self) -> UUID:
        """从记忆 ID 和版本派生稳定 Point ID，无 IO；重试不生成新的索引身份。

        不同版本使用不同 Point，避免迟到的旧索引任务覆盖新版本 Point。
        此标识不替代检索后的 PostgreSQL 当前状态与版本检查。
        """
        return uuid5(
            NAMESPACE_URL, f"chainscope:memory:{self.memory_id}:v{self.version}"
        )
