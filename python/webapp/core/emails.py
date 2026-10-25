"""邮箱格式规则（方案 4.1 节）。前端校验以后与这里保持一致，但以后端为准。

第一版规则：
- 只接受常见的 ASCII 邮箱，去掉首尾空白后检查；
- 总长度不超过 254，@ 前面不超过 64；
- 登录不区分大小写：统一转小写得到 email_key，用来查找账户和建立唯一约束；
- 不删除地址里的点、不去掉 + 后缀，不把两个别名认作同一个账户。
"""

import re

from .errors import InvalidEmail

MAX_LENGTH = 254
MAX_LOCAL = 64
# @ 前面：字母、数字和常见符号，点不能在开头结尾，也不能连续出现。
LOCAL = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
)
# 域名的每一段：字母数字开头结尾，中间可以有连字符，每段最长 63。
LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def normalize_email(raw: str) -> tuple[str, str]:
    """检查邮箱格式，返回 (显示用的地址, email_key)。格式不对抛出 InvalidEmail。"""
    email = raw.strip()
    if not email.isascii() or len(email) > MAX_LENGTH or email.count("@") != 1:
        raise InvalidEmail()
    local, domain = email.split("@")
    labels = domain.split(".")
    if (
        not local
        or len(local) > MAX_LOCAL
        or not LOCAL.fullmatch(local)
        or len(labels) < 2
        or not all(LABEL.fullmatch(label) for label in labels)
        or not labels[-1].isalpha()  # 顶级域名（如 com、cn）只能是字母
        or len(labels[-1]) < 2
    ):
        raise InvalidEmail()
    return email, email.lower()
