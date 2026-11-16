"""随机令牌、验证码和 HMAC 指纹。

为什么数据库只存“指纹”：
登录令牌、注册凭证、验证码本身就等于“钥匙”。如果数据库原样保存，
数据库一旦泄漏，别人拿着就能冒充用户登录。所以浏览器（或邮件）里给的是原值，
数据库里只存 HMAC-SHA256(服务端密钥, 原值) 的结果：
- 没有服务端密钥，从指纹推不回原值，也无法自己算出指纹；
- 校验时把收到的原值再算一次指纹，和数据库比对即可。

purpose（用途）会一起参与计算，所以同一个值用在不同地方得到的指纹也不同，
不能把一种凭证拿去冒充另一种。
"""

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32  # 256 位随机数
CODE_DIGITS = 6


def new_token() -> str:
    """生成 256 位安全随机令牌，文本形式（base64url，约 43 个字符）。

    secrets 模块使用操作系统的密码学安全随机源；普通的 random 模块可以被预测，不能用。
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_code() -> str:
    """生成 6 位数字验证码，例如 "042917"（保留开头的 0）。"""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def fingerprint(key: bytes, purpose: str, *parts: str) -> bytes:
    """计算 HMAC-SHA256 指纹，结果是 32 字节。

    各部分之间用 \\x00 分隔（正常文本里不会出现这个字符），
    避免 ("ab", "c") 和 ("a", "bc") 拼起来一样而得到相同指纹。
    """
    message = "\x00".join((purpose, *parts)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).digest()


def same(a: bytes, b: bytes) -> bool:
    """比较两个指纹是否相同。

    用 compare_digest 而不是 ==：== 遇到第一个不同的字节就提前返回，
    攻击者可以通过响应时间的细微差别一点点猜出内容；compare_digest 的耗时与内容无关。
    """
    return hmac.compare_digest(a, b)
