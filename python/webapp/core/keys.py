"""服务端密钥：从 .env 读取 4 把用途不同的密钥（方案 10.1 节）。

| 变量 | 用途 |
| --- | --- |
| AUTH_SESSION_KEY    | 计算登录令牌的指纹（数据库只存指纹）；也用于绑定注册流程 |
| AUTH_CSRF_KEY       | 计算 CSRF 令牌的指纹 |
| AUTH_EMAIL_CODE_KEY | 计算邮箱验证码、注册凭证的校验值 |
| AUTH_RATE_LIMIT_KEY | 把邮箱、IP 变成限速表里的匿名键 |

规则：
- 每把至少 256 位（32 字节）随机数，用 python/tools/generate_webapp_keys.py 生成，
  格式是 base64url（只含字母、数字、- 和 _）；
- 4 把必须互不相同，一把泄漏不会连带其他用途；
- 缺少或格式不对就拒绝启动，绝不使用默认值或演示密钥；
- 这些密钥不是用来加密密码的：密码只有 Argon2id 哈希，没有任何密钥能还原密码。

AUTH_KEY_VERSION 是密钥版本号（默认 1），写进每条会话、验证记录。
第一版的换钥方式：生成新密钥并把版本号加 1，旧版本的会话全部失效，用户重新登录。
"""

import base64
import binascii
import os
import re
from dataclasses import dataclass, field

from dotenv import dotenv_values

from .errors import ConfigError
from .settings import ROOT

MIN_KEY_BYTES = 32
KEY_NAMES = (
    "AUTH_SESSION_KEY",
    "AUTH_CSRF_KEY",
    "AUTH_EMAIL_CODE_KEY",
    "AUTH_RATE_LIMIT_KEY",
)
BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class AuthKeys:
    """4 把密钥和版本号。repr=False：打印这个对象时不会显示密钥内容。"""

    session: bytes = field(repr=False)
    csrf: bytes = field(repr=False)
    email_code: bytes = field(repr=False)
    rate_limit: bytes = field(repr=False)
    version: int = 1


def decode_key(name: str, value: str) -> bytes:
    """把 base64url 文本还原成字节，并检查长度。错误提示只说变量名，不回显内容。"""
    value = value.strip()
    if not value:
        raise ConfigError(
            f"请在项目 .env 配置 {name}（用 generate_webapp_keys.py 生成）"
        )
    if not BASE64URL.fullmatch(value):
        raise ConfigError(f"{name} 格式不正确，应为 base64url 文本")
    try:
        # base64 要求长度是 4 的倍数，生成工具去掉了末尾的 = ，这里补回去。
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise ConfigError(f"{name} 格式不正确，应为 base64url 文本") from None
    if len(raw) < MIN_KEY_BYTES:
        raise ConfigError(f"{name} 太短，至少需要 {MIN_KEY_BYTES} 字节（256 位）")
    return raw


def load_keys(values: dict | None = None) -> AuthKeys:
    """读取并检查 4 把密钥。`values` 平时不传（读 .env）；测试时传入字典。"""
    if values is None:
        values = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
    raw = [decode_key(name, values.get(name) or "") for name in KEY_NAMES]
    if len(set(raw)) != len(raw):
        raise ConfigError("4 把 AUTH_*_KEY 必须互不相同")
    try:
        version = int(values.get("AUTH_KEY_VERSION") or "1")
    except ValueError:
        raise ConfigError("AUTH_KEY_VERSION 必须是整数") from None
    # 数据库里 key_version 是 smallint，最大 32767。
    if not 1 <= version <= 32767:
        raise ConfigError("AUTH_KEY_VERSION 必须在 1～32767 之间")
    return AuthKeys(*raw, version=version)
