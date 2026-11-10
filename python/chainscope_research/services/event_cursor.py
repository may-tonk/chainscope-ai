"""无服务端游标缓存的 HMAC 分页令牌；绑定用户、研究修订、条件和既有快照。

签名密钥由调用方显式注入，不写入 YAML，不使用模型提供的密钥或身份。
"""

import base64
import hashlib
import hmac
import json
import time

from pydantic import SecretStr

from chainscope_research.core.errors import ResearchRuntimeError


class EventCursor:
    """固定密钥签发和验证；共享部署时调用方必须注入同一受保护密钥。"""

    def __init__(self, key: SecretStr):
        """要求至少 32 字节密钥，不创建临时默认密钥冒充可跨进程续页。"""
        raw = key.get_secret_value().encode("utf-8")
        if len(raw) < 32:
            raise ValueError("分页密钥至少需要 32 字节")
        self._key = raw

    def issue(self, binding: str, stamp: str, last: tuple, expires: int) -> str:
        """签发条件绑定的稳定复合位置，令牌不包含凭证或 SQL。"""
        body = json.dumps(
            {"binding": binding, "stamp": stamp, "last": last, "expires": expires},
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(body).rstrip(b"=")
        signature = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        return encoded.decode() + "." + signature

    def parse(self, token: str, binding: str, stamp: str) -> tuple:
        """拒绝篡改、过期、跨用户/条件或快照变化；不能悄悄从头返回当作续页。"""
        try:
            if len(token) > 4096:
                raise ValueError("oversize")
            body, signature = token.split(".")
            expected = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if (
                data["binding"] != binding
                or data["stamp"] != stamp
                or data["expires"] <= time.time()
            ):
                raise ValueError("scope")
            last = data["last"]
            if (
                len(last) != 4
                or type(last[0]) is not int
                or type(last[2]) is not int
                or not isinstance(last[1], str)
                or last[3] not in ("transfer", "swap")
            ):
                raise ValueError("position")
            return tuple(last)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ResearchRuntimeError(
                "invalid_input", "分页游标无效、已过期或数据快照改变，请重新开始查询。"
            ) from None
