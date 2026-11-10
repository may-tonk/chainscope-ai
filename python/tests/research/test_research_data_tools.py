"""四个工具的隔离数据库测试：不连接业务库，不迁移生产表，不调用模型。

SQLite 仅用于查询逻辑/事务/合同的确定性测试；真实 MySQL 冒烟另外记录。
"""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
import yaml
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import SecretStr

from chainscope import models
from chainscope_research.agent.tools.data_tools import create_data_tools
from chainscope_research.bootstrap import build_analysis_application
from chainscope_research.contracts.access import ToolGrant
from chainscope_research.contracts.periods import (
    AnalysisPeriods,
    ContextPeriod,
    TimePeriod,
)
from chainscope_research.contracts.requests import ExecutionContext
from chainscope_research.contracts.tools import (
    AnalyzeAddressActivityInput,
    AnalyzeTokenActivityInput,
    LookupBlockEventsInput,
    LookupTransactionEventsInput,
)
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.core.tool_settings import TOOL_IDS, load_analysis_settings
from chainscope_research.storage.repositories.chain_facts import ToolStopped

ROOT = Path(__file__).resolve().parents[3]
TOKEN, A, B, C, ZERO = (
    "0x" + character * 40 for character in ("1", "a", "b", "c", "0")
)
BASE = datetime(2026, 1, 1)


def block_hash(number):
    """生成明确标为测试的确定性哈希，不代表真实链上区块。"""
    return "0x" + f"{number:064x}"


def context(user="owner", revision=1):
    """构造虚构可信上下文；真实网站必须认证后生成，不能从请求体直接取得。"""
    return ExecutionContext(
        user_id=user,
        session_id="session",
        research_id="study",
        request_revision=revision,
        task_id="task",
        attempt_id="attempt",
        call_id="call",
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
        data_cutoff_time=BASE.replace(tzinfo=UTC) + timedelta(days=2),
    )


def periods():
    """两段等长历史范围，重点时段右端与缓存截止一致且排除端点事件。"""
    start = BASE.replace(tzinfo=UTC)
    return AnalysisPeriods(
        focus_period=TimePeriod(
            start=start + timedelta(days=1), end=start + timedelta(days=2)
        ),
        comparison_periods=(
            ContextPeriod(
                period=TimePeriod(start=start, end=start + timedelta(days=1)),
                reason="等长比较",
            ),
        ),
    )


class OwnerPermission:
    """测试服务端权限源；仅 owner 拥有 bem，不把 user_id 当成自带权限。"""

    def __init__(self):
        """独立记录每次解析，支持验证查询后撤销权限。"""
        self.calls = 0
        self.revoked = False
        self.revoke_after_read = False

    async def resolve(self, ctx):
        """按实际上下文身份返回授权；第二次可撤销，模拟查询期间权限变化。"""
        self.calls += 1
        allowed = (
            ctx.user_id == "owner"
            and not self.revoked
            and not (self.revoke_after_read and self.calls > 1)
        )
        return ToolGrant(
            user_id=ctx.user_id,
            allowed_job_ids=("bem",) if allowed else (),
            allowed_tools=tuple(sorted(TOOL_IDS)) if allowed else (),
        )


@pytest.fixture
def facts_engine(tmp_path):
    """创建临时事实表及小样例；只复现测试所需列，不宣称验证 MySQL 存储约束。"""
    metadata = sa.MetaData()
    tables = {}
    for original in (
        models.jobs,
        models.transfers,
        models.windows,
        models.window_events,
        models.block_headers,
        models.swaps,
        models.swap_windows,
        models.supplement_jobs,
    ):
        columns = []
        for column in original.columns:
            python_type = column.type.python_type
            storage_type = (
                sa.DateTime()
                if python_type is datetime
                else sa.Boolean()
                if python_type is bool
                else sa.Integer()
                if python_type is int
                else sa.String()
            )
            columns.append(
                sa.Column(
                    column.name,
                    storage_type,
                    primary_key=column.primary_key,
                    nullable=column.nullable,
                )
            )
        tables[original.name] = sa.Table(original.name, metadata, *columns)
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "facts.sqlite"))
    metadata.create_all(engine)

    def insert(connection, original, **values):
        """填充非空测试列的机械默认值；关键业务字段由每条测试数据显式给出。"""
        defaults = {}
        for column in original.columns:
            if not column.nullable:
                kind = column.type.python_type
                defaults[column.name] = (
                    BASE
                    if kind is datetime
                    else False
                    if kind is bool
                    else 0
                    if kind is int
                    else ""
                )
        connection.execute(tables[original.name].insert(), {**defaults, **values})

    with engine.begin() as connection:
        insert(
            connection,
            models.jobs,
            job_id="bem",
            chain_id=56,
            token_address=TOKEN,
            token_decimals=8,
            source="test",
            start_block=1,
            end_block=205,
            window_size=205,
            verified_through=205,
            status="running",
        )
        for number, timestamp in (
            (1, BASE),
            (100, BASE + timedelta(days=1)),
            (120, BASE + timedelta(days=1, hours=2)),
            (200, BASE + timedelta(days=2)),
        ):
            insert(
                connection,
                models.block_headers,
                chain_id=56,
                block_number=number,
                block_hash=block_hash(number),
                block_time_utc=timestamp,
            )
        insert(
            connection,
            models.windows,
            job_id="bem",
            start_block=1,
            end_block=205,
            status="done",
            updated_at_utc=BASE,
        )
        insert(
            connection,
            models.swap_windows,
            job_id="bem",
            start_block=1,
            end_block=205,
            status="done",
            updated_at_utc=BASE,
        )
        insert(
            connection,
            models.supplement_jobs,
            job_id="bem",
            verified_through=205,
            updated_at_utc=BASE,
        )
        events = [
            (1, 10, 1, 0, A, B, "10", 0),
            (2, 10, 1, 1, A, B, "20", 0),
            (3, 110, 2, 0, ZERO, A, "100", 1),
            (4, 120, 3, 0, A, B, "40", 1),
            (5, 120, 3, 1, B, A, "10", 1),
            (6, 130, 4, 0, A, A, "7", 1),
            (7, 140, 5, 0, A, ZERO, "5", 1),
            (8, 150, 6, 0, B, C, "0", 1),
            (9, 160, 7, 0, A, B, "999", 1),
            (10, 155, 8, 0, ZERO, ZERO, "3", 1),
            (11, 200, 9, 0, A, B, "888", 2),
        ]
        for event_id, number, transaction, log, sender, receiver, amount, day in events:
            insert(
                connection,
                models.transfers,
                event_id=event_id,
                chain_id=56,
                token_address=TOKEN,
                block_number=number,
                block_hash=block_hash(number),
                block_time_utc=BASE + timedelta(days=day, hours=0 if day == 2 else 2),
                tx_hash=block_hash(transaction),
                log_index=log,
                from_address=sender,
                to_address=receiver,
                amount_raw=amount,
                token_decimals=8,
                amount_len=len(amount),
            )
            if event_id != 9:
                insert(
                    connection,
                    models.window_events,
                    job_id="bem",
                    start_block=1,
                    event_id=event_id,
                )
        insert(
            connection,
            models.swaps,
            swap_id=1,
            job_id="bem",
            chain_id=56,
            pool_address=C,
            block_number=120,
            block_time_utc=BASE + timedelta(days=1, hours=2),
            tx_hash=block_hash(3),
            log_index=2,
            tx_from=A,
            trader=A,
            side="sell",
            token_amount_raw="40",
            quote_amount_raw="4",
            quote_token=B,
            attribution="test",
            transfer_matched=True,
        )
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def service(facts_engine):
    """注入隔离 Engine 和真实形状的权限端口，测试后等待线程退出。"""
    app = build_analysis_application(
        ROOT, OwnerPermission(), SecretStr("test-key-" * 8), engine=facts_engine
    )
    yield app
    await app.aclose()


def metric_map(data, index=0):
    """取精确字符串指标用于独立核对，不经过浮点数或格式化显示值。"""
    return {
        item.metric_id: item.value if item.kind == "count" else item.value_raw
        for item in data.slices[index].metrics
    }


@pytest.mark.asyncio
async def test_token_classification_counts_and_comparison(service):
    """分别核对日志、交易、普通转账、发行、自转账；原始未入窗口事件与右端不计。"""
    args = AnalyzeTokenActivityInput(
        chain_id=56, token_address=TOKEN, periods=periods()
    )
    result = await service.execute("analyze_token_activity", args, context())
    assert result.outcome.status == "success", result.outcome
    values = metric_map(result.outcome.data)
    assert values["transfer_event_count"] == "7" and values["transaction_count"] == "6"
    assert (
        values["transfer_amount_raw"] == "165" and values["ordinary_amount_raw"] == "50"
    )
    assert values["mint_amount_raw"] == "100" and values["burn_amount_raw"] == "5"
    assert values["self_amount_raw"] == "7" and values["unknown_zero_amount_raw"] == "3"
    assert (
        values["zero_amount_event_count"] == "1"
        and values["active_address_count"] == "3"
    )
    change = next(
        item
        for item in result.outcome.data.changes
        if item.metric_id == "ordinary_amount_raw"
    )
    assert (change.delta, change.relative_numerator, change.relative_denominator) == (
        "20",
        "20",
        "30",
    )
    assert result.metadata.sources[0].completed_through_block == "200"


@pytest.mark.asyncio
async def test_address_flow_and_exclusion(service):
    """净转账流量排除铸造、销毁与自转账，不冒充余额；筛选按两端执行。"""
    args = AnalyzeAddressActivityInput(
        chain_id=56, token_address=TOKEN, address=A, periods=periods()
    )
    result = await service.execute("analyze_address_activity", args, context())
    assert result.outcome.status == "success", result.outcome
    values = metric_map(result.outcome.data)
    assert (
        values["inflow_raw"] == "10"
        and values["outflow_raw"] == "40"
        and values["net_transfer_flow_raw"] == "-30"
    )
    assert {entry.address for entry in result.outcome.data.slices[0].top_addresses} == {
        B
    }
    excluded = args.model_copy(update={"exclude_addresses": (B,)})
    result = await service.execute("analyze_address_activity", excluded, context())
    assert metric_map(result.outcome.data)["ordinary_amount_raw"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["lookup_transaction_events", "lookup_block_events"])
async def test_hash_lookup_includes_transfer_and_swap(service, tool):
    """两类哈希查询都有真实明细与去重后的事件引用，不强制时间参数。"""
    args = (
        LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3))
        if tool == "lookup_transaction_events"
        else LookupBlockEventsInput(chain_id=56, block_hash=block_hash(120))
    )
    result = await service.execute(tool, args, context())
    assert result.outcome.status == "success", result.outcome
    data = result.outcome.data
    assert len(data.events) == 3 and len(data.transfers) == 2 and len(data.swaps) == 1
    assert data.swaps[0].token_amount_raw == "40" and data.swaps[0].side == "sell"


@pytest.mark.asyncio
async def test_cursor_pages_and_cannot_cross_user_filters_or_revision(service):
    """稳定续页不重不漏，篡改与改条件/修订不能复用旧令牌。"""
    args = LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3), page_size=1)
    seen, cursor = [], None
    for _ in range(3):
        response = await service.execute(
            "lookup_transaction_events",
            args.model_copy(update={"cursor": cursor}),
            context(),
        )
        assert response.outcome.status == "success", response.outcome
        data = response.outcome.data
        seen += [(ref.log_index, ref.event_kind) for ref in data.events]
        cursor = data.next_cursor
    assert len(set(seen)) == 3 and cursor is None
    first = await service.execute("lookup_transaction_events", args, context())
    token = first.outcome.data.next_cursor
    for changed in (
        args.model_copy(update={"cursor": token + "x"}),
        args.model_copy(update={"cursor": token, "tx_hash": block_hash(4)}),
    ):
        response = await service.execute(
            "lookup_transaction_events", changed, context()
        )
        assert (
            response.outcome.status == "failed"
            and response.outcome.error.code == "invalid_input"
        )
    response = await service.execute(
        "lookup_transaction_events",
        args.model_copy(update={"cursor": token}),
        context(revision=2),
    )
    assert response.outcome.status == "failed"


@pytest.mark.asyncio
async def test_permission_before_and_after_query(service):
    """无权用户不运行事实查询；执行期间撤权也不交付已查到的私人结果。"""
    args = LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3))
    denied = await service.execute("lookup_transaction_events", args, context("other"))
    assert (
        denied.outcome.status == "failed"
        and denied.outcome.error.code == "permission_denied"
    )
    service.authorizer.calls = 0
    service.authorizer.revoke_after_read = True
    revoked = await service.execute("lookup_transaction_events", args, context())
    assert (
        revoked.outcome.status == "failed"
        and revoked.outcome.error.code == "permission_denied"
    )


@pytest.mark.asyncio
async def test_langchain_tools_validate_json_dates_and_reject_identity_fields(service):
    """实际 StructuredTool.ainvoke 接受 JSON 日期/数组，未知身份字段仍被拦截。"""
    tools = {
        tool.name: tool
        for tool in await create_data_tools(
            service, load_prompt_registry(ROOT), context()
        )
    }
    assert set(tools) == TOOL_IDS
    # 验证供应商绑定前的 LangChain 转换结果，不只检查手工拼出的工具元数据。
    provider_definition = convert_to_openai_tool(tools["analyze_token_activity"])
    parameters = provider_definition["function"]["parameters"]
    assert set(parameters["required"]) == {"chain_id", "token_address", "periods"}
    assert parameters["additionalProperties"] is False
    assert "user_id" not in parameters["properties"]
    payload = {
        "chain_id": 56,
        "token_address": TOKEN,
        "periods": periods().model_dump(mode="json"),
        "exclude_addresses": [],
    }
    result = await tools["analyze_token_activity"].ainvoke(payload)
    assert result["outcome"]["status"] == "success", result
    invalid = await tools["analyze_token_activity"].ainvoke(
        {**payload, "user_id": "other"}
    )
    assert (
        invalid["outcome"]["status"] == "failed"
        and invalid["outcome"]["error"]["code"] == "invalid_input"
    )
    assert (
        await create_data_tools(service, load_prompt_registry(ROOT), context("other"))
        == []
    )


@pytest.mark.asyncio
async def test_zero_results_and_outside_ranges_are_not_failures_in_disguise(service):
    """已有范围没有目标事件是真实空结果；时间超界明确不支持，不裁剪成零。"""
    empty = await service.execute(
        "lookup_transaction_events",
        LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(999)),
        context(),
    )
    assert empty.outcome.status == "success" and empty.outcome.data.events == ()
    args = AnalyzeTokenActivityInput(
        chain_id=56,
        token_address=TOKEN,
        periods=AnalysisPeriods(
            focus_period=TimePeriod(
                start=BASE.replace(tzinfo=UTC),
                end=(BASE + timedelta(days=3)).replace(tzinfo=UTC),
            )
        ),
    )
    response = await service.execute("analyze_token_activity", args, context())
    assert response.outcome.status == "unsupported"


@pytest.mark.asyncio
async def test_budget_and_expired_context_are_explicit(service):
    """超过行数预算不输出部分合计冒充完整结果；过期请求不再派发。"""
    service.settings = service.settings.model_copy(update={"max_analysis_rows": 1})
    args = AnalyzeTokenActivityInput(
        chain_id=56, token_address=TOKEN, periods=periods()
    )
    response = await service.execute("analyze_token_activity", args, context())
    assert (
        response.outcome.status == "failed"
        and response.outcome.error.code == "budget_exceeded"
    )
    expired = context().model_copy(
        update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    response = await service.execute("analyze_token_activity", args, expired)
    assert (
        response.outcome.status == "failed" and response.outcome.error.code == "timeout"
    )


@pytest.mark.asyncio
async def test_precise_uint256_sum_and_bad_source_amount(service, facts_engine):
    """真实查询路径累计完整 uint256 后仍精确；坏原值会失败而不是忽略。"""
    maximum = str(2**256 - 1)
    with facts_engine.begin() as connection:
        connection.execute(
            models.transfers.update()
            .where(models.transfers.c.event_id == 4)
            .values(amount_raw=maximum)
        )
    response = await service.execute(
        "analyze_token_activity",
        AnalyzeTokenActivityInput(chain_id=56, token_address=TOKEN, periods=periods()),
        context(),
    )
    assert metric_map(response.outcome.data)["transfer_amount_raw"] == str(
        int(maximum) + 125
    )


@pytest.mark.asyncio
async def test_cooperative_stop_and_database_readonly(service, facts_engine):
    """预先停止不查询，事实事务本身也拒绝 DML；测试只在临时 SQLite 中操作。"""
    stopped = threading.Event()
    stopped.set()
    result = await service.execute(
        "lookup_transaction_events",
        LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3)),
        context(),
        stop=stopped,
    )
    assert result.outcome.status == "cancelled"
    with pytest.raises(sa.exc.OperationalError):
        service.repository.run(
            lambda view: view.connection.exec_driver_sql("DELETE FROM transfer_events"),
            threading.Event(),
            time.monotonic() + 5,
            5000,
        )


@pytest.mark.asyncio
async def test_failed_database_is_not_returned_as_empty(service):
    """基础设施异常只能失败，不生成 events=[] 的成功结果。"""

    def broken(*arguments):
        """注入驱动异常，不改变任何业务数据库。"""
        raise sa.exc.OperationalError(
            "test", {}, RuntimeError("private connection details")
        )

    service.repository.run = broken
    result = await service.execute(
        "lookup_transaction_events",
        LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3)),
        context(),
    )
    assert (
        result.outcome.status == "failed" and "private" not in result.model_dump_json()
    )


def change_fixture(engine, statement):
    """单独写连接修改临时样例，避免复用已进入 query_only 模式的测试读连接。"""
    writer = sa.create_engine(engine.url)
    try:
        with writer.begin() as connection:
            connection.execute(statement)
    finally:
        writer.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["-1", "01", "1e8", str(2**256)])
@pytest.mark.parametrize(
    "tool", ["analyze_token_activity", "lookup_transaction_events"]
)
async def test_malformed_source_amount_fails_explicitly(
    service, facts_engine, raw, tool
):
    """非法原值及超 uint256 单条金额均使分析失败，不跳过坏行伪装完整统计。"""
    change_fixture(
        facts_engine,
        models.transfers.update()
        .where(models.transfers.c.event_id == 4)
        .values(amount_raw=raw),
    )
    args = (
        AnalyzeTokenActivityInput(chain_id=56, token_address=TOKEN, periods=periods())
        if tool == "analyze_token_activity"
        else LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3))
    )
    result = await service.execute(tool, args, context())
    assert result.outcome.status == "failed"
    assert result.outcome.error.code == "dependency_unavailable"


@pytest.mark.asyncio
async def test_swap_gap_is_partial_and_missing_hash_remains_unknown(
    service, facts_engine
):
    """独立补采落后时不承诺全量事件；缺少缓存的 Swap 哈希保持 null。"""
    change_fixture(
        facts_engine,
        models.block_headers.delete().where(models.block_headers.c.block_number == 120),
    )
    args = LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3))
    result = await service.execute("lookup_transaction_events", args, context())
    assert result.outcome.data.swaps[0].evidence.block_hash is None
    block = await service.execute(
        "lookup_block_events",
        LookupBlockEventsInput(chain_id=56, block_hash=block_hash(120)),
        context(),
    )
    assert len(block.outcome.data.events) == 3
    assert block.outcome.data.swaps[0].evidence.block_hash == block_hash(120)
    change_fixture(
        facts_engine, models.supplement_jobs.update().values(verified_through=100)
    )
    partial = await service.execute("lookup_transaction_events", args, context())
    assert partial.outcome.status == "partial"
    assert len(partial.outcome.data.transfers) == 2 and partial.outcome.data.swaps == ()
    assert "Swap" in partial.outcome.limitations[0]


@pytest.mark.asyncio
async def test_old_target_within_swap_watermark_is_not_reported_partial(
    service, facts_engine
):
    """较新的补采尚未完成，不影响已在独立水位以内的旧交易结果完整性。"""
    change_fixture(
        facts_engine, models.supplement_jobs.update().values(verified_through=100)
    )
    result = await service.execute(
        "lookup_transaction_events",
        LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(1)),
        context(),
    )
    assert (
        result.outcome.status == "success" and len(result.outcome.data.transfers) == 2
    )


@pytest.mark.asyncio
async def test_cursor_rejects_changed_window_snapshot(service, facts_engine):
    """正式窗口更新后旧游标失效，避免跨两个数据库快照承诺不重不漏。"""
    args = LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3), page_size=1)
    first = await service.execute("lookup_transaction_events", args, context())
    change_fixture(
        facts_engine,
        models.windows.update().values(updated_at_utc=BASE + timedelta(minutes=1)),
    )
    result = await service.execute(
        "lookup_transaction_events",
        args.model_copy(update={"cursor": first.outcome.data.next_cursor}),
        context(),
    )
    assert (
        result.outcome.status == "failed"
        and result.outcome.error.code == "invalid_input"
    )


@pytest.mark.asyncio
async def test_sampled_evidence_and_unequal_periods(service):
    """证据截取不改变完整合计；不等长比较保留差值但不输出增长率。"""
    service.settings = service.settings.model_copy(update={"evidence_limit": 1})
    span = periods()
    shorter = span.comparison_periods[0].model_copy(
        update={
            "period": TimePeriod(
                start=BASE.replace(tzinfo=UTC),
                end=(BASE + timedelta(hours=12)).replace(tzinfo=UTC),
            )
        }
    )
    span = span.model_copy(update={"comparison_periods": (shorter,)})
    result = await service.execute(
        "analyze_token_activity",
        AnalyzeTokenActivityInput(chain_id=56, token_address=TOKEN, periods=span),
        context(),
    )
    assert metric_map(result.outcome.data)["transfer_event_count"] == "7"
    assert len(result.outcome.data.slices[0].evidence) == 1
    assert result.outcome.data.slices[0].evidence_is_sampled
    assert all(
        change.relative_denominator is None for change in result.outcome.data.changes
    )


@pytest.mark.asyncio
async def test_timeout_retains_slot_until_worker_really_stops(service):
    """等待超时后线程仍占资源，直到真正退出才归还，不因请求超时无限扩容。"""
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def delayed(operation, stop, deadline, timeout):
        """模拟暂时无法中断的驱动，收尾时观察协作停止信号；不操作任何数据库。"""
        entered.set()
        release.wait(2)
        if stop.is_set():
            stopped.set()
        raise ToolStopped()

    service.repository.run = delayed
    capacity = service.settings.max_parallel_calls
    ctx = context().model_copy(
        update={"deadline_at": datetime.now(UTC) + timedelta(milliseconds=100)}
    )
    args = LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3))
    try:
        result = await service.execute("lookup_transaction_events", args, ctx)
        assert entered.is_set() and result.outcome.error.code == "timeout"
        assert service._slots._value == capacity - 1
    finally:
        release.set()
    for _ in range(100):
        if service._slots._value == capacity:
            break
        await asyncio.sleep(0.01)
    assert service._slots._value == capacity and stopped.is_set()


@pytest.mark.asyncio
async def test_coroutine_cancellation_propagates_and_signals_worker(service):
    """上层取消继续传播，线程另收停止信号；不返回伪造的成功或停止确认。"""
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def delayed(operation, stop, deadline, timeout):
        """隔离模拟驱动阻塞，只用于验证 asyncio 与线程的取消边界。"""
        entered.set()
        release.wait(2)
        if stop.is_set():
            stopped.set()
        raise ToolStopped()

    service.repository.run = delayed
    task = asyncio.create_task(
        service.execute(
            "lookup_transaction_events",
            LookupTransactionEventsInput(chain_id=56, tx_hash=block_hash(3)),
            context(),
        )
    )
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    for _ in range(100):
        if stopped.is_set():
            break
        await asyncio.sleep(0.01)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_parallel_calls_keep_their_own_context_and_address(service):
    """并行请求各自保留地址、修订和调用标识，不能共享可变结果或线程连接。"""
    a, b = await asyncio.gather(
        *(
            service.execute(
                "analyze_address_activity",
                AnalyzeAddressActivityInput(
                    chain_id=56, token_address=TOKEN, address=address, periods=periods()
                ),
                context(revision=revision).model_copy(update={"call_id": call_id}),
            )
            for address, revision, call_id in ((A, 1, "call-a"), (B, 2, "call-b"))
        )
    )
    assert a.outcome.data.address == A and b.outcome.data.address == B
    assert a.call.call_id == "call-a" and b.call.call_id == "call-b"
    assert a.call.request_revision == 1 and b.call.request_revision == 2
    assert metric_map(a.outcome.data)["net_transfer_flow_raw"] == "-30"
    assert metric_map(b.outcome.data)["net_transfer_flow_raw"] == "30"


@pytest.mark.asyncio
async def test_tool_yaml_is_used_by_real_builder(tmp_path, facts_engine):
    """从临时 YAML 修改预算和启用状态，经真实装配后检验调用行为，避免写死配置。"""
    settings = yaml.safe_load(
        (ROOT / "config/research_tools.yaml").read_text(encoding="utf-8")
    )
    settings.update(
        max_parallel_calls=3, max_analysis_rows=1, evidence_limit=2, top_addresses=2
    )
    settings["tools"]["lookup_block_events"]["enabled"] = False
    directory = tmp_path / "config"
    directory.mkdir()
    path = directory / "research_tools.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    app = build_analysis_application(
        tmp_path, OwnerPermission(), SecretStr("test-key-" * 8), engine=facts_engine
    )
    try:
        assert app.settings.max_parallel_calls == 3 and app.settings.evidence_limit == 2
        assert app.settings.top_addresses == 2
        tools = await create_data_tools(app, load_prompt_registry(ROOT), context())
        assert "lookup_block_events" not in {tool.name for tool in tools}
        result = await app.execute(
            "lookup_block_events",
            LookupBlockEventsInput(chain_id=56, block_hash=block_hash(120)),
            context(),
        )
        assert result.outcome.status == "unsupported"
        budget = await app.execute(
            "analyze_token_activity",
            AnalyzeTokenActivityInput(
                chain_id=56, token_address=TOKEN, periods=periods()
            ),
            context(),
        )
        assert budget.outcome.error.code == "budget_exceeded"
    finally:
        await app.aclose()
    del settings["tools"]["lookup_block_events"]
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with pytest.raises(ResearchRuntimeError):
        load_analysis_settings(tmp_path)
