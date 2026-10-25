"""密码规则和哈希的测试（passwords.py）。不需要数据库。"""

import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from webapp.core.errors import PasswordRejected, WebappError
from webapp.security import passwords

GOOD = "river tea lamp orbit 47"


def rejected(password, **kwargs) -> str:
    """辅助函数：断言这个密码会被拒绝，并返回拒绝原因的编号。"""
    with pytest.raises(PasswordRejected) as caught:
        passwords.check_new_password(password, **kwargs)
    return caught.value.code


def test_length_limits_are_inclusive():
    """8 和 128 个字符都允许，7 和 129 个都拒绝（边界值都要测）。"""
    assert rejected("x7Kq" + "abc") == "password_too_short"  # 7
    assert passwords.check_new_password("x7Kq" + "abcd")  # 8
    assert passwords.check_new_password("x7Kq9" * 25 + "abc")  # 128
    assert rejected("x7Kq9" * 25 + "abcd") == "password_too_long"  # 129


def test_length_counts_code_points_after_nfc():
    """长度按 NFC 规范化后计算：两个码点写成的 é 只算 1 个字符。"""
    decomposed = "é"  # é written as two code points; NFC makes it one
    # 输入时 10 个码点，NFC 规范化后只有 7 个：太短。
    assert rejected(decomposed * 3 + "q8w7") == "password_too_short"
    value = passwords.check_new_password(decomposed * 3 + "q8w7e")
    assert value == "é" * 3 + "q8w7e"
    assert len(value) == 8


def test_oversized_input_is_rejected_before_normalizing():
    """超长输入在设置和登录时都直接拒绝，不做规范化和哈希。"""
    assert rejected("a" * (passwords.MAX_INPUT_CHARS + 1)) == "password_too_long"
    ok, new_hash = passwords.verify_password(
        "a" * 10_000, passwords.hash_password(GOOD)
    )
    assert (ok, new_hash) == (False, None)


@pytest.mark.parametrize(
    "password",
    [
        "PasswordPassword",  # common list, compared case-insensitively
        "correct horse battery staple",
        "aaaaaaaaaaaaaaaa",  # one character repeated
        "abcabcabcabcabcabc",  # short unit repeated
        "abcdefghijklmnop",  # one alphabet run
        "ponmlkjihgfedcba",
        "ChainScope2026!!",  # project name plus a few characters
    ],
)
def test_weak_passwords_are_rejected(password):
    """常见密码（不分大小写）、重复片段、连续字符、项目名加几位，都算弱密码。"""
    assert rejected(password) == "password_common"


def test_password_equal_to_email_is_rejected():
    """密码不能等于邮箱，也不能等于邮箱 @ 前面的部分。"""
    assert rejected("someone.long@example.com", email="Someone.Long@example.com")
    assert rejected("someone.longname", email="someone.longname@example.com")
    assert passwords.check_new_password(GOOD, email="someone@example.com") == GOOD


def test_spaces_and_case_are_kept():
    """首尾空格和大小写原样保留：少了空格或改了大小写都登录不上。"""
    value = passwords.check_new_password("  " + GOOD + "  ")
    assert value == "  " + GOOD + "  "
    stored = passwords.hash_password(value)
    assert passwords.verify_password(value, stored) == (True, None)
    assert passwords.verify_password(GOOD, stored)[0] is False
    assert passwords.verify_password(value.upper(), stored)[0] is False


def test_hash_is_argon2id_with_the_chosen_parameters():
    """哈希是 Argon2id、参数正确、不含明文、每次盐不同；格式损坏的哈希明确报错。"""
    stored = passwords.hash_password(GOOD)
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert GOOD not in stored
    assert passwords.hash_password(GOOD) != stored  # random salt
    assert passwords.verify_password(GOOD, stored) == (True, None)
    assert passwords.verify_password(GOOD + "x", stored) == (False, None)
    with pytest.raises(WebappError, match="无法识别") as caught:
        passwords.verify_password(GOOD, "not a hash")
    assert GOOD not in str(caught.value)


def test_nfc_equivalent_input_verifies():
    """注册和登录时 é 的写法不同（1 个码点或 2 个码点）也能登录。"""
    composed = "café river tea lamp"
    stored = passwords.hash_password(composed)
    assert passwords.verify_password("café river tea lamp", stored)[0]


def test_older_parameters_are_upgraded_after_a_good_login():
    """旧参数生成的哈希：密码正确时返回按新参数生成的哈希，密码错误时不返回。"""
    weaker = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192, parallelism=1),))
    old = weaker.hash(GOOD)
    ok, new_hash = passwords.verify_password(GOOD, old)
    assert ok
    assert new_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert passwords.verify_password("wrong password value", old) == (False, None)


def test_unknown_user_check_always_fails_after_hashing(monkeypatch):
    """邮箱不存在时也确实做了一次哈希校验，而且结果一定是失败。"""
    calls = []
    real = passwords.verify_password
    monkeypatch.setattr(
        passwords, "verify_password", lambda *a: calls.append(a) or real(*a)
    )
    assert passwords.verify_unknown_user(GOOD) is False
    assert len(calls) == 1


def test_common_list_loads_long_entries_only():
    """弱密码列表能读到，而且每条都不短于最低长度（更短的本来就会因长度被拒绝）。"""
    entries = passwords.common_passwords()
    assert len(entries) > 20
    assert all(len(entry) >= passwords.MIN_LENGTH for entry in entries)
