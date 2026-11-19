"""显式准备 Memory 的 PostgreSQL schema 与官方 checkpoint 表，不修改账户表。

使用现有 development/test 配置及其迁移账号，继续校验目标库、TLS 和最小权限。
只有传入 --apply 才执行 DDL；业务记忆表由后续独立迁移创建。
此命令会改变 schema、授权和 checkpoint 内部表，不能作为只读健康检查执行。
本文件归入 python/tools/memory；环境初始化入口与后续 Memory 业务迁移职责分开。
"""

import argparse
import json
from typing import TypedDict
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg import sql
from psycopg.rows import dict_row

from webapp.core.settings import load_profile

SCHEMAS = ("research", "research_checkpoint")


def connect(profile, role, schema="public", autocommit=False):
    """根据可信 profile、账号用途和固定 schema 返回 psycopg 连接，由调用方关闭。

    保持 TLS verify-full，并分别限制连接、SQL 和锁等待时间。
    schema 只由本工具常量或默认值传入；凭证通过驱动参数传递，不拼接进可打印 DSN。
    """
    account = profile.account(role)
    return psycopg.connect(
        host=profile.host,
        port=profile.port,
        dbname=profile.database,
        user=account.user,
        password=account.password,
        sslmode="verify-full",
        sslrootcert=str(profile.sslrootcert),
        connect_timeout=5,
        options=f"-c search_path={schema} -c statement_timeout=30000 -c lock_timeout=5000",
        application_name="chainscope-memory-environment",
        autocommit=autocommit,
        # 官方 checkpoint 初始化器按列名读取结果，因此必须使用字典行工厂。
        row_factory=dict_row,
    )


def prepare(profile):
    """为选定 profile 准备两个 schema 和 checkpoint 表，无返回值。

    schema 与默认授权在一个事务内提交；官方 checkpoint 迁移使用独立连接，
    与前一事务不构成原子操作。中途失败可检查其迁移水位后重跑，不能当作已全部完成。
    """
    runtime = sql.Identifier(profile.runtime.user)
    # 先检查服务端实际身份，防止本地配置名称正确却连接到错误数据库。
    with connect(profile, "migrator") as conn:
        found = conn.execute("""SELECT current_user AS account, current_database() AS database,
            (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser,
            (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS tls""").fetchone()
        if (
            found["account"] != profile.migrator.user
            or found["database"] != profile.database
            or found["superuser"]
            or not found["tls"]
        ):
            raise ValueError(
                "Database identity does not match the expected restricted migrator"
            )
        for name in SCHEMAS:
            schema = sql.Identifier(name)
            owner = conn.execute(
                "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname=%s",
                (name,),
            ).fetchone()
            # IF NOT EXISTS 不能验证对象归属；在此拒绝接管其他账号拥有的同名 schema。
            if owner and owner["owner"] != profile.migrator.user:
                raise ValueError("Existing schema belongs to a different owner")
            conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                    schema, sql.Identifier(profile.migrator.user)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, runtime)
            )
            # 默认权限约束将来由迁移账号创建的表；下方的 ALL TABLES 授权覆盖现有表。
            # 只授予运行所需读写和序列使用权限，未授予 schema CREATE。
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(schema, runtime)
            )
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}"
                ).format(schema, runtime)
            )
    # 官方迁移含并发索引，必须使用独立 autocommit 连接，不能塞进业务事务。
    with connect(profile, "migrator", "research_checkpoint", autocommit=True) as conn:
        PostgresSaver(conn).setup()
    # 同时覆盖已初始化的 checkpoint 表，工具重复执行时保持授权一致。
    with connect(profile, "migrator") as conn:
        for name in SCHEMAS:
            schema = sql.Identifier(name)
            conn.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(schema, runtime)
            )
            conn.execute(
                sql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
                ).format(schema, runtime)
            )


class ProbeState(TypedDict):
    """仅用于环境验收的公开固定文本，不包含用户对话或任何凭证。"""

    value: str


def verify(profile):
    """输入已初始化 profile，返回持久化和权限验证结果，最后删除本次测试 thread。

    会写入固定公开样例；随机 thread_id 只属于本次操作，不读取或清理用户会话。
    必须关闭第一条连接再读，才能验证内容来自数据库，而非仍在内存中的图状态。
    """
    thread_id = "memory-environment-" + uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    builder = StateGraph(ProbeState)
    # 小图只产生一个可核对的状态变化，不调用任何模型、工具或真实研究任务。
    builder.add_node("confirm", lambda state: {"value": state["value"] + "-stored"})
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", END)
    try:
        with connect(
            profile, "runtime", "research_checkpoint", autocommit=True
        ) as conn:
            builder.compile(checkpointer=PostgresSaver(conn)).invoke(
                {"value": "environment"}, config
            )
        with connect(
            profile, "runtime", "research_checkpoint", autocommit=True
        ) as conn:
            graph = builder.compile(checkpointer=PostgresSaver(conn))
            if graph.get_state(config).values.get("value") != "environment-stored":
                raise ValueError("Checkpoint was not readable after reconnection")
            permissions = conn.execute(
                "SELECT has_schema_privilege(current_user,'research','CREATE') AS business_ddl, has_schema_privilege(current_user,'research_checkpoint','CREATE') AS checkpoint_ddl"
            ).fetchone()
            if permissions["business_ddl"] or permissions["checkpoint_ddl"]:
                raise ValueError("Runtime role unexpectedly has DDL privileges")
        return {
            "profile": profile.name,
            "checkpoint_roundtrip": True,
            "runtime_ddl": False,
        }
    finally:
        # 正常或异常均尝试删除唯一测试线程；清理失败会向外报告，不伪装为完全成功。
        with connect(
            profile, "runtime", "research_checkpoint", autocommit=True
        ) as conn:
            PostgresSaver(conn).delete_thread(thread_id)


def main():
    """解析 --apply 和 profile，逐套初始化并验收，以退出码报告失败。

    缺少 --apply 时仅提示计划；错误只输出类型及 SQLSTATE，不回显连接和数据库原文。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--profile", choices=("development", "test", "all"), default="all"
    )
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"apply_required": True, "schemas": SCHEMAS}))
        return
    # profile 加载器已有开发/测试隔离检查；不能用同一数据库冒充两套验证。
    names = ("development", "test") if args.profile == "all" else (args.profile,)
    for name in names:
        try:
            profile = load_profile(name)
            prepare(profile)
            print(json.dumps(verify(profile)))
        except Exception as error:  # noqa: BLE001 -- 管理工具边界，防止驱动异常回显凭证。
            print(
                json.dumps(
                    {
                        "profile": name,
                        "success": False,
                        "error_type": type(error).__name__,
                        "sqlstate": getattr(error, "sqlstate", None),
                    }
                )
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
