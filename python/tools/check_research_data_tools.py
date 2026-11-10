r"""在既有 MySQL 上只读验收四个研究工具，并以独立查询核对精确计数和金额。

运行示例：.venv\Scripts\python.exe python/tools/check_research_data_tools.py --job-id bem --hours 1
本入口是持有本地数据库权限的操作者验收，不代表网站账户鉴权；不调用模型或 RPC，
不迁移、修改、重采业务数据，不自动保存报告。输出仅写 stdout，供人工检查。
"""

import argparse
import asyncio
import json
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from langsmith import tracing_context
from pydantic import SecretStr

from chainscope import models
from chainscope_research.agent.tools.data_tools import create_data_tools
from chainscope_research.bootstrap import build_analysis_application
from chainscope_research.contracts.access import ToolGrant
from chainscope_research.contracts.requests import ExecutionContext
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.core.tool_settings import TOOL_IDS
from chainscope_research.storage.repositories.chain_facts import utc_naive

ROOT = Path(__file__).resolve().parents[2]
ZERO = "0x" + "0" * 40


class LocalOperator:
    """只授权命令行明确指定的一项本地数据集，不能被网站用作默认授权器。"""

    def __init__(self, job_id):
        """记录人工明确选择的 job_id，不从模型参数扩展授权范围。"""
        self.job_id = job_id

    async def resolve(self, context):
        """仅本地验收身份能使用本次指定数据集；真实网站须替换成账户权限源。"""
        allowed = context.user_id == "local-data-tool-check"
        return ToolGrant(
            user_id=context.user_id,
            allowed_job_ids=(self.job_id,) if allowed else (),
            allowed_tools=tuple(sorted(TOOL_IDS)) if allowed else (),
        )


def read_check_data(view, job_id, hours):
    """读取样例哈希和独立 JOIN 结果；按缓存区块缩小范围，避免全历史哈希扫描。

    对照查询不调用工具的指标函数，以独立的整数加法核对时间筛选、日志去重和金额。
    """
    job = view.rows(
        sa.select(models.jobs).where(models.jobs.c.job_id == job_id)
    ).first()
    if job is None:
        raise ValueError("指定采集任务不存在")
    source = view.source(
        (job_id,), job["chain_id"], job["token_address"], datetime.now(UTC)
    )
    end = source.scope.data_cutoff_time
    start = end - timedelta(hours=hours)
    lower = view.rows(
        sa.select(models.block_headers.c.block_number)
        .where(
            models.block_headers.c.chain_id == job["chain_id"],
            models.block_headers.c.block_number.between(
                job["start_block"], source.through
            ),
            models.block_headers.c.block_time_utc < utc_naive(start),
        )
        .order_by(models.block_headers.c.block_number.desc())
        .limit(1)
    ).first()
    if lower is None or start - timedelta(hours=hours) < source.first_time:
        raise ValueError("既有缓存时间范围不足以完成本次重点及等长比较验收")
    first = lower["block_number"]
    t, we, w = models.transfers, models.window_events, models.windows
    statement = (
        sa.select(t)
        .distinct()
        .select_from(
            t.join(we, we.c.event_id == t.c.event_id).join(
                w,
                sa.and_(we.c.job_id == w.c.job_id, we.c.start_block == w.c.start_block),
            )
        )
        .where(
            we.c.job_id == job_id,
            we.c.start_block.between(
                max(job["start_block"], first - job["window_size"] + 1), source.through
            ),
            w.c.status == "done",
            t.c.chain_id == job["chain_id"],
            t.c.token_address == job["token_address"],
            t.c.block_number.between(first, source.through),
            t.c.block_number.between(w.c.start_block, w.c.end_block),
            t.c.block_time_utc >= utc_naive(start),
            t.c.block_time_utc < utc_naive(end),
        )
        .limit(200001)
    )
    rows = [dict(row) for row in view.rows(statement)]
    if not rows or len(rows) > 200000:
        raise ValueError("验收样例为空或超过独立对照查询预算，请调整 --hours")
    row = max(rows, key=lambda item: (item["block_number"], item["log_index"]))
    return source, start, end, row, rows


def independent_values(rows, address=None):
    """用单独口径表达式核对日志/交易计数、总额与分类金额，所有金额保持整数。"""
    selected = [
        row
        for row in rows
        if address is None or address in (row["from_address"], row["to_address"])
    ]
    totals = {key: 0 for key in ("ordinary", "mint", "burn", "self", "unknown_zero")}
    incoming = outgoing = 0
    for row in selected:
        sender, receiver, amount = (
            row["from_address"],
            row["to_address"],
            int(row["amount_raw"]),
        )
        if sender == receiver == ZERO:
            totals["unknown_zero"] += amount
        elif sender == ZERO:
            totals["mint"] += amount
        elif receiver == ZERO:
            totals["burn"] += amount
        elif sender == receiver:
            totals["self"] += amount
        else:
            totals["ordinary"] += amount
            if address:
                incoming += amount if receiver == address else 0
                outgoing += amount if sender == address else 0
    result = {
        "transfer_event_count": str(len(selected)),
        "transaction_count": str(len({row["tx_hash"] for row in selected})),
        "transfer_amount_raw": str(sum(int(row["amount_raw"]) for row in selected)),
        **{kind + "_amount_raw": str(value) for kind, value in totals.items()},
    }
    if address:
        result.update(
            inflow_raw=str(incoming),
            outflow_raw=str(outgoing),
            net_transfer_flow_raw=str(incoming - outgoing),
        )
    return result


async def check(job_id, hours):
    """经真实 StructuredTool.ainvoke 验收全部工具并输出安全摘要，最后关闭资源。"""
    app = build_analysis_application(
        ROOT, LocalOperator(job_id), SecretStr(secrets.token_hex(32))
    )
    try:
        source, start, end, sample, rows = await asyncio.to_thread(
            app.repository.run,
            lambda view: read_check_data(view, job_id, hours),
            threading.Event(),
            time.monotonic() + 30,
            30000,
        )
        context = ExecutionContext(
            user_id="local-data-tool-check",
            session_id="local-check",
            research_id="local-check",
            request_revision=1,
            task_id="data-tools",
            attempt_id="local",
            call_id="smoke",
            data_cutoff_time=end,
            deadline_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        tools = {
            tool.name: tool
            for tool in await create_data_tools(
                app, load_prompt_registry(ROOT), context
            )
        }
        address = next(
            value
            for value in (sample["from_address"], sample["to_address"])
            if value != ZERO
        )
        periods = {
            "focus_period": {"start": start.isoformat(), "end": end.isoformat()},
            "comparison_periods": [
                {
                    "period": {
                        "start": (start - (end - start)).isoformat(),
                        "end": start.isoformat(),
                    },
                    "reason": "等长比较验收",
                }
            ],
        }
        common = {
            "chain_id": source.scope.chain_id,
            "token_address": source.scope.token_address,
        }
        requests = {
            "lookup_transaction_events": {
                **common,
                "tx_hash": sample["tx_hash"],
                "page_size": 50,
            },
            "lookup_block_events": {
                **common,
                "block_hash": sample["block_hash"],
                "page_size": 50,
            },
            "analyze_address_activity": {
                **common,
                "address": address,
                "periods": periods,
            },
            "analyze_token_activity": {**common, "periods": periods},
        }
        output = {
            "source": source.scope.model_dump(mode="json"),
            "focus_period": periods["focus_period"],
            "tools": {},
        }
        for name, payload in requests.items():
            began = time.monotonic()
            result = await tools[name].ainvoke(payload)
            status = result["outcome"]["status"]
            detail = {
                "status": status,
                "elapsed_seconds": round(time.monotonic() - began, 3),
            }
            if status not in ("success", "partial"):
                raise ValueError(f"{name} 未通过：{result['outcome']}")
            data = result["outcome"]["data"]
            if name.startswith("analyze_"):
                values = {
                    item["metric_id"]: item.get("value", item.get("value_raw"))
                    for item in data["slices"][0]["metrics"]
                }
                expected = independent_values(
                    rows, address if name == "analyze_address_activity" else None
                )
                if any(values[key] != value for key, value in expected.items()):
                    raise ValueError(f"{name} 与独立对照不一致")
                detail.update(independent_check="passed", metrics_checked=expected)
            else:
                detail.update(
                    events=len(data["events"]),
                    transfers=len(data["transfers"]),
                    swaps=len(data["swaps"]),
                    has_next_page=data["next_cursor"] is not None,
                )
                if not any(
                    ref["tx_hash"] == sample["tx_hash"]
                    and ref["log_index"] == sample["log_index"]
                    for ref in data["events"]
                ):
                    raise ValueError(f"{name} 未找到样例事件")
            output["tools"][name] = detail
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        await app.aclose()


def main():
    """只接受任务与小时参数；安全失败不打印数据库连接或完整驱动异常。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, help="本地操作者有权读取的采集任务")
    parser.add_argument(
        "--hours", type=int, default=1, choices=range(1, 25), metavar="1..24"
    )
    args = parser.parse_args()
    try:
        with tracing_context(enabled=False):
            asyncio.run(check(args.job_id, args.hours))
    except Exception as error:  # noqa: BLE001 -- 验收命令不得回显驱动异常中的秘密。
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "数据库或验收执行失败，请在受控环境检查。"
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
