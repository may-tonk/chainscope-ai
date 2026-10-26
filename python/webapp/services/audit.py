"""认证审计：记录“什么时候、哪个账户、发生了什么”，写入 auth_events 表。

绝不写入：密码、验证码、令牌、Cookie、邮箱原文。
来源（IP）只保存脱敏后的形式，例如 203.0.113.x，够用来排查问题，又不保存完整地址。

record() 不自己开事务，而是用调用方传入的连接：审计记录和它描述的操作在同一个事务里，
操作回滚了，审计也一起回滚，不会出现“记录说登录成功了，其实会话没建成”。
"""

import ipaddress

from ..db.tables import events


def mask_source(source: str | None) -> str | None:
    """把来源 IP 脱敏：IPv4 去掉最后一段，IPv6 只保留前 48 位；不是 IP 的只记 "other"。"""
    if not source:
        return None
    try:
        address = ipaddress.ip_address(source)
    except ValueError:
        return "other"
    if address.version == 4:
        return ".".join(str(address).split(".")[:3]) + ".x"
    network = ipaddress.ip_network(f"{address}/48", strict=False)
    return f"{network.network_address}/48"


def record(
    connection,
    event_type: str,
    *,
    user_id=None,
    request_id: str | None = None,
    source: str | None = None,
    reason: str | None = None,
) -> None:
    """写一条审计事件。event_type 例如 login_succeeded；reason 是内部原因码，不对外显示。"""
    connection.execute(
        events.insert().values(
            event_type=event_type,
            user_id=user_id,
            request_id=request_id,
            source=mask_source(source),
            reason=reason,
        )
    )
