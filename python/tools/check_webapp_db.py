"""账户库连接检查（只读，不建表、不改任何数据）。

对每套已配置的配置（development、test）和每个账号（运行账号、迁移账号），
用 TLS 连接一次，让服务器回答“当前是谁、连的哪个库、有什么权限”，然后确认：

- 运行账号不能建表；
- 迁移账号能建表，而且不是超级管理员；
- 开发运行账号连不上测试库（开发和测试隔离）；
- 不加密的连接会被服务器拒绝。

密码从 .env 读取，不会打印出来。结果以 JSON 输出，全部 "ok": true 时退出码为 0。

    python python/tools/check_webapp_db.py                  # 检查两套配置
    python python/tools/check_webapp_db.py --profile test   # 只检查测试库
"""

import argparse
import json
import sys
from dataclasses import replace

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from webapp.core.errors import WebappError
from webapp.core.settings import PROFILES, ROLES, configured, load_profile
from webapp.db.database import database_error, identity


def try_connect(profile, role, **connect_args):
    """连接一次，返回 (身份信息, 当前迁移版本)；失败时抛出安全的错误提示。

    connect_args 可以覆盖默认连接参数，例如 sslmode="disable" 用来测试不加密的连接会不会被拒绝。
    NullPool：不保留连接池，用完立即断开，只是检查用。
    """
    engine = sa.create_engine(
        profile.url(role),
        connect_args={**profile.connect_args(), **connect_args},
        poolclass=sa.pool.NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            found = identity(connection)
            # 还没迁移过的库里没有 alembic_version 表，这时版本记为 None。
            has_versions = sa.inspect(connection).has_table("alembic_version")
            revision = (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                if has_versions
                else None
            )
            return found, revision
    except SQLAlchemyError as exc:
        raise database_error(exc) from None
    finally:
        engine.dispose()


def check_account(profile, role) -> dict:
    """检查一个账号：能否连上，身份、加密和权限是否符合它的用途。"""
    result = {"profile": profile.name, "role": role, "database": profile.database}
    try:
        found, revision = try_connect(profile, role)
    except WebappError as exc:
        return {**result, "ok": False, "error": str(exc)}
    expected = profile.account(role).user
    problems = []
    if found["account"] != expected or found["database"] != profile.database:
        problems.append("连接到的账号或数据库与配置不一致")
    if not found["tls"]:
        problems.append("连接没有使用 TLS")
    if found["superuser"]:
        problems.append("账号是超级管理员")
    # 运行账号只能读写，不能建表；迁移账号必须能建表。
    if role == "runtime" and found["can_create"]:
        problems.append("运行账号不应有 public 的 CREATE 权限")
    if role == "migrator" and not (found["can_use"] and found["can_create"]):
        problems.append("迁移账号缺少 public 的 USAGE 或 CREATE 权限")
    return {
        **result,
        **found,
        "migration_revision": revision,
        "ok": not problems,
        "problems": problems,
    }


def expect_refused(label, profile, role, **connect_args) -> dict:
    """这次连接“应该被拒绝”：被拒绝才算通过，连上了反而说明隔离没生效。"""
    # 先确认密码已填写：否则会因为“缺密码”报错，被误判成“被服务器拒绝”。
    try:
        profile.account(role)
    except WebappError as exc:
        return {"check": label, "ok": False, "error": str(exc)}
    try:
        try_connect(profile, role, **connect_args)
    except WebappError as exc:
        return {"check": label, "ok": True, "refused_with": str(exc)}
    return {"check": label, "ok": False, "error": "连接被接受，隔离没有生效"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=(*PROFILES, "all"), default="all")
    args = parser.parse_args(argv)
    names = PROFILES if args.profile == "all" else (args.profile,)
    results = []
    profiles = {}

    # 第一部分：逐个检查每套配置的两个账号。
    for name in names:
        if not configured(name):
            results.append({"profile": name, "ok": False, "error": "未配置，跳过"})
            continue
        try:
            profiles[name] = load_profile(name)
        except WebappError as exc:
            results.append({"profile": name, "ok": False, "error": str(exc)})
            continue
        for role in ROLES:
            results.append(check_account(profiles[name], role))

    # 第二部分：隔离检查。前提是开发运行账号本身能正常连上：
    # 如果虚拟机没开、密码填错，所有连接都会失败，这时的“被拒绝”证明不了隔离有效。
    dev_reachable = any(
        item.get("profile") == "development"
        and item.get("role") == "runtime"
        and item["ok"]
        for item in results
    )
    if "development" in profiles and not dev_reachable:
        results.append(
            {
                "check": "隔离检查",
                "ok": False,
                "error": "开发运行账号本身连不上，无法判断",
            }
        )
    elif "development" in profiles:
        dev = profiles["development"]
        # 同一个账号改成不加密连接，服务器应拒绝。
        results.append(
            expect_refused("未加密连接被拒绝", dev, "runtime", sslmode="disable")
        )
        if configured("test"):
            # 开发运行账号去连测试库，服务器应拒绝。
            test_database = load_profile("test").database
            results.append(
                expect_refused(
                    "开发运行账号不能连接测试库",
                    replace(dev, database=test_database),
                    "runtime",
                )
            )
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0 if results and all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
