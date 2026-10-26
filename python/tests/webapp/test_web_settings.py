"""账户库配置读取的测试（settings.py）。只用假配置字典，不连数据库，也不读真实 .env。"""

import pytest

from webapp.core.errors import ConfigError
from webapp.core.settings import configured, load_profile

SECRET = "not-a-real-password-123"


@pytest.fixture
def values(tmp_path):
    """一份完整的假配置；证书用临时文件代替，密码是假的。"""
    cert = tmp_path / "ca.crt"
    cert.write_text("test")
    return {
        "WEBAPP_DB_HOST": "192.0.2.10",
        "WEBAPP_DB_PORT": "5432",
        "WEBAPP_DB_SSLROOTCERT": str(cert),
        "WEBAPP_DB_NAME": "chainscope_app",
        "WEBAPP_DB_USER": "chainscope_app",
        "WEBAPP_DB_PASSWORD": SECRET,
        "WEBAPP_DB_MIGRATOR_USER": "chainscope_migrator",
        "WEBAPP_DB_MIGRATOR_PASSWORD": "",
        "WEBAPP_TEST_DB_NAME": "chainscope_app_test",
        "WEBAPP_TEST_DB_USER": "chainscope_test",
        "WEBAPP_TEST_DB_PASSWORD": SECRET,
        "WEBAPP_TEST_DB_MIGRATOR_USER": "chainscope_test_migrator",
        "WEBAPP_TEST_DB_MIGRATOR_PASSWORD": SECRET,
    }


def test_profile_builds_tls_url_without_showing_the_password(values):
    """连接地址各项正确、强制 verify-full，并且打印配置或地址时看不到密码。"""
    profile = load_profile("development", values)
    url = profile.url("runtime")
    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.host, url.port, url.database) == (
        "chainscope_app",
        "192.0.2.10",
        5432,
        "chainscope_app",
    )
    assert profile.connect_args()["sslmode"] == "verify-full"
    assert profile.connect_args()["sslrootcert"] == values["WEBAPP_DB_SSLROOTCERT"]
    assert SECRET not in repr(profile)
    assert SECRET not in str(url)


def test_password_is_only_required_for_the_account_in_use(values):
    """迁移账号密码没填时，运行账号照样能用；只有真要用迁移账号时才报缺密码。"""
    profile = load_profile("development", values)
    profile.url("runtime")
    with pytest.raises(ConfigError, match="WEBAPP_DB_MIGRATOR_PASSWORD"):
        profile.url("migrator")


def test_test_profile_uses_its_own_names(values):
    """测试配置读的是 WEBAPP_TEST_DB_* 这一组变量。"""
    profile = load_profile("test", values)
    assert profile.database == "chainscope_app_test"
    assert profile.runtime.user == "chainscope_test"
    assert profile.migrator.user == "chainscope_test_migrator"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"WEBAPP_DB_HOST": ""}, "WEBAPP_DB_HOST"),
        ({"WEBAPP_DB_PORT": "abc"}, "WEBAPP_DB_PORT"),
        ({"WEBAPP_DB_PORT": "70000"}, "WEBAPP_DB_PORT"),
        ({"WEBAPP_DB_SSLROOTCERT": "missing.crt"}, "CA 证书"),
        ({"WEBAPP_DB_NAME": "Bad-Name"}, "WEBAPP_DB_NAME"),
        ({"WEBAPP_DB_MIGRATOR_USER": "chainscope_app"}, "不同的账号"),
    ],
)
def test_invalid_settings_are_rejected(values, change, message):
    """缺主机、端口不对、证书不存在、库名不合法、两个账号相同，都要报错并指出是哪一项。"""
    with pytest.raises(ConfigError, match=message):
        load_profile("development", {**values, **change})


def test_test_profile_can_never_reach_the_development_database(values):
    """测试配置不能和开发库同名，也不能使用开发账号（测试会删表）。"""
    with pytest.raises(ConfigError, match="同名"):
        load_profile("test", {**values, "WEBAPP_TEST_DB_NAME": "chainscope_app"})
    with pytest.raises(ConfigError, match="开发账号"):
        load_profile("test", {**values, "WEBAPP_TEST_DB_USER": "chainscope_app"})


def test_unknown_profile_and_unconfigured_profile(values):
    """不认识的配置名报错；没填库名时 configured() 返回 False。"""
    with pytest.raises(ConfigError, match="未知"):
        load_profile("production", values)
    assert configured("test", values)
    assert not configured("test", {**values, "WEBAPP_TEST_DB_NAME": ""})


def test_errors_never_contain_passwords(values):
    """报错文字里不出现密码。"""
    with pytest.raises(ConfigError) as caught:
        load_profile("development", {**values, "WEBAPP_DB_HOST": ""})
    assert SECRET not in str(caught.value)
