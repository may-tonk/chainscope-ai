"""CLI for bounded collection, MySQL persistence and evidence verification."""

import argparse
import csv
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .config import load_settings
from .database import (
    check_database,
    database_error,
    engine_for,
    initialize,
    verify_batch,
)
from .errors import ChainScopeError
from .parser import quantity
from .pipeline import run_collection
from .records import inspect_csv
from .rpc_client import RpcClient


def main() -> int:
    parser = argparse.ArgumentParser(description="ChainScope：链上转账采集与核验工具")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="检查已有 Transfer CSV 的有限样本")
    inspect.add_argument("--input", type=Path, required=True)
    inspect.add_argument("--limit", type=int, default=3, help="读取前 N 条记录，1–1000")
    commands.add_parser("check", help="检查 BSC RPC 和 MySQL 连接，隐藏连接密钥")
    commands.add_parser("init-db", help="创建指定 MySQL 数据库并执行 Alembic 迁移")
    for name in ("fetch", "collect"):
        command = commands.add_parser(
            name,
            help="采集固定区间"
            + (
                "并写入 MySQL（默认不生成归档文件）"
                if name == "collect"
                else "并保存有容量限制的调试归档（不入库）"
            ),
        )
        command.add_argument("--token", required=True, help="BSC token 合约地址")
        command.add_argument("--from-block", type=int, required=True)
        command.add_argument("--to-block", type=int, required=True)
        command.add_argument("--max-pages", type=int, default=20)
        command.add_argument(
            "--archive-max-mib",
            type=int,
            default=10,
            help="启用归档时的单批上限，默认 10 MiB；raw 总上限为 100 MiB",
        )
        if name == "collect":
            command.add_argument(
                "--save-raw",
                action="store_true",
                help="显式保存 RPC 响应和批次摘要，仅用于调试；默认关闭",
            )
        command.add_argument(
            "--receipt-limit",
            type=int,
            default=10,
            help="核验前 N 个不同交易的全部已采集事件",
        )
    verify = commands.add_parser("verify", help="读取 MySQL 批次并精确计算原始金额汇总")
    verify.add_argument("--batch-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_csv(args.input, args.limit)
        elif args.command == "init-db":
            result = initialize(load_settings())
        elif args.command in ("collect", "fetch"):
            result = run_collection(
                load_settings(),
                args.token,
                args.from_block,
                args.to_block,
                persist=args.command == "collect",
                max_pages=args.max_pages,
                receipt_limit=args.receipt_limit,
                save_raw=getattr(args, "save_raw", False),
                archive_max_bytes=args.archive_max_mib * 1024 * 1024,
            )
        else:
            settings = load_settings()
            engine = engine_for(settings)
            try:
                if args.command == "verify":
                    result = verify_batch(engine, args.batch_id)
                else:
                    with RpcClient(settings.require_rpc()) as rpc:
                        chain_id = quantity(rpc.call("eth_chainId", []), "chain_id")
                        if chain_id != 56:
                            raise ChainScopeError("RPC 不是 BSC 主网")
                    result = {"chain_id": chain_id, **check_database(engine)}
            finally:
                engine.dispose()
    except SQLAlchemyError as exc:
        print(str(database_error(exc)), file=sys.stderr)
        return 1
    except ChainScopeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (
        OSError,
        UnicodeError,
        ValueError,
        csv.Error,
        TypeError,
        KeyError,
        httpx.HTTPError,
    ) as exc:
        # Unexpected provider shapes and IO errors may include sensitive strings.
        print(
            f"操作失败（{type(exc).__name__}）；检查配置或原始批次文件", file=sys.stderr
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
