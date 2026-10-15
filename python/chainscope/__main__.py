"""CLI for collection jobs, MySQL persistence and evidence verification."""

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
from .jobs import job_status
from .parser import quantity
from .pipeline import run_collection
from .records import inspect_csv
from .rpc_client import RpcClient
from .sync import create, run_sync


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
    job = commands.add_parser("job-create", help="创建长期采集任务（一个代币一个任务）")
    job.add_argument("--job", default="bem", help="任务名，默认 bem")
    job.add_argument("--token", required=True, help="BSC token 合约地址")
    job.add_argument(
        "--start-block", type=int, help="起始区块；省略时用 eth_getCode 查找部署区块"
    )
    job.add_argument(
        "--window-size", type=int, default=50_000, help="每个窗口的区块数，默认 50000"
    )
    sync = commands.add_parser(
        "sync", help="按窗口采集到最终确认高度；随时可中断，下次自动续采"
    )
    sync.add_argument("--job", default="bem")
    sync.add_argument("--max-windows", type=int, help="本次最多处理的窗口数")
    sync.add_argument("--max-minutes", type=float, help="本次运行时间上限（分钟）")
    sync.add_argument(
        "--event-limit",
        type=int,
        default=50_000,
        help="单个窗口最多事件数，超过则自动拆分窗口",
    )
    status = commands.add_parser("status", help="查看任务水位、积压和失败窗口")
    status.add_argument("--job", default="bem")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_csv(args.input, args.limit)
        elif args.command == "init-db":
            result = initialize(load_settings())
        elif args.command == "job-create":
            result = create(
                load_settings(),
                args.job,
                args.token,
                start_block=args.start_block,
                window_size=args.window_size,
            )
        elif args.command == "sync":
            if args.event_limit < 1 or (args.max_windows or 1) < 1:
                raise ChainScopeError("窗口数和事件上限必须为正数")
            result = run_sync(
                load_settings(),
                args.job,
                max_windows=args.max_windows,
                max_seconds=None if args.max_minutes is None else args.max_minutes * 60,
                event_limit=args.event_limit,
            )
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
                elif args.command == "status":
                    result = job_status(engine, args.job)
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
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
