"""CLI for collection jobs, MySQL persistence and evidence verification."""

import argparse
import csv
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .config import ROOT, load_settings
from .database import (
    check_database,
    database_error,
    engine_for,
    initialize,
    verify_batch,
)
from .dex import inspect_pool, load_pools, save_pools, set_pool_enabled
from .errors import ChainScopeError
from .holders import import_labels, top_holders, verify_holders
from .jobs import job_lock, load_job, verify_job
from .market import Http, snapshot_market
from .parser import quantity
from .pipeline import run_collection
from .query import query_job
from .query import report as storage_report
from .records import inspect_csv
from .rpc_client import RpcClient
from .sync import (
    create,
    pools_discover,
    portal_for,
    redo_job,
    run_recheck,
    run_sync,
    say,
    status_with_head,
)


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
        "--from",
        dest="from_time",
        help="按北京时间指定起点（如 2026-10-01T08:00，整秒），与 --start-block 二选一",
    )
    job.add_argument(
        "--to",
        dest="to_time",
        help="按北京时间指定终点（不包含，不能晚于现在）；省略则持续跟随最新确认区块",
    )
    job.add_argument(
        "--window-size", type=int, default=50_000, help="每个窗口的区块数，默认 50000"
    )
    sync = commands.add_parser(
        "sync", help="按窗口采集到最终确认高度；随时可中断，下次自动续采"
    )
    sync.add_argument("--job", default="bem")
    sync.add_argument(
        "--primary",
        choices=("alchemy", "sqd"),
        default="alchemy",
        help="转账主来源，默认 Alchemy",
    )
    sync.add_argument(
        "--phase",
        choices=("all", "collect", "supplement"),
        default="all",
        help="all 分窗采集并正式入库，再补买卖；collect 仅暂存；supplement 提交已暂存完整窗口并补买卖",
    )
    sync.add_argument(
        "--until-block", type=int, help="本次固定截止区块，不修改任务的长期结束位置"
    )
    sync.add_argument("--max-windows", type=int, help="本次最多处理的窗口数")
    sync.add_argument(
        "--event-limit",
        type=int,
        default=50_000,
        help="单个窗口最多事件数，超过则自动拆分窗口",
    )
    sync.add_argument(
        "--block-hard-limit",
        type=int,
        default=50_000,
        help="单个区块（Transfer+Swap）最多条数，超过则隔离该区块，默认 50000",
    )
    sync.add_argument(
        "--max-requests", type=int, help="本次 HTTP 请求数软上限（在窗口之间检查）"
    )
    sync.add_argument(
        "--max-cu", type=int, help="本次 Alchemy 计算单元软上限（估算，窗口之间检查）"
    )
    sync.add_argument(
        "--max-minutes",
        type=float,
        help="本次运行时间软上限（分钟）；当前窗口会做完，超出部分记入 budget_overrun_seconds",
    )
    sync.add_argument(
        "--no-reconcile",
        action="store_true",
        help="已禁用：待核验数据必须隔离；仅采集请用 --phase collect",
    )
    sync.add_argument(
        "--trace-memory", action="store_true", help="记录本次运行的 Python 内存峰值"
    )
    status = commands.add_parser("status", help="查看任务水位、积压和失败窗口")
    status.add_argument("--job", default="bem")
    status.add_argument(
        "--max-lag-blocks",
        type=int,
        help="与刚读取的最终确认高度相比，落后超过该区块数时退出码 2；"
        "读不到高度时退出码 4",
    )
    recheck = commands.add_parser(
        "recheck", help="随机复查已完成窗口；有变化的窗口标为失败并回退水位"
    )
    recheck.add_argument("--job", default="bem")
    recheck.add_argument("--sample", type=int, default=20)
    recheck.add_argument("--seed", type=int, help="随机种子，便于复现")
    recheck.add_argument("--no-reconcile", action="store_true")
    redo = commands.add_parser(
        "redo", help="把已完成窗口重置为待采集（旧解析版本，或指定区块范围）"
    )
    redo.add_argument("--job", default="bem")
    redo.add_argument("--from-block", type=int)
    redo.add_argument("--to-block", type=int)
    query = commands.add_parser(
        "query", help="按北京时间统计转账；范围为 [from, to)，超出水位会提示部分覆盖"
    )
    query.add_argument("--job", default="bem")
    query.add_argument("--from", dest="start", required=True, help="如 2026-10-01")
    query.add_argument(
        "--to", dest="end", required=True, help="如 2026-10-08（不包含）"
    )
    report = commands.add_parser("report", help="各表存储占用和三类查询耗时")
    report.add_argument("--job", default="bem")
    verify_job_cmd = commands.add_parser(
        "verify-job", help="从 MySQL 重新计算每个完成窗口，核对数量、金额和指纹"
    )
    verify_job_cmd.add_argument("--job", default="bem")
    pools = commands.add_parser(
        "pools",
        help="交易池：discover 自动发现 / list / add 手工添加 / enable / disable",
    )
    pools.add_argument(
        "action", choices=("discover", "list", "add", "enable", "disable")
    )
    pools.add_argument("--job", default="bem")
    pools.add_argument("--pool", help="池子地址（add/enable/disable 需要）")
    pools.add_argument(
        "--created-block", type=int, help="add：池子创建区块，默认任务起点"
    )
    pools.add_argument(
        "--from-block", type=int, help="discover：从该区块开始扫描，默认部署区块"
    )
    pools.add_argument(
        "--to-block", type=int, help="discover：扫描到该区块，默认最终确认高度"
    )
    pools.add_argument(
        "--chunk-blocks",
        type=int,
        default=1_000_000,
        help="discover：每段扫描的区块数，默认 100 万（约 9 天）",
    )
    holders = commands.add_parser(
        "holders", help="持仓排行（默认前 80），附标签和是否合约；可指定历史区块"
    )
    holders.add_argument("--job", default="bem")
    holders.add_argument("--top", type=int, default=80)
    holders.add_argument(
        "--at-block", type=int, help="统计到该区块为止（需在已核验范围内）"
    )
    holders_verify = commands.add_parser(
        "holders-verify",
        help="用链上 totalSupply 和 balanceOf 核对持仓，并记录是否合约",
    )
    holders_verify.add_argument("--job", default="bem")
    holders_verify.add_argument("--top", type=int, default=20)
    labels_cmd = commands.add_parser("labels-import", help="从 TOML 文件导入地址标签")
    labels_cmd.add_argument(
        "--file", type=Path, default=ROOT / "config" / "address_labels.toml"
    )
    market = commands.add_parser(
        "market", help="保存 CoinGecko 价格/市值/成交量、池子行情和 4 小时 K 线"
    )
    market.add_argument("--job", default="bem")
    market.add_argument(
        "--ohlcv-limit", type=int, default=180, help="K 线根数，0 表示不取"
    )
    market.add_argument("--no-chain", action="store_true", help="不读链上储备做核对")
    args = parser.parse_args()
    code = 0
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
                from_time=args.from_time,
                to_time=args.to_time,
                window_size=args.window_size,
            )
        elif args.command == "sync":
            if (
                args.event_limit < 1
                or (args.max_windows or 1) < 1
                or args.block_hard_limit < args.event_limit
            ):
                raise ChainScopeError(
                    "窗口数和事件上限必须为正数，单区块上限不能小于事件上限"
                )
            result = run_sync(
                load_settings(),
                args.job,
                reconcile=not args.no_reconcile,
                primary=args.primary,
                phase=args.phase,
                until_block=args.until_block,
                trace_memory=args.trace_memory,
                max_windows=args.max_windows,
                max_seconds=None if args.max_minutes is None else args.max_minutes * 60,
                max_requests=args.max_requests,
                max_compute_units=args.max_cu,
                event_limit=args.event_limit,
                block_hard_limit=args.block_hard_limit,
            )
            code = (
                3
                if result.get("quarantined")
                or result.get("quarantined_remaining")
                or result.get("stopped") == "incomplete"
                else 0
            )
            if (
                not code
                and args.primary == "alchemy"
                and not result.get("coverage_complete")
            ):
                code = 2
        elif args.command == "recheck":
            result = run_recheck(
                load_settings(),
                args.job,
                reconcile=not args.no_reconcile,
                sample=args.sample,
                seed=args.seed,
            )
            code = 0 if result["ok"] else 3
        elif args.command in ("pools", "holders-verify", "market"):
            result, code = run_with_sources(args)
        elif args.command == "status":
            settings = load_settings()
            engine = engine_for(settings)
            try:
                with httpx.Client(timeout=httpx.Timeout(30, connect=10)) as http:
                    portal = portal_for(settings, http)
                    result = status_with_head(
                        engine, args.job, portal, max_lag_blocks=args.max_lag_blocks
                    )
            finally:
                engine.dispose()
            code = {True: 2, "unknown": 4}.get(result["behind"], 0)
        elif args.command == "query":
            settings = load_settings()
            engine = engine_for(settings)
            try:
                with httpx.Client(timeout=httpx.Timeout(60, connect=15)) as http:
                    portal = portal_for(settings, http)
                    result = query_job(engine, portal, args.job, args.start, args.end)
            finally:
                engine.dispose()
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
                elif args.command == "redo":
                    result = redo_job(
                        engine, args.job, first=args.from_block, last=args.to_block
                    )
                elif args.command == "report":
                    result = storage_report(engine, args.job)
                elif args.command == "verify-job":
                    result = verify_job(engine, args.job)
                    code = 0 if result["ok"] else 3
                elif args.command == "holders":
                    result = top_holders(
                        engine, args.job, top=args.top, at_block=args.at_block
                    )
                elif args.command == "labels-import":
                    result = import_labels(engine, args.file)
                else:
                    with RpcClient(settings.require_rpc()) as rpc:
                        chain_id = quantity(rpc.call("eth_chainId", []), "chain_id")
                        if chain_id != 56:
                            raise ChainScopeError("RPC 不是 BSC 主网")
                    result = {
                        "chain_id": chain_id,
                        **check_database(engine, full=True),
                    }
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
    return code


def run_with_sources(args) -> tuple[dict, int]:
    """Commands that need MySQL plus the Portal, the RPC or market APIs."""
    settings = load_settings()
    engine = engine_for(settings)
    try:
        with httpx.Client(timeout=httpx.Timeout(60, connect=15)) as http:
            if args.command == "market":
                chain = (
                    nullcontext()
                    if args.no_chain
                    else RpcClient(settings.require_rpc())
                )
                with chain as rpc:
                    result = snapshot_market(
                        engine,
                        Http(http),
                        args.job,
                        rpc=rpc,
                        api_key=settings.coingecko_key,
                        ohlcv_limit=args.ohlcv_limit,
                    )
                return result, 0
            if args.command == "holders-verify":
                with RpcClient(settings.require_rpc()) as rpc:
                    result = verify_holders(engine, rpc, args.job, top=args.top)
                return result, 0 if result["ok"] else 3
            if args.action == "discover":
                portal = portal_for(settings, http, say)
                return pools_discover(
                    engine,
                    args.job,
                    portal,
                    from_block=args.from_block,
                    to_block=args.to_block,
                    chunk_blocks=args.chunk_blocks,
                ), 0
            if args.action == "list":
                with engine.connect() as connection:
                    found = load_pools(connection, args.job, enabled_only=False)
                return {"job_id": args.job, "pools": list(found.values())}, 0
            if not args.pool:
                raise ChainScopeError("请用 --pool 指定池子地址")
            pool = args.pool.lower()
            if args.action == "add":
                with engine.connect() as connection:
                    job = load_job(connection, args.job)
                with RpcClient(settings.require_rpc()) as rpc:
                    row = inspect_pool(rpc, job["token_address"], pool)
                row["created_block"] = args.created_block or job["start_block"]
                with job_lock(engine, args.job):
                    saved = save_pools(
                        engine, args.job, [row], "manual", enable_known=False
                    )
                if not saved["added"]:
                    raise ChainScopeError("该池子已存在；用 pools enable 启用")
                return {
                    "added": saved["added"][0],
                    "windows_reset": saved["windows_reset"],
                    "next": "池子默认停用；确认无误后执行 pools enable，下次 sync 回补",
                }, 0
            with job_lock(engine, args.job):
                result = set_pool_enabled(
                    engine, args.job, pool, args.action == "enable"
                )
            return result, 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
