"""账户库的连接，以及“这条连接到底是谁”的检查。

配置文件写的是“应该连谁”，但真正连上之后，要以数据库服务器的回答为准：
当前是哪个账号、哪个库、有没有加密、有没有建表权限。迁移前就靠这个检查来防止用错账号。
"""

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from ..core.errors import DatabaseError, MigrationRefused
from ..core.settings import ROOT, DatabaseProfile

# 只读查询：问服务器“这条连接的真实情况”。各列含义：
#   account        当前登录的账号
#   database       当前连接的数据库
#   schema         默认的 schema（我们约定为 public）
#   can_use        对 public 有没有 USAGE 权限（能否使用里面的表）
#   can_create     对 public 有没有 CREATE 权限（能否建表；运行账号应为 false）
#   superuser      是否超级管理员（如 postgres；迁移和运行都不允许用）
#   server_version PostgreSQL 版本
#   tls            这条连接是否加密
#   tls_version    加密协议版本，如 TLSv1.3
# pg_stat_ssl 是系统视图，每条连接一行；用 pg_backend_pid()（当前连接的进程号）找到自己这一行。
IDENTITY = sa.text(
    """
    SELECT current_user AS account,
           current_database() AS database,
           current_schema() AS schema,
           has_schema_privilege(current_user, 'public', 'USAGE') AS can_use,
           has_schema_privilege(current_user, 'public', 'CREATE') AS can_create,
           (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS superuser,
           current_setting('server_version') AS server_version,
           s.ssl AS tls,
           s.version AS tls_version
    FROM pg_stat_ssl AS s
    WHERE s.pid = pg_backend_pid()
    """
)


def engine_for(profile: DatabaseProfile, role: str = "runtime"):
    """创建连接池（engine）。role 默认用运行账号，迁移时传 "migrator"。

    连接池参数按第一版并发目标（10 人在线、每秒 5 次登录）设置，A17 实测后再调整：
    - pool_size=5：平时保持最多 5 条连接；
    - max_overflow=5：忙的时候最多再临时多开 5 条；
    - pool_timeout=10：10 秒内拿不到空闲连接就报错，不无限排队；
    - pool_pre_ping=True：取出连接前先确认它还活着（例如虚拟机重启过）；
    - hide_parameters=True：出错时不把 SQL 参数（可能含邮箱、哈希）写进报错。
    """
    return sa.create_engine(
        profile.url(role),
        connect_args=profile.connect_args(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        hide_parameters=True,
    )


def database_error(exc: SQLAlchemyError) -> DatabaseError:
    """把数据库异常转成安全的提示。

    只保留 SQLSTATE（PostgreSQL 的标准错误编号，如 42501 = 权限不足、
    28P01 = 密码错误），丢掉服务器原文，因为原文里可能有账号名或主机名。
    """
    code = getattr(getattr(exc, "orig", None), "sqlstate", None) or "unknown"
    return DatabaseError(
        f"PostgreSQL 操作失败（SQLSTATE {code}）；请检查虚拟机、.env 中的账号密码和证书"
    )


def migration_config(profile_name: str) -> Config:
    """在 Python 里执行账户库迁移时用的 Alembic 配置（测试会用到）。

    读取 alembic.ini 里的 [webapp] 一节（不是 MySQL 用的默认节），
    并告诉迁移入口 env.py 要迁移哪套配置（development 或 test）。
    """
    config = Config(str(ROOT / "alembic.ini"), ini_section="webapp")
    config.attributes["profile"] = profile_name
    return config


def identity(connection) -> dict:
    """执行上面的 IDENTITY 查询，返回 {列名: 值} 字典。"""
    return dict(connection.execute(IDENTITY).mappings().one())


def require_migrator(connection, profile: DatabaseProfile) -> dict:
    """迁移前的检查：任何一项不符合就拒绝迁移，一张表都不建。

    必须同时满足：
    1. 当前账号就是这套配置的迁移账号（不是运行账号，也不是 postgres）；
    2. 当前数据库就是这套配置的库（开发配置不能连到测试库，反之亦然）；
    3. 默认 schema 是 public；
    4. 对 public 同时有 USAGE 和 CREATE 权限；
    5. 不是超级管理员；
    6. 连接已加密。
    把所有不符合的项一起列出来，方便一次改完。
    """
    found = identity(connection)
    problems = []
    if found["account"] != profile.migrator.user:
        problems.append(f"当前账号是 {found['account']}，应为 {profile.migrator.user}")
    if found["database"] != profile.database:
        problems.append(f"当前数据库是 {found['database']}，应为 {profile.database}")
    if found["schema"] != "public":
        problems.append(f"当前 schema 是 {found['schema']}，应为 public")
    if not (found["can_use"] and found["can_create"]):
        problems.append("迁移账号缺少 public 的 USAGE 或 CREATE 权限")
    if found["superuser"]:
        problems.append("不能用超级管理员账号迁移")
    if not found["tls"]:
        problems.append("连接没有使用 TLS")
    if problems:
        raise MigrationRefused("拒绝迁移：" + "；".join(problems))
    return found
