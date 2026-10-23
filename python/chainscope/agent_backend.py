"""Read-only planning adapter and an explicit bridge to the existing collector."""

import hashlib
import json
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa

from .agent_contracts import Completion, JobSpec, Progress, Target, Windows, Work
from .agent_store import shown
from .dex import FACTORIES
from .hybrid import sync_hybrid
from .ingestion import collection_progress
from .jobs import QUARANTINE, create_job
from .models import jobs, pool_scans, supplement_jobs, swap_windows, windows
from .parser import HASH, fixed_hex, quantity
from .query import BEIJING
from .reconcile import finalized_block
from .rpc_client import RpcClient
from .sqd import PARSER_VERSION
from .sync import deploy_block, has_code, portal_for, token_decimals
from .token_registry import AgentError


def block_at_or_before(rpc, moment, head):
    """Last finalized block at or before the requested instant, including equal seconds."""
    if moment > datetime.now(UTC):
        raise AgentError(
            "TARGET_IN_FUTURE",
            "指定截止时间尚未到达，不能采集未来数据",
            "prepare_again",
        )
    stamp = int(moment.timestamp())
    headers = {}

    def header(number):
        if number not in headers:
            block = rpc.call("eth_getBlockByNumber", [hex(number), False])
            if (
                not isinstance(block, dict)
                or quantity(block.get("number"), "block_number") != number
            ):
                raise AgentError("TARGET_HEADER_CONFLICT", "时间定位返回的区块头不一致")
            quantity(block.get("timestamp"), "timestamp")
            headers[number] = block
        return headers[number]

    if quantity(header(head)["timestamp"], "timestamp") <= stamp:
        raise AgentError(
            "TARGET_NOT_FINALIZED",
            "指定时间尚未全部最终确认，请稍后重试；没有改为其他截止时间",
            "prepare_again",
        )
    low, high = 0, head
    while low < high:
        middle = (low + high + 1) // 2
        if quantity(header(middle)["timestamp"], "timestamp") <= stamp:
            low = middle
        else:
            high = middle - 1
    if quantity(header(low)["timestamp"], "timestamp") > stamp:
        raise AgentError("TARGET_BEFORE_CHAIN", "截止时间早于链上可用历史")
    if low < head and quantity(header(low + 1)["timestamp"], "timestamp") <= stamp:
        raise AgentError("TARGET_HEADER_CONFLICT", "截止时间边界无法确认")
    return low, header(low)


def completion(progress, target):
    def reached(mark):
        return mark is not None and mark >= target

    return Completion(
        transfers=reached(progress.transfers_through),
        swaps=reached(progress.swaps_through),
        pools=reached(progress.pools_through),
        balances=reached(progress.balances_through),
    )


def remaining_work(progress, start, target):
    def next_block(mark):
        value = start if mark is None else max(start, mark + 1)
        return value if value <= target else None

    return Work(
        transfers_from=next_block(progress.transfers_through),
        swaps_from=next_block(progress.swaps_through),
        pools_from=next_block(progress.pools_through),
        balances_from=next_block(progress.balances_through),
    )


class Backend:
    def __init__(self, engine, settings, rpc_factory=None):
        self.engine, self.settings = engine, settings
        self.rpc_factory = rpc_factory or (lambda: RpcClient(settings.require_rpc()))
        self.validation_requests = 0
        # Set by the worker: one JSON line per collection event, kept per run.
        self.log_path = None

    def inspect(self, token):
        # A single repeatable-read snapshot: concurrent window commits do not mix watermarks.
        with (
            self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as c,
            c.begin(),
        ):
            row = (
                c.execute(
                    sa.select(jobs).where(
                        jobs.c.chain_id == token.chain_id,
                        jobs.c.token_address == token.address,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None, Progress(observed_at=shown())
            job = dict(row)
            job_id = job["job_id"]

            def tally(table):
                rows = c.execute(
                    sa.select(table.c.status, sa.func.count())
                    .where(table.c.job_id == job_id)
                    .group_by(table.c.status)
                ).all()
                values = dict(rows)
                quarantined = c.scalar(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(
                        table.c.job_id == job_id,
                        table.c.status == "failed",
                        table.c.error_type.in_(QUARANTINE),
                    )
                )
                total = c.scalar(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(table.c.event_count), 0)
                    ).where(
                        table.c.job_id == job_id,
                        table.c.status == "done",
                    )
                )
                return Windows(
                    **{
                        k: int(values.get(k, 0))
                        for k in ("done", "pending", "running", "failed")
                    },
                    quarantined=int(quarantined),
                ), int(total)

            tw, transfers = tally(windows)
            sw, swaps = tally(swap_windows)
            staged = collection_progress(c, job_id)
            swap_mark = c.scalar(
                sa.select(supplement_jobs.c.verified_through).where(
                    supplement_jobs.c.job_id == job_id
                )
            )
            if swap_mark is not None:
                swap_mark = (
                    min(swap_mark, job["verified_through"])
                    if job["verified_through"] is not None
                    else None
                )
            discovered = (
                c.execute(sa.select(pool_scans).where(pool_scans.c.job_id == job_id))
                .mappings()
                .first()
            )
            config = hashlib.sha256(
                json.dumps(FACTORIES, sort_keys=True).encode()
            ).hexdigest()
            pool_mark = (
                discovered["scanned_through"]
                if discovered and discovered["configuration"] == config
                else None
            )
            return job, Progress(
                collected_through=staged["collected_through"],
                transfers_through=job["verified_through"],
                swaps_through=swap_mark,
                pools_through=pool_mark,
                balances_through=job["balances_through"],
                transfers=tw,
                swaps=sw,
                transfer_events=transfers,
                swap_events=swaps,
                pending_events=staged["pending_events_saved"],
                observed_at=shown(),
            )

    def legacy_busy(self, job_id):
        with self.engine.connect() as c:
            return (
                c.scalar(
                    sa.text("SELECT IS_FREE_LOCK(:name)"),
                    {"name": f"chainscope:job:{job_id}"},
                )
                == 0
            )

    def finalized(self):
        with self.rpc_factory() as rpc:
            if quantity(rpc.call("eth_chainId", []), "chain_id") != 56:
                raise AgentError("CHAIN_MISMATCH", "RPC 不是 BNB Smart Chain 主网")
            return finalized_block(rpc)

    def target_and_job(self, token, requested, policy):
        job, progress = self.inspect(token)
        with self.rpc_factory() as rpc:
            if quantity(rpc.call("eth_chainId", []), "chain_id") != 56:
                raise AgentError("CHAIN_MISMATCH", "RPC 不是 BNB Smart Chain 主网")
            head = finalized_block(rpc)
            resolved_header = None
            if requested.mode == "time":
                end, resolved_header = block_at_or_before(
                    rpc, datetime.fromisoformat(requested.end_time), head
                )
            else:
                end = (
                    head
                    if requested.mode == "latest_finalized"
                    else requested.end_block
                )
            if end > head:
                raise AgentError(
                    "TARGET_NOT_FINALIZED", "截止区块尚未最终确认", "prepare_again"
                )
            if not has_code(rpc, token.address, end):
                raise AgentError(
                    "TOKEN_CONTRACT_UNAVAILABLE", "目标区块没有可用代币合约"
                )
            if job is None:
                deployed = deploy_block(rpc, token.address, end)
                decimals = token_decimals(rpc, token.address, end)
                if decimals is None:
                    raise AgentError(
                        "TOKEN_METADATA_INVALID",
                        "无法确认代币 decimals，不能建立准确金额任务",
                    )
                quantity(
                    rpc.call(
                        "eth_call",
                        [{"to": token.address, "data": "0x18160ddd"}, hex(end)],
                    ),
                    "totalSupply",
                )
                with self.engine.connect() as c:
                    collision = c.scalar(
                        sa.select(jobs.c.token_address).where(
                            jobs.c.job_id == token.token_id
                        )
                    )
                if collision is not None:
                    raise AgentError(
                        "JOB_ID_CONFLICT", "服务标识与另一代币的已有任务冲突"
                    )
                spec = JobSpec(
                    job_id=token.token_id,
                    start_block=deployed,
                    deploy_block=deployed,
                    decimals=decimals,
                    create_required=True,
                    window_size=policy.window_size,
                    parser_version=PARSER_VERSION,
                )
            else:
                if job["token_decimals"] is None or job["deploy_block"] is None:
                    raise AgentError(
                        "JOB_METADATA_INVALID",
                        "已有任务缺少部署位置或精度，需要处理后再使用 Agent",
                    )
                spec = JobSpec(
                    job_id=job["job_id"],
                    start_block=job["start_block"],
                    deploy_block=job["deploy_block"],
                    decimals=job["token_decimals"],
                    create_required=False,
                    window_size=job["window_size"],
                    parser_version=job["parser_version"],
                )
                if job["end_block"] is not None and end > job["end_block"]:
                    raise AgentError(
                        "TARGET_OUTSIDE_JOB",
                        "目标超出已有任务的固定范围，不自动修改任务",
                    )
            if end < spec.start_block:
                raise AgentError(
                    "TARGET_BEFORE_JOB", "截止区块早于任务起点", "prepare_again"
                )
            header = resolved_header or rpc.call(
                "eth_getBlockByNumber", [hex(end), False]
            )
            if quantity(header["number"], "block_number") != end:
                raise AgentError(
                    "TARGET_HEADER_CONFLICT", "返回区块头与指定截止位置不一致"
                )
            target = Target(
                block=end,
                block_hash=fixed_hex(header["hash"], HASH, "block_hash"),
                time_beijing=datetime.fromtimestamp(
                    quantity(header["timestamp"], "timestamp"), BEIJING
                ).isoformat(),
                mode=requested.mode,
                requested_time=datetime.fromisoformat(requested.end_time)
                .astimezone(BEIJING)
                .isoformat()
                if requested.mode == "time"
                else None,
            )
        return target, spec, progress

    def blockers(self, job, target):
        from .agent_contracts import Issue

        result = []
        with self.engine.connect() as c:
            current = (
                c.execute(sa.select(jobs).where(jobs.c.job_id == job.job_id))
                .mappings()
                .first()
            )
            if current and current["status"] != "active":
                result.append(
                    Issue(code="JOB_DISABLED", message="已有任务处于暂停状态")
                )
            for table in (windows, swap_windows):
                failed = c.scalar(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(
                        table.c.job_id == job.job_id,
                        table.c.start_block <= target,
                        table.c.status == "failed",
                        table.c.error_type.in_(QUARANTINE),
                    )
                )
                # Swap failures currently require review; no tool resets them.
                if table is swap_windows:
                    failed = c.scalar(
                        sa.select(sa.func.count())
                        .select_from(table)
                        .where(
                            table.c.job_id == job.job_id,
                            table.c.start_block <= target,
                            table.c.status == "failed",
                        )
                    )
                if failed:
                    result.append(
                        Issue(
                            code="DATA_REVIEW_REQUIRED",
                            message=f"{table.name} 有需要处理的失败或隔离窗口",
                        )
                    )
                crossing = c.scalar(
                    sa.select(table.c.start_block)
                    .where(
                        table.c.job_id == job.job_id,
                        table.c.start_block <= target,
                        table.c.end_block > target,
                        table.c.status != "done",
                    )
                    .limit(1)
                )
                if crossing is not None:
                    result.append(
                        Issue(
                            code="CUTOFF_WINDOW_CONFLICT",
                            message=f"目标落在 {table.name} 未完成窗口内部，不扩大截止范围",
                        )
                    )
        if self.legacy_busy(job.job_id):
            result.append(Issue(code="JOB_BUSY", message="已有采集进程持有该任务锁"))
        return result

    def validate(self, plan):
        from .token_registry import Registry

        registry = Registry.load()
        token = registry.resolve(plan.token.token_id)
        if (
            registry.fingerprint(token) != plan.registry_digest
            or plan.job.parser_version != PARSER_VERSION
        ):
            raise AgentError(
                "PLAN_STALE", "服务列表、策略或解析版本已变化", "prepare_again"
            )
        with self.rpc_factory() as rpc:
            try:
                if (
                    quantity(rpc.call("eth_chainId", []), "chain_id") != 56
                    or finalized_block(rpc) < plan.target.block
                ):
                    raise AgentError("CHAIN_MISMATCH", "当前来源不能确认计划目标")
                header = rpc.call(
                    "eth_getBlockByNumber", [hex(plan.target.block), False]
                )
                if (
                    quantity(header["number"], "block_number") != plan.target.block
                    or fixed_hex(header["hash"], HASH, "block_hash")
                    != plan.target.block_hash
                ):
                    raise AgentError(
                        "TARGET_HASH_CONFLICT", "目标区块哈希发生冲突，需要调查"
                    )
            finally:
                self.validation_requests = rpc.requests
        job, progress = self.inspect(token)
        if job is not None and any(
            (
                job["job_id"] != plan.job.job_id,
                job["start_block"] != plan.job.start_block,
                job["deploy_block"] != plan.job.deploy_block,
                job["token_decimals"] != plan.job.decimals,
                job["window_size"] != plan.job.window_size,
                job["parser_version"] != plan.job.parser_version,
                job["end_block"] is not None and job["end_block"] < plan.target.block,
            )
        ):
            raise AgentError("PLAN_STALE", "已有任务配置发生变化", "prepare_again")
        blockers = self.blockers(plan.job, plan.target.block)
        if blockers:
            raise AgentError(blockers[0].code, blockers[0].message)
        return progress

    def execute(self, plan, notify, stop, meter):
        """All business mutations stay in existing task creation / sync_hybrid."""
        job, _ = self.inspect(plan.token)
        if job is None:
            if stop():
                return {"stopped": "stop_requested"}
            create_job(
                self.engine,
                plan.job.job_id,
                plan.token.address,
                source="alchemy",
                start_block=plan.job.start_block,
                deploy_block=plan.job.deploy_block,
                window_size=plan.job.window_size,
                parser_version=plan.job.parser_version,
                decimals=plan.job.decimals,
            )
        with (
            self.rpc_factory() as rpc,
            httpx.Client(
                timeout=httpx.Timeout(120, connect=15), follow_redirects=False
            ) as http,
        ):
            portal = portal_for(self.settings, http)
            meter["read"] = lambda: rpc.requests + portal.stats["requests"]
            return sync_hybrid(
                self.engine,
                plan.job.job_id,
                portal,
                rpc,
                until_block=plan.target.block,
                max_seconds=max(
                    0.001,
                    plan.budget.max_runtime_seconds
                    - plan.budget.used_runtime_seconds
                    - meter["elapsed"](),
                ),
                max_requests=max(
                    1,
                    plan.budget.max_http_attempts
                    - plan.budget.used_http_attempts
                    - self.validation_requests,
                ),
                notify=notify,
                log_path=self.log_path,
                stop_requested=stop,
            )
