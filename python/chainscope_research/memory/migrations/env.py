"""Memory 独立迁移入口；只迁移 research 内的自有表，启动网站不会调用。

连接参数复用现有 profile 加载器，业务存储本身不依赖网站配置模块。
身份检查与 PostgreSQL DDL 处于同一个显式事务，中途失败整体回滚。
"""

from alembic import context
from sqlalchemy.exc import SQLAlchemyError

from chainscope_research.memory.storage.database import (
    SCHEMA,
    VERSION_TABLE,
    engine_for,
    require_identity,
)

config = context.config


def run(connection, profile):
    """在调用方事务中验证迁移身份并应用版本；不自行提交或接管其他 schema。"""
    if not connection.in_transaction():
        raise ValueError("Memory 迁移连接必须处于显式事务内")
    require_identity(connection, profile, "migrator")
    # 固定事务锁使两个部署进程不能同时初始化同一条迁移链。
    connection.exec_driver_sql("SELECT pg_advisory_xact_lock(836201117)")
    context.configure(
        connection=connection,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()
    # 默认 DML 授权用于业务表；版本表只向运行账号开放读取。
    runtime = connection.dialect.identifier_preparer.quote(profile.runtime.user)
    connection.exec_driver_sql(
        f"REVOKE INSERT, UPDATE, DELETE ON research.memory_alembic_version FROM {runtime}"
    )


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        literal_binds=True,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    from webapp.core.settings import load_profile

    name = config.attributes.get("profile") or context.get_x_argument(
        as_dictionary=True
    ).get("profile", "development")
    profile = load_profile(name)
    config.attributes["active_profile"] = profile.name
    supplied = config.attributes.get("connection")
    if supplied is not None:
        run(supplied, profile)
    else:
        engine = engine_for(profile, "migrator")
        try:
            with engine.begin() as connection:
                run(connection, profile)
        except SQLAlchemyError as error:
            code = getattr(getattr(error, "orig", None), "sqlstate", None) or "unknown"
            raise RuntimeError(f"Memory 迁移失败，SQLSTATE={code}") from None
        finally:
            engine.dispose()
