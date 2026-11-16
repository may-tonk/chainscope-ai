"""账户库（PostgreSQL）的迁移入口，和 MySQL 采集库的迁移完全分开。

Alembic 每次执行迁移命令都会运行这个文件。常用命令（在项目根目录执行）：

    alembic -n webapp upgrade head                    # 升级开发库 chainscope_app
    alembic -n webapp -x profile=test upgrade head    # 升级测试库 chainscope_app_test
    alembic -n webapp upgrade head --sql              # 只输出 SQL 供审阅，不连接数据库

-n webapp 表示使用 alembic.ini 里的 [webapp] 一节；不加 -n 用的是 MySQL 那一节。

这里一律用对应配置的迁移账号连接。连上之后先让服务器确认账号、数据库、
public 的权限和 TLS，任何一项不符合都拒绝迁移（见 webapp/db/database.py 的 require_migrator）。
"""

from alembic import context
from sqlalchemy.exc import SQLAlchemyError

from webapp.core.errors import MigrationRefused
from webapp.core.settings import load_profile
from webapp.db.database import database_error, engine_for, require_migrator
from webapp.db.tables import metadata

config = context.config


def run(connection, profile):
    """在调用方已经开启的事务里：先检查连接身份，再执行迁移。

    事务由谁负责（谁开启、谁提交）必须明确。run() 自己不开启也不提交事务，
    事务归调用 run() 的一方：
    - 自己建连接时（下面的 engine.begin()），成功就提交，出错就整体回滚；
    - 外部传入连接时，调用方必须先 connection.begin()，迁移结束后由调用方提交或回滚。

    为什么不能交给 Alembic 自己开事务：require_migrator() 的 SELECT 会让 SQLAlchemy
    自动开始一个事务（autobegin）。Alembic 看到连接已经在事务里，就认为“事务归外面管”，
    自己不再提交。如果外面也没人提交，连接关闭时迁移会被整体回滚，表白建了。

    PostgreSQL 的建表语句可以放在事务里：中途出错会整体回滚，不会留下建了一半的表。
    """
    if not connection.in_transaction():
        raise MigrationRefused(
            "传入的连接必须已由调用方开启事务（connection.begin()），迁移结果由调用方提交"
        )
    require_migrator(connection, profile)
    context.configure(connection=connection, target_metadata=metadata)
    # 已在调用方的事务里，这里不会另开事务，只是 Alembic 的标准写法。
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    # 离线模式（命令带 --sql）：不连数据库，只把要执行的 SQL 打印出来。
    context.configure(
        dialect_name="postgresql", target_metadata=metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    # 确定要迁移哪套配置：
    # 测试代码通过 config.attributes 传入；命令行通过 -x profile=test 传入；都没有就是开发库。
    name = config.attributes.get("profile") or context.get_x_argument(
        as_dictionary=True
    ).get("profile", "development")
    profile = load_profile(name)
    # 记下当前迁移的是哪套配置，迁移文件的 downgrade() 据此判断是否允许删表（只允许测试库）。
    config.attributes["active_profile"] = profile.name
    if config.attributes.get("connection") is not None:
        # 调用方传入的连接：事务的开启和提交都由调用方负责，见 run() 的说明。
        run(config.attributes["connection"], profile)
    else:
        # 自己用迁移账号建立连接。engine.begin() 表示这个事务归本文件所有：
        # 代码块正常结束就提交，抛出异常就回滚，然后关闭连接。
        engine = engine_for(profile, "migrator")
        try:
            with engine.begin() as connection:
                run(connection, profile)
        except SQLAlchemyError as exc:
            # 数据库报错转成只含 SQLSTATE 的安全提示，不输出服务器原文。
            raise database_error(exc) from None
        finally:
            engine.dispose()
