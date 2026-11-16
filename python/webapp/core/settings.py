"""读取账户库（PostgreSQL）的连接配置。

配置来自项目根目录的 .env；如果操作系统环境变量里有同名变量，以环境变量为准。

同一台 PostgreSQL 服务器上有两套配置（profile）：
- development：开发库 chainscope_app
- test：测试库 chainscope_app_test（自动化测试会在这里反复建表、删表）

每套配置有两个账号：
- runtime（运行账号）：网站后端日常读写数据用，不能建表；
- migrator（迁移账号）：数据库 owner，只在执行迁移（建表、改结构）时用。

某个账号的密码只有在真正要用这个账号时才检查。这样网站后端运行时，
.env 里可以不放迁移账号的密码，减少泄漏面。
连接一律使用 TLS 并校验服务器证书（verify-full），不允许明文连接。
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import URL

from .errors import ConfigError

# 项目根目录：本文件在 python/webapp/core/ 下，往上三级就是项目根目录。
ROOT = Path(__file__).resolve().parents[3]
PROFILES = ("development", "test")
ROLES = ("runtime", "migrator")
# verify-full：既加密，又核对服务器证书是否由我们的 CA 签发、证书里的地址是否就是要连的主机，
# 防止有人冒充数据库服务器。
SSLMODE = "verify-full"
# 数据库名、账号名只允许小写字母开头的小写字母、数字、下划线（PostgreSQL 标识符最长 63 个字符）。
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}")

# 每套配置各自使用的变量名；主机、端口和 CA 证书两套共用，所以不在这里。
VARIABLES = {
    "development": {
        "database": "WEBAPP_DB_NAME",
        "runtime": ("WEBAPP_DB_USER", "WEBAPP_DB_PASSWORD"),
        "migrator": ("WEBAPP_DB_MIGRATOR_USER", "WEBAPP_DB_MIGRATOR_PASSWORD"),
    },
    "test": {
        "database": "WEBAPP_TEST_DB_NAME",
        "runtime": ("WEBAPP_TEST_DB_USER", "WEBAPP_TEST_DB_PASSWORD"),
        "migrator": (
            "WEBAPP_TEST_DB_MIGRATOR_USER",
            "WEBAPP_TEST_DB_MIGRATOR_PASSWORD",
        ),
    },
}


@dataclass(frozen=True)
class Account:
    """一个数据库账号。frozen=True 表示创建后不能再修改。"""

    user: str
    # repr=False：打印或写日志时不显示密码。
    password: str = field(repr=False)
    # 这个密码对应的 .env 变量名，缺密码时用来提示“请填写哪个变量”。
    password_variable: str = field(repr=False)


@dataclass(frozen=True)
class DatabaseProfile:
    """一套完整的连接配置：连哪台服务器、哪个库、用哪两个账号。"""

    name: str  # "development" 或 "test"
    host: str
    port: int
    database: str
    sslrootcert: Path  # 用来校验服务器证书的 CA 证书文件
    runtime: Account
    migrator: Account

    def account(self, role: str) -> Account:
        """取出某个用途的账号；这时才检查它的密码有没有填。"""
        if role not in ROLES:
            raise ConfigError(f"未知的数据库账号用途：{role}")
        account = self.runtime if role == "runtime" else self.migrator
        if not account.password:
            raise ConfigError(f"请在项目 .env 填写 {account.password_variable}")
        return account

    def url(self, role: str) -> URL:
        """生成 SQLAlchemy 连接地址。

        用 URL.create 逐项传入，而不是自己拼字符串：密码里有 @、: 等特殊字符也不会出错，
        而且 SQLAlchemy 打印 URL 时会把密码显示成 ***。
        """
        account = self.account(role)
        return URL.create(
            "postgresql+psycopg",  # 数据库类型＋驱动（psycopg 3）
            username=account.user,
            password=account.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )

    def connect_args(self) -> dict:
        """传给 psycopg 驱动的连接参数：TLS 设置、连接超时、连接名称。"""
        return {
            "sslmode": SSLMODE,
            "sslrootcert": str(self.sslrootcert),
            "connect_timeout": 5,  # 5 秒连不上就报错，不无限等待
            # 在数据库端（如 pg_stat_activity）能看出是哪个程序连上来的。
            "application_name": f"chainscope-webapp-{self.name}",
        }


def _values() -> dict:
    """读取 .env，再用操作系统环境变量覆盖同名项。

    interpolate=False：不展开值里的 ${...}，避免密码里恰好有 $ 时被改掉。
    """
    return {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}


def _required(values: dict, name: str) -> str:
    """必填项：没填或只有空格就报错，并指出是哪个变量。"""
    value = (values.get(name) or "").strip()
    if not value:
        raise ConfigError(f"请在项目 .env 配置 {name}")
    return value


def _identifier(values: dict, name: str) -> str:
    """必填，且必须是合法的数据库名或账号名。"""
    value = _required(values, name)
    if not IDENTIFIER.fullmatch(value):
        raise ConfigError(f"{name} 只能是小写字母开头的小写字母、数字或下划线")
    return value


def configured(profile: str, values: dict | None = None) -> bool:
    """这套配置有没有填数据库名。测试用它决定要不要跳过真实数据库测试。"""
    values = _values() if values is None else values
    return bool((values.get(VARIABLES[profile]["database"]) or "").strip())


def load_profile(profile: str = "development", values: dict | None = None):
    """读取并检查一套配置，返回 DatabaseProfile。

    `values` 平时不传，自动读取 .env；测试时传入一个字典，不依赖真实的 .env。
    任何一项不合格都立即报错，不带着错误的配置继续运行。
    """
    if profile not in PROFILES:
        raise ConfigError(f"未知的数据库配置：{profile}（可选 {', '.join(PROFILES)}）")
    values = _values() if values is None else values
    names = VARIABLES[profile]

    # 1. 两套配置共用的部分：主机、端口、CA 证书。
    host = _required(values, "WEBAPP_DB_HOST")
    try:
        port = int(values.get("WEBAPP_DB_PORT") or "5432")  # 不填默认 5432
    except ValueError:
        raise ConfigError("WEBAPP_DB_PORT 必须是整数") from None
    if not 1 <= port <= 65535:
        raise ConfigError("WEBAPP_DB_PORT 超出范围")
    cert = Path(_required(values, "WEBAPP_DB_SSLROOTCERT"))
    if not cert.is_file():
        raise ConfigError("WEBAPP_DB_SSLROOTCERT 指向的 CA 证书文件不存在")

    # 2. 这套配置自己的数据库名和两个账号；密码这里只读取，不检查是否为空。
    database = _identifier(values, names["database"])
    accounts = {}
    for role in ROLES:
        user_name, password_name = names[role]
        accounts[role] = Account(
            _identifier(values, user_name),
            values.get(password_name) or "",
            password_name,
        )

    # 3. 隔离规则：运行账号和迁移账号必须分开。
    if accounts["runtime"].user == accounts["migrator"].user:
        raise ConfigError("运行账号和迁移账号必须是两个不同的账号")

    # 4. 测试会删表重建，所以测试配置绝不能指向开发库，也不能用开发账号。
    #    数据库端已经禁止开发账号连测试库，这里在程序里再加一道检查。
    if profile == "test":
        dev = VARIABLES["development"]
        if database == (values.get(dev["database"]) or "").strip():
            raise ConfigError("测试库不能和开发库同名")
        dev_users = {(values.get(dev[role][0]) or "").strip() for role in ROLES}
        if dev_users & {account.user for account in accounts.values()}:
            raise ConfigError("测试配置不能使用开发账号")
    return DatabaseProfile(profile, host, port, database, cert, **accounts)
