"""已有 MySQL 事实的只读访问；复用表定义，不导入采集执行、迁移或持仓刷新。

同步驱动由工具服务隔离到受限线程。所有查询由固定 SQLAlchemy 表达式构造，
不接受模型 SQL；以既有完成水位和已存区块时间固定保守快照，不补采链数据。
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TypeVar

import sqlalchemy as sa
from dotenv import dotenv_values
from sqlalchemy.engine import URL, Connection, Engine

from chainscope.models import (
    block_headers,
    jobs,
    supplement_jobs,
    swap_windows,
    swaps,
    transfers,
    window_events,
    windows,
)
from chainscope_research.contracts.results import DatasetScope
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.tool_settings import AnalysisSettings

Value = TypeVar("Value")


class ToolStopped(Exception):
    """线程已在安全检查点观察到停止信号，不能把它当作可重试数据库失败。"""


def utc_naive(value: datetime) -> datetime:
    """把明确带时区的业务边界转为 MySQL UTC DATETIME，不依赖本机时区。"""
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class SourceSnapshot:
    """实际可分析的单数据集快照；水位之后或未进入正式窗口的事件不参与结果。"""

    job: dict
    scope: DatasetScope
    first_time: datetime
    through: int
    swap_through: int
    stamp: str


@dataclass(frozen=True)
class LookupPage:
    """内部查询页及目标区块边界，用于判断目标是否受 Swap 独立补采缺口影响。"""

    rows: list[dict]
    target_last_block: int | None


def create_read_engine(root: Path, settings: AnalysisSettings) -> Engine:
    """按明确根目录读取已有 MySQL 凭证，创建连接池；不创建表或输出连接字符串。"""
    values = {**dotenv_values(root / ".env", interpolate=False), **os.environ}
    url = URL.create(
        "mysql+pymysql",
        username=values.get("MYSQL_USER"),
        password=values.get("MYSQL_PASSWORD"),
        host=values.get("MYSQL_HOST") or "127.0.0.1",
        port=int(values.get("MYSQL_PORT") or "3306"),
        database=values.get("MYSQL_DATABASE") or "chainscope",
        query={"charset": "utf8mb4"},
    )
    return sa.create_engine(
        url,
        isolation_level="REPEATABLE READ",
        pool_size=settings.max_parallel_calls,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.connect_timeout_seconds,
            "read_timeout": settings.read_timeout_seconds,
            "write_timeout": settings.read_timeout_seconds,
        },
    )


class ReadonlyFacts:
    """每次工具操作拥有一个只读事务，不共享 Connection 或用户状态。"""

    def __init__(self, engine: Engine):
        """注入已存在数据库的 Engine；测试可注入临时 SQLite，业务不运行 DDL。"""
        self.engine = engine

    def run(self, operation: Callable, stop: Event, deadline: float, timeout_ms: int):
        """在只读快照中执行受控操作并回滚关闭；失败不返回空数据代替。"""
        with self.engine.connect() as connection:
            if connection.dialect.name == "mysql":
                connection.exec_driver_sql(
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
                )
            elif connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA query_only = ON")
            else:
                raise ResearchRuntimeError(
                    "resource_unavailable", "当前事实仓库方言不受支持。"
                )
            try:
                return operation(FactsView(connection, stop, deadline, timeout_ms))
            finally:
                connection.rollback()


class FactsView:
    """事务内的查询视图；不暴露任意 SQL 执行接口给模型或工具参数。"""

    def __init__(
        self, connection: Connection, stop: Event, deadline: float, timeout_ms: int
    ):
        """保存当前事务的资源与协作式停止信号，不能跨工作线程使用。"""
        self.connection = connection
        self.stop = stop
        self.deadline = deadline
        self.timeout_ms = timeout_ms

    def check(self) -> None:
        """SQL 之间与读取行时检查停止和期限；数据库正在执行时另由 SELECT 超时限制。"""

        if self.stop.is_set():
            raise ToolStopped()
        if time.monotonic() >= self.deadline:
            raise ResearchRuntimeError("timeout", "工具已停止等待或执行超过期限。")

    def rows(self, statement):
        """执行固定 SELECT 并返回映射；MySQL 使用服务端 SELECT 时间限制。"""
        self.check()
        remaining_ms = min(
            self.timeout_ms, max(1, int((self.deadline - time.monotonic()) * 1000))
        )
        statement = statement.prefix_with(
            f"/*+ MAX_EXECUTION_TIME({remaining_ms}) */", dialect="mysql"
        )
        return self.connection.execute(statement).mappings()

    def source(
        self,
        job_ids: tuple[str, ...],
        chain_id: int,
        token: str | None,
        cutoff: datetime,
    ) -> SourceSnapshot:
        """从已授权任务和既有缓存选择唯一快照；多数据集需要明确代币，不猜测范围。

        若完成水位区块缺少时间缓存，采用该水位内且不晚于调用截止的最新已存
        区块，明确披露保守边界；不拿最后一条事件时间或采集处理时间代替截止。
        """
        condition = [jobs.c.job_id.in_(job_ids), jobs.c.chain_id == chain_id]
        if token:
            condition.append(jobs.c.token_address == token.lower())
        candidates = list(self.rows(sa.select(jobs).where(*condition).limit(2)))
        if len(candidates) != 1:
            raise ResearchRuntimeError(
                "resource_unavailable",
                "授权范围内没有唯一可用数据集；请明确链和代币范围。",
            )
        job = dict(candidates[0])
        mark = job["verified_through"]
        if mark is None:
            raise ResearchRuntimeError(
                "resource_unavailable", "该数据集尚无已完成采集范围。"
            )
        where = [
            block_headers.c.chain_id == chain_id,
            block_headers.c.block_number.between(job["start_block"], mark),
            block_headers.c.block_time_utc <= utc_naive(cutoff),
        ]
        header = self.rows(
            sa.select(block_headers)
            .where(*where)
            .order_by(block_headers.c.block_number.desc())
            .limit(1)
        ).first()
        if header is None:
            raise ResearchRuntimeError(
                "resource_unavailable",
                "已有记录缺少可用截止区块时间，当前无法确定分析范围。",
            )
        through = int(header["block_number"])
        first = self.rows(
            sa.select(block_headers.c.block_time_utc)
            .where(
                block_headers.c.chain_id == chain_id,
                block_headers.c.block_number.between(job["start_block"], through),
            )
            .order_by(block_headers.c.block_number)
            .limit(1)
        ).first()
        swap_mark = self.rows(
            sa.select(supplement_jobs.c.verified_through).where(
                supplement_jobs.c.job_id == job["job_id"]
            )
        ).first()
        swap_through = (
            min(through, int(swap_mark["verified_through"]))
            if swap_mark and swap_mark["verified_through"] is not None
            else job["start_block"] - 1
        )
        # 只读取既有窗口更新时间来识别分页快照变化，不扫描事件重新核验采集。
        stamps = []
        for table in (windows, swap_windows):
            row = self.rows(
                sa.select(sa.func.max(table.c.updated_at_utc).label("updated")).where(
                    table.c.job_id == job["job_id"], table.c.start_block <= through
                )
            ).one()
            stamps.append(str(row["updated"]))
        stamp = ":".join(
            (
                job["job_id"],
                str(through),
                str(swap_through),
                header["block_hash"],
                *stamps,
            )
        )
        note = f"只读正式窗口快照；采集水位 {mark}，本次使用已存区块时间对应边界 {through}；不是全链完整索引。"
        scope = DatasetScope(
            dataset_id=job["job_id"],
            chain_id=chain_id,
            token_address=job["token_address"],
            completed_through_block=str(through),
            data_cutoff_time=header["block_time_utc"].replace(tzinfo=UTC),
            scope_note=note,
        )
        return SourceSnapshot(
            job,
            scope,
            first["block_time_utc"].replace(tzinfo=UTC),
            through,
            swap_through,
            stamp,
        )

    def linked_transfers(
        self, source: SourceSnapshot, first: int | None = None, last: int | None = None
    ):
        """使用 EXISTS 避免窗口关联重复放大事件，仅接纳 done 且不超过连续水位的事实。"""
        first = (
            source.job["start_block"]
            if first is None
            else max(first, source.job["start_block"])
        )
        last = source.through if last is None else min(last, source.through)
        window_first = max(
            source.job["start_block"], first - source.job["window_size"] + 1
        )
        admitted = sa.exists(
            sa.select(1)
            .select_from(
                window_events.join(
                    windows,
                    sa.and_(
                        window_events.c.job_id == windows.c.job_id,
                        window_events.c.start_block == windows.c.start_block,
                    ),
                )
            )
            .where(
                window_events.c.event_id == transfers.c.event_id,
                window_events.c.job_id == source.job["job_id"],
                window_events.c.start_block.between(window_first, last),
                windows.c.status == "done",
                transfers.c.block_number.between(
                    windows.c.start_block, windows.c.end_block
                ),
            )
        )
        return sa.select(transfers).where(
            transfers.c.chain_id == source.scope.chain_id,
            transfers.c.token_address == source.scope.token_address,
            transfers.c.block_number.between(first, last),
            admitted,
        )

    def activity(
        self,
        source: SourceSnapshot,
        period,
        address: str | None,
        excluded: tuple[str, ...],
        limit: int,
    ) -> list[dict]:
        """按 [start,end) 读取有界 Transfer；历史与截止不满足时拒绝，不静默裁剪。"""
        if (
            period.start < source.first_time
            or period.end > source.scope.data_cutoff_time
        ):
            raise ResearchRuntimeError(
                "resource_unavailable",
                "所选时段超出当前有明确时间边界的已采集范围，请调整时段。",
            )
        # 利用已存缓存缩小区块范围；严格 <start，避免漏掉同一秒内的其他区块。
        lower = self.rows(
            sa.select(block_headers.c.block_number)
            .where(
                block_headers.c.chain_id == source.scope.chain_id,
                block_headers.c.block_number <= source.through,
                block_headers.c.block_time_utc < utc_naive(period.start),
            )
            .order_by(block_headers.c.block_number.desc())
            .limit(1)
        ).first()
        upper = self.rows(
            sa.select(block_headers.c.block_number)
            .where(
                block_headers.c.chain_id == source.scope.chain_id,
                block_headers.c.block_number <= source.through,
                block_headers.c.block_time_utc >= utc_naive(period.end),
            )
            .order_by(block_headers.c.block_number)
            .limit(1)
        ).first()
        statement = self.linked_transfers(
            source,
            int(lower["block_number"]) if lower else None,
            int(upper["block_number"]) if upper else None,
        ).where(
            transfers.c.block_time_utc >= utc_naive(period.start),
            transfers.c.block_time_utc < utc_naive(period.end),
        )
        if address:
            statement = statement.where(
                sa.or_(
                    transfers.c.from_address == address,
                    transfers.c.to_address == address,
                )
            )
        if excluded:
            statement = statement.where(
                transfers.c.from_address.not_in(excluded),
                transfers.c.to_address.not_in(excluded),
            )
        rows = list(
            self.rows(
                statement.order_by(
                    transfers.c.block_number, transfers.c.tx_hash, transfers.c.log_index
                ).limit(limit + 1)
            )
        )
        if len(rows) > limit:
            raise ResearchRuntimeError(
                "budget_exceeded",
                "所选时段事件量超过本次精确分析上限，请缩小时段或调整工具预算。",
            )
        return [dict(row) for row in rows]

    def lookup(
        self,
        source: SourceSnapshot,
        *,
        tx_hash: str | None,
        block_hash: str | None,
        after: tuple | None,
        size: int,
    ) -> LookupPage:
        """合并正式 Transfer 与已补齐 Swap，按稳定复合键做 keyset 分页，不用 OFFSET。"""
        # 先用已有哈希索引/区块缓存定位小范围，再检查正式窗口关系。
        # 定位信息不直接交付，也不把未入正式窗口的行计入结果。
        if tx_hash:
            locator = self.rows(
                sa.select(
                    sa.func.min(transfers.c.block_number).label("first"),
                    sa.func.max(transfers.c.block_number).label("last"),
                ).where(
                    transfers.c.chain_id == source.scope.chain_id,
                    transfers.c.token_address == source.scope.token_address,
                    transfers.c.tx_hash == tx_hash,
                )
            ).one()
            swap_locator = self.rows(
                sa.select(
                    sa.func.min(swaps.c.block_number).label("first"),
                    sa.func.max(swaps.c.block_number).label("last"),
                ).where(
                    swaps.c.job_id == source.job["job_id"], swaps.c.tx_hash == tx_hash
                )
            ).one()
            starts = [
                row["first"]
                for row in (locator, swap_locator)
                if row["first"] is not None
            ]
            ends = [
                row["last"]
                for row in (locator, swap_locator)
                if row["last"] is not None
            ]
            if not starts:
                return LookupPage([], None)
            first, last = min(starts), max(ends)
        else:
            header = self.rows(
                sa.select(block_headers.c.block_number)
                .where(
                    block_headers.c.chain_id == source.scope.chain_id,
                    block_headers.c.block_hash == block_hash,
                )
                .limit(1)
            ).first()
            if header is None:
                header = self.rows(
                    sa.select(transfers.c.block_number)
                    .where(
                        transfers.c.chain_id == source.scope.chain_id,
                        transfers.c.token_address == source.scope.token_address,
                        transfers.c.block_hash == block_hash,
                    )
                    .limit(1)
                ).first()
            if header is None:
                return LookupPage([], None)
            first = last = header["block_number"]
        t = self.linked_transfers(source, int(first), int(last)).subquery()
        transfer = sa.select(
            t.c.block_number,
            t.c.tx_hash,
            t.c.log_index,
            sa.literal("transfer").label("kind"),
            t.c.block_hash,
            t.c.from_address,
            t.c.to_address,
            t.c.amount_raw,
            t.c.token_decimals,
            sa.literal(None).label("pool_address"),
            sa.literal(None).label("trader"),
            sa.literal(None).label("side"),
            sa.literal(None).label("quote_token"),
            sa.literal(None).label("quote_amount_raw"),
            sa.literal(None).label("attribution"),
        )
        if tx_hash:
            transfer = transfer.where(t.c.tx_hash == tx_hash)
        if block_hash:
            transfer = transfer.where(t.c.block_hash == block_hash)
        h = block_headers
        admitted = sa.exists(
            sa.select(1)
            .select_from(swap_windows)
            .where(
                swap_windows.c.job_id == swaps.c.job_id,
                swap_windows.c.status == "done",
                swaps.c.block_number.between(
                    swap_windows.c.start_block, swap_windows.c.end_block
                ),
            )
        )
        swap = (
            sa.select(
                swaps.c.block_number,
                swaps.c.tx_hash,
                swaps.c.log_index,
                sa.literal("swap").label("kind"),
                sa.func.coalesce(h.c.block_hash, sa.literal(block_hash)).label(
                    "block_hash"
                )
                if block_hash
                else h.c.block_hash,
                sa.literal(None).label("from_address"),
                sa.literal(None).label("to_address"),
                swaps.c.token_amount_raw.label("amount_raw"),
                sa.literal(source.job["token_decimals"]).label("token_decimals"),
                swaps.c.pool_address,
                swaps.c.trader,
                swaps.c.side,
                swaps.c.quote_token,
                swaps.c.quote_amount_raw,
                swaps.c.attribution,
            )
            .select_from(
                swaps.outerjoin(
                    h,
                    sa.and_(
                        h.c.chain_id == swaps.c.chain_id,
                        h.c.block_number == swaps.c.block_number,
                    ),
                )
            )
            .where(
                swaps.c.job_id == source.job["job_id"],
                swaps.c.block_number.between(
                    source.job["start_block"], source.swap_through
                ),
                admitted,
            )
        )
        if tx_hash:
            swap = swap.where(swaps.c.tx_hash == tx_hash)
        if block_hash:
            swap = swap.where(swaps.c.block_number == first)
        combined = transfer.union_all(swap).subquery()
        keys = (
            combined.c.block_number,
            combined.c.tx_hash,
            combined.c.log_index,
            combined.c.kind,
        )
        statement = sa.select(combined)
        if after is not None:
            statement = statement.where(sa.tuple_(*keys) > after)
        return LookupPage(
            [dict(row) for row in self.rows(statement.order_by(*keys).limit(size + 1))],
            int(last),
        )
