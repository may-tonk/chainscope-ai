"""密码规则和密码哈希（对应方案第 5 节和 6.1 节）。

规则：
- 长度按 NFC 规范化后的 Unicode 码点计数，8～128 个字符（2026-10-25 用户决定最低 8 位，
  理由和代价见方案 5.1 节）；
- 不去掉首尾空格、不转小写、不截断，用户输入什么就是什么；
- 不强制“必须有大写、数字、符号”这类组合规则；
- 拒绝常见弱密码，只和本地列表比对，密码不会发送到任何外部服务
  （列表目前只有几十条，挡不住 12345678 这类常见短密码，用户已知并接受）；
- 只保存 Argon2id 哈希，不保存明文，也不保存可以还原的加密密码。
"""

import contextlib
import functools
import secrets
import threading
import unicodedata

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

from ..core.errors import PasswordRejected, ServiceBusy, WebappError
from ..core.settings import ROOT

MIN_LENGTH = 8
MAX_LENGTH = 128
# 规范化之前先粗略限制输入长度：防止有人提交几 MB 的“密码”，
# 让服务器在规范化和哈希上耗尽 CPU 和内存。128 个字符 × 4 已足够宽松。
MAX_INPUT_CHARS = MAX_LENGTH * 4
COMMON_PASSWORDS = ROOT / "config" / "common_passwords.txt"
# 项目相关的词：密码基本就是“项目名＋几个字符”时也算弱密码。
PROJECT_WORDS = ("chainscope",)

# Argon2id 参数取 OWASP 推荐的最低配置：内存 19 MiB（19456 KiB）、2 次迭代、1 个并行通道。
# 内存越大、迭代越多，攻击者暴力破解越慢，但服务器每次登录也越慢。
# 等在真正运行后端的服务器上按并发目标实测（验收 A17）后，再决定是否提高。
hasher = PasswordHash((Argon2Hasher(time_cost=2, memory_cost=19456, parallelism=1),))

# 同一个后端进程里，最多同时算 4 个密码哈希（每个约占 19 MiB 内存）。
# 第 5 个请求排队等待，等 HASH_WAIT_SECONDS 秒还没轮到就返回“服务繁忙”，
# 防止大量登录请求同时涌入把内存和 CPU 吃光。按每秒 5 次登录的目标，4 个足够。
HASH_SLOTS = 4
HASH_WAIT_SECONDS = 5
_hash_slots = threading.BoundedSemaphore(HASH_SLOTS)


@contextlib.contextmanager
def _hash_slot():
    """占用一个哈希名额，用完自动归还；等不到名额抛出 ServiceBusy。"""
    if not _hash_slots.acquire(timeout=HASH_WAIT_SECONDS):
        raise ServiceBusy()
    try:
        yield
    finally:
        _hash_slots.release()


def normalize(password: str) -> str:
    """NFC 规范化：同一个字在不同输入法下可能是不同的码点序列。

    例如 “é” 可能是 1 个码点（U+00E9），也可能是 “e”加一个重音符号共 2 个码点。
    规范化后两者一样，注册时和登录时就不会因为输入方式不同而对不上。
    """
    return unicodedata.normalize("NFC", password)


@functools.cache  # 只在第一次调用时读文件，之后直接用缓存
def common_passwords() -> frozenset[str]:
    """读取弱密码列表；# 开头的行是注释，空行跳过；统一转成不区分大小写的形式。"""
    lines = COMMON_PASSWORDS.read_text(encoding="utf-8").splitlines()
    return frozenset(
        normalize(line).casefold()
        for line in lines
        if line.strip() and not line.startswith("#")
    )


def _repeated(text: str) -> bool:
    """整个密码是不是 1～4 个字符的片段重复出来的，例如 'aaaaaaaaaaaaaaa'、'abcabcabcabcabc'。"""
    return any(
        len(text) % size == 0 and text == text[:size] * (len(text) // size)
        for size in range(1, 5)
    )


def _sequential(text: str) -> bool:
    """整个密码是不是一串连续字符，例如 'abcdefghijklmnop'、'ponmlkjihgfedcba'。

    做法：算出每两个相邻字符的编码差，如果全是 +1 或全是 -1，就是连续的。
    """
    steps = {ord(b) - ord(a) for a, b in zip(text, text[1:])}
    return steps in ({1}, {-1})


def _mostly_project_word(text: str) -> bool:
    """密码是否基本就是项目名加几个字符，例如 'chainscope2026!'。

    规则：包含项目名，且总长度比项目名多不到 8 个字符。
    """
    return any(word in text and len(text) < len(word) + 8 for word in PROJECT_WORDS)


def check_new_password(password: str, *, email: str | None = None) -> str:
    """检查注册或修改时设置的新密码；通过后返回规范化后的密码，用来生成哈希。

    不通过时抛出 PasswordRejected，其中的 code 以后给前端用来显示对应提示。
    `email` 传入时，还会拒绝和邮箱、或和邮箱 @ 前面部分相同的密码。
    """
    # 第一道：原始输入太长就直接拒绝，不做任何处理。
    if len(password) > MAX_INPUT_CHARS:
        raise PasswordRejected("password_too_long", f"密码最多 {MAX_LENGTH} 个字符")
    # 第二道：规范化后再按码点数检查长度。
    value = normalize(password)
    if len(value) < MIN_LENGTH:
        raise PasswordRejected("password_too_short", f"密码至少 {MIN_LENGTH} 个字符")
    if len(value) > MAX_LENGTH:
        raise PasswordRejected("password_too_long", f"密码最多 {MAX_LENGTH} 个字符")
    # 第三道：弱密码检查。casefold 是比 lower 更彻底的“忽略大小写”，
    # 只用于比较，保存的仍是用户原样输入的密码。
    folded = value.casefold()
    weak = (
        folded in common_passwords()
        or _repeated(folded)
        or _sequential(folded)
        or _mostly_project_word(folded)
    )
    if email:
        email_key = email.strip().casefold()
        weak = weak or folded in (email_key, email_key.split("@")[0])
    if weak:
        # 不说明具体是哪条规则命中，提示保持笼统即可。
        raise PasswordRejected(
            "password_common", "这个密码太常见或太容易猜到，请换一个"
        )
    return value


def hash_password(password: str) -> str:
    """生成要存进数据库的密码哈希。调用前应先通过 check_new_password。

    结果形如 $argon2id$v=19$m=19456,t=2,p=1$<盐>$<哈希>，算法参数和随机盐都写在里面，
    所以同一个密码每次生成的哈希都不一样，以后校验时也知道当初用的是什么参数。
    """
    with _hash_slot():
        return hasher.hash(normalize(password))


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """登录时校验密码。返回 (是否正确, 新哈希)。

    新哈希通常是 None。只有当数据库里的哈希是用旧参数生成的（例如以后调高了 Argon2 参数），
    密码又正确时，才会返回按新参数生成的哈希，由登录流程替换旧值。
    这样升级参数不需要用户重设密码。
    """
    if len(password) > MAX_INPUT_CHARS:
        return False, None
    try:
        with _hash_slot():
            return hasher.verify_and_update(normalize(password), password_hash)
    except UnknownHashError:
        # 数据库里的哈希格式无法识别，说明账户数据损坏，而不是用户输错密码。
        # 明确报错，不能悄悄当成“密码错误”掩盖过去。
        raise WebappError("账户保存的密码哈希格式无法识别") from None


@functools.cache  # 只在第一次用到时生成一次
def _unknown_user_hash() -> str:
    """一个随机密码的哈希，专门给“邮箱不存在”的情况陪跑用，任何人都猜不中。"""
    return hash_password(secrets.token_urlsafe(32))


def verify_unknown_user(password: str) -> bool:
    """邮箱不存在时也做一次同样耗时的哈希校验，结果永远是失败。

    如果不存在的邮箱立刻返回、存在的邮箱要花几十毫秒算哈希，
    攻击者就能通过响应快慢判断哪些邮箱注册过。陪跑一次可以缩小这种时间差。
    """
    verify_password(password, _unknown_user_hash())
    return False
