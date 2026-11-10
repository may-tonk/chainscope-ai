"""四类工具的异步业务入口：授权、预算、只读事务、计算与安全结果统一处理。

不接受任意 SQL，不默认授予权限。同步驱动及精确计算在受限线程执行；等待超时
不谎报数据库已经停止，执行资源仅在真实工作结束后归还。
"""

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from chainscope_research.analysis.cleaning.transfer_rules import (
    METHOD_VERSION,
    classify_transfer,
    parse_uint256,
)
from chainscope_research.analysis.metrics.activity import (
    calculate_slice,
    compare_slices,
    event_reference,
)
from chainscope_research.contracts.access import ToolAuthorizer
from chainscope_research.contracts.common import CallIdentity
from chainscope_research.contracts.results import ResultMetadata, SafeError, ToolResult
from chainscope_research.contracts.tools import (
    AddressActivityData,
    AnalyzeAddressActivityInput,
    EventLookupData,
    LookupTransactionEventsInput,
    SwapDetail,
    TokenActivityData,
    TransferDetail,
)
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.tool_settings import AnalysisSettings
from chainscope_research.services.event_cursor import EventCursor
from chainscope_research.storage.repositories.chain_facts import (
    ReadonlyFacts,
    ToolStopped,
)


class AnalysisToolService:
    """执行四个已定义能力；无会话记忆、无研究队列、无自动重试。"""

    def __init__(
        self,
        repository: ReadonlyFacts,
        authorizer: ToolAuthorizer,
        settings: AnalysisSettings,
        cursor: EventCursor,
        *,
        owns_engine=False,
    ):
        """注入真实授权与数据资源；没有默认允许所有用户的构造路径。"""
        self.repository = repository
        self.authorizer = authorizer
        self.settings = settings
        self.cursor = cursor
        self.owns_engine = owns_engine
        self._pool = ThreadPoolExecutor(
            max_workers=settings.max_parallel_calls,
            thread_name_prefix="chainscope-analysis",
        )
        self._slots = asyncio.Semaphore(settings.max_parallel_calls)
        self._closed = False

    async def aclose(self) -> None:
        """等待线程真实退出后释放资源，不阻塞事件循环；由资源装配方在关闭时调用。"""
        self._closed = True
        await asyncio.to_thread(self._pool.shutdown, wait=True, cancel_futures=True)
        if self.owns_engine:
            await asyncio.to_thread(self.repository.engine.dispose)

    async def permission(self, context, tool_id):
        """每次按可信 user_id 获取当前授权，并核对身份、工具能力和数据集范围。"""
        grant = await self.authorizer.resolve(context)
        if (
            grant.user_id != context.user_id
            or tool_id not in grant.allowed_tools
            or not grant.allowed_job_ids
        ):
            raise ResearchRuntimeError(
                "permission_denied", "当前用户无权执行该工具或访问相应数据。"
            )
        return grant

    async def execute(
        self, tool_id, arguments, context, *, stop: Event | None = None
    ) -> ToolResult:
        """执行一个类型化请求，返回分状态合同；外部协程取消继续向上层传播。

        stop 是后台调用方持有的协作停止信号，不是模型参数。只有线程已检查并
        退出后才返回 cancelled；超时只返回失败，不承诺撤销已发出的 SQL。
        """
        from chainscope_research.agent.tools.catalog import TOOL_DEFINITIONS

        definition = next(
            (item for item in TOOL_DEFINITIONS if item.tool_id == tool_id), None
        )
        if definition is None:
            raise ResearchRuntimeError("invalid_input", "未登记的工具名称。")
        call = CallIdentity.model_validate(
            context.model_dump(include=set(CallIdentity.model_fields))
        )

        def result(outcome, metadata=None):
            """将业务结果绑定本次服务端调用标识，错误中不包含原始 SQL 或凭证。"""
            return definition.output_model(
                tool_id=tool_id, call=call, outcome=outcome, metadata=metadata
            )

        signal = stop if stop is not None else Event()
        try:
            if self._closed:
                raise ResearchRuntimeError(
                    "resource_unavailable", "工具运行资源已关闭。"
                )
            if not isinstance(arguments, definition.input_model):
                raise ResearchRuntimeError(
                    "invalid_input", "工具输入不符合对应 Pydantic 合同。"
                )
            policy = self.settings.tools[tool_id]
            if not policy.enabled:
                raise ResearchRuntimeError(
                    "resource_unavailable", "当前工具已在服务端禁用。"
                )
            remaining = min(
                policy.timeout_seconds,
                (context.deadline_at - datetime.now(UTC)).total_seconds(),
            )
            if remaining <= 0:
                raise ResearchRuntimeError("timeout", "本次调用已超过执行期限。")
            deadline = time.monotonic() + remaining
            async with asyncio.timeout(remaining):
                grant = await self.permission(context, tool_id)
                if signal.is_set():
                    return result(
                        {"status": "cancelled", "reason": "本次工具执行已停止。"}
                    )
                await self._slots.acquire()
                loop = asyncio.get_running_loop()
                try:
                    # 等待槽位期间也可能关闭服务；未成功派发时由当前协程归还槽位。
                    if self._closed:
                        raise ResearchRuntimeError(
                            "resource_unavailable", "工具运行资源已关闭。"
                        )
                    future = loop.run_in_executor(
                        self._pool,
                        self.repository.run,
                        lambda view: self._work(
                            view, tool_id, arguments, context, grant.allowed_job_ids
                        ),
                        signal,
                        deadline,
                        max(1, int((deadline - time.monotonic()) * 1000)),
                    )
                except BaseException:
                    self._slots.release()
                    raise

                def release(completed):
                    """仅真实工作结束后归还槽位，取走晚到异常，避免超时后无限堆积线程。"""
                    self._slots.release()
                    if not completed.cancelled():
                        completed.exception()

                future.add_done_callback(release)
                try:
                    data, metadata, limitations = await asyncio.shield(future)
                finally:
                    if not future.done():
                        signal.set()
                # 权限可能在工作期间被撤销；发布前再次核对，不因查到结果就直接交付。
                latest = await self.permission(context, tool_id)
                if metadata.sources[0].dataset_id not in latest.allowed_job_ids:
                    raise ResearchRuntimeError(
                        "permission_denied", "执行期间数据集访问权限已改变。"
                    )
                if signal.is_set():
                    return result(
                        {"status": "cancelled", "reason": "本次工具执行已停止。"}
                    )
                outcome = (
                    {"status": "partial", "data": data, "limitations": limitations}
                    if limitations
                    else {"status": "success", "data": data}
                )
                return result(outcome, metadata)
        except asyncio.CancelledError:
            signal.set()
            raise
        except ToolStopped:
            return result(
                {"status": "cancelled", "reason": "执行方已确认本次工具停止。"}
            )
        except TimeoutError:
            signal.set()
            return result(
                {
                    "status": "failed",
                    "error": SafeError(
                        code="timeout",
                        public_message="工具等待超时，尚未返回完整结果；数据库查询可能仍在收尾。",
                    ),
                }
            )
        except ResearchRuntimeError as error:
            if error.code == "resource_unavailable":
                return result({"status": "unsupported", "reason": error.public_message})
            safe = SafeError.model_validate(
                {"code": error.code, "public_message": error.public_message}
            )
            return result({"status": "failed", "error": safe})
        except SQLAlchemyError:
            return result(
                {
                    "status": "failed",
                    "error": SafeError(
                        code="dependency_unavailable",
                        public_message="事实数据库查询失败，未将失败转换为零结果。",
                    ),
                }
            )
        except Exception:  # noqa: BLE001 -- 工具公开边界不得把内部异常回传模型或用户。
            return result(
                {
                    "status": "failed",
                    "error": SafeError(
                        code="internal_error",
                        public_message="工具内部执行失败，未产生可交付结果。",
                    ),
                }
            )

    def _work(self, view, tool_id, arguments, context, job_ids):
        """线程内完成查询及精确计算，返回完整结果或明确错误，不在事件循环算大整数。"""
        source = view.source(
            job_ids,
            arguments.chain_id,
            arguments.token_address,
            context.data_cutoff_time,
        )
        if tool_id.startswith("lookup_"):
            data, limitations = self._lookup(view, source, arguments, context, tool_id)
        else:
            data, limitations = self._activity(view, source, arguments)
        view.check()
        metadata = ResultMetadata(
            result_id=str(uuid4()),
            generated_at=datetime.now(UTC),
            method_version=METHOD_VERSION,
            sources=(source.scope,),
        )
        return data, metadata, limitations

    def _lookup(self, view, source, arguments, context, tool_id):
        """获取一页正式事件及可核对明细；游标绑定用户、修订、筛选和快照。"""
        payload = arguments.model_dump(mode="json", exclude={"cursor"})
        binding = hashlib.sha256(
            json.dumps(
                [
                    context.user_id,
                    context.research_id,
                    context.request_revision,
                    tool_id,
                    payload,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        after = (
            self.cursor.parse(arguments.cursor, binding, source.stamp)
            if arguments.cursor
            else None
        )
        is_transaction = isinstance(arguments, LookupTransactionEventsInput)
        page = view.lookup(
            source,
            tx_hash=arguments.tx_hash.lower() if is_transaction else None,
            block_hash=None if is_transaction else arguments.block_hash.lower(),
            after=after,
            size=arguments.page_size,
        )
        rows = page.rows
        more = len(rows) > arguments.page_size
        rows = rows[: arguments.page_size]
        refs, transfer_details, swap_details = [], [], []
        for row in rows:
            reference = event_reference(row, arguments.chain_id, row["kind"])
            refs.append(reference)
            if row["kind"] == "transfer":
                classify_transfer(row, source.job["token_decimals"])
                transfer_details.append(
                    TransferDetail(
                        evidence=reference,
                        token_address=source.scope.token_address,
                        from_address=row["from_address"],
                        to_address=row["to_address"],
                        amount_raw=row["amount_raw"],
                        decimals=row["token_decimals"],
                    )
                )
            else:
                parse_uint256(row["amount_raw"])
                parse_uint256(row["quote_amount_raw"])
                swap_details.append(
                    SwapDetail.model_validate(
                        {
                            "evidence": reference,
                            "pool_address": row["pool_address"],
                            "trader": row["trader"],
                            "side": row["side"],
                            "token_amount_raw": row["amount_raw"],
                            "quote_token": row["quote_token"],
                            "quote_amount_raw": row["quote_amount_raw"],
                            "attribution": row["attribution"],
                        }
                    )
                )
        cursor = None
        if more:
            last = rows[-1]
            cursor = self.cursor.issue(
                binding,
                source.stamp,
                (
                    last["block_number"],
                    last["tx_hash"],
                    last["log_index"],
                    last["kind"],
                ),
                min(
                    int(context.deadline_at.timestamp()),
                    int(time.time()) + self.settings.cursor_ttl_seconds,
                ),
            )
        # 只对目标可能涉及的区块判断缺口；旧交易已在补采范围内时不误报 partial。
        required_through = min(
            source.through,
            page.target_last_block
            if page.target_last_block is not None
            else source.through,
        )
        limitations = (
            (
                f"Swap 独立采集仅完成至区块 {source.swap_through}，不能认定已掌握目标交易或区块的全部事件。",
            )
            if source.swap_through < required_through
            else ()
        )
        return EventLookupData(
            events=tuple(refs),
            next_cursor=cursor,
            transfers=tuple(transfer_details),
            swaps=tuple(swap_details),
        ), limitations

    def _activity(self, view, source, arguments):
        """围绕重点范围逐个计算比较和背景；超出预算或覆盖时不裁剪成假完整结果。"""
        periods = (
            [("focus", arguments.periods.focus_period)]
            + [
                ("comparison", item.period)
                for item in arguments.periods.comparison_periods
            ]
            + [("context", item.period) for item in arguments.periods.context_periods]
        )
        if len(periods) > self.settings.max_periods:
            raise ResearchRuntimeError(
                "budget_exceeded", "本次分析时段数量超过工具预算。"
            )
        address = (
            arguments.address.lower()
            if isinstance(arguments, AnalyzeAddressActivityInput)
            else None
        )
        slices = []
        for role, period in periods:
            rows = view.activity(
                source,
                period,
                address,
                tuple(item.lower() for item in arguments.exclude_addresses),
                self.settings.max_analysis_rows,
            )
            slices.append(
                calculate_slice(
                    rows, source, period, role, address, self.settings, view.check
                )
            )
        changes = compare_slices(slices)
        focus = slices[0]
        counts = {
            metric.metric_id: metric.value
            for metric in focus.metrics
            if metric.kind == "count"
        }
        observations = [
            f"重点范围内有 {counts['transfer_event_count']} 条 Transfer 日志、{counts['transaction_count']} 笔不同交易；普通转账 {counts['ordinary_event_count']} 条。",
            "统计为已采集代币的转账行为，不能直接解释价格变化、买卖或地址真实身份；地址净转账流不是余额。",
        ]
        if any(item.evidence_is_sampled for item in slices):
            observations.append(
                "事件引用仅为每个范围头部的有界样例，贡献排名和指标基于完整的本次预算内查询结果。"
            )
        if arguments.exclude_addresses:
            observations.append(
                "已排除任一端命中 exclude_addresses 的事件；数值仅适用于筛选后的样本总体。"
            )
        data = {
            "chain_id": arguments.chain_id,
            "token_address": source.scope.token_address,
            "slices": tuple(slices),
            "changes": changes,
            "observations": tuple(observations),
        }
        if address:
            return AddressActivityData(**data, address=address), ()
        return TokenActivityData(**data), ()
