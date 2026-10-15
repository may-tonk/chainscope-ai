"""Exact balance deltas of a job's verified Transfers, applied by block range.

Only events linked to the job's windows count, so a Transfer that a redo
dropped from its window, or a row left by the old `collect` command, never
reaches a balance. MySQL DECIMAL holds at most 65 digits; capacity() refuses to
run when the collected amounts could overflow it instead of letting MySQL clamp.
"""

import sqlalchemy as sa

from .errors import ChainScopeError
from .models import transfers, windows

ZERO = "0x" + "0" * 40
DECIMAL_DIGITS = 65

LEGS = """
    SELECT t.to_address AS holder, CAST(t.amount_raw AS DECIMAL(65,0)) AS delta,
           t.block_number AS seen
    FROM window_events w JOIN transfer_events t ON t.event_id = w.event_id
    WHERE w.job_id = :job AND w.start_block BETWEEN :window_first AND :last
      AND t.block_number BETWEEN :first AND :last
    UNION ALL
    SELECT t.from_address, -CAST(t.amount_raw AS DECIMAL(65,0)), t.block_number
    FROM window_events w JOIN transfer_events t ON t.event_id = w.event_id
    WHERE w.job_id = :job AND w.start_block BETWEEN :window_first AND :last
      AND t.block_number BETWEEN :first AND :last
"""
UPSERT = f"""
INSERT INTO address_balances (chain_id, token_address, address, balance_raw, last_block)
SELECT 56, :token, holder, delta_sum, seen_block FROM (
    SELECT holder, SUM(delta) * :sign AS delta_sum, MAX(seen) AS seen_block
    FROM ({LEGS}) AS legs
    WHERE holder <> :zero
    GROUP BY holder
) AS d
ON DUPLICATE KEY UPDATE
    balance_raw = balance_raw + delta_sum,
    last_block = GREATEST(last_block, seen_block)
"""


def range_params(job: dict, first: int, last: int) -> dict:
    # A window never spans more than window_size blocks, so any window holding
    # a block >= first starts at or after first - window_size + 1.
    return {
        "job": job["job_id"],
        "token": job["token_address"],
        "first": first,
        "last": last,
        "window_first": max(0, first - job["window_size"] + 1),
        "zero": ZERO,
    }


def capacity(connection, job: dict) -> None:
    """Refuse when |balance| could exceed DECIMAL(65,0) for the collected data."""
    longest = connection.scalar(
        sa.select(sa.func.max(transfers.c.amount_len)).where(
            transfers.c.chain_id == 56,
            transfers.c.token_address == job["token_address"],
        )
    )
    events = connection.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(windows.c.event_count), 0)).where(
            windows.c.job_id == job["job_id"], windows.c.status == "done"
        )
    )
    if longest is None:
        return
    # Every balance is a sum of at most `events` amounts below 10**longest.
    if (10**longest - 1) * max(int(events), 1) >= 10**DECIMAL_DIGITS:
        raise ChainScopeError(
            f"最大转账金额有 {longest} 位、已采集 {int(events)} 条事件，"
            "持仓累计可能超过 MySQL DECIMAL(65,0)；为避免截断，停止计算持仓"
        )


def apply_range(connection, job: dict, first: int, last: int, sign: int) -> None:
    """Add (sign=1) or remove (sign=-1) the Transfers of [first, last]."""
    if last < first:
        return
    capacity(connection, job)
    connection.execute(
        sa.text(UPSERT), {**range_params(job, first, last), "sign": sign}
    )


def clear(connection, token: str) -> None:
    connection.execute(
        sa.text(
            "DELETE FROM address_balances WHERE chain_id = 56 AND token_address = :t"
        ),
        {"t": token},
    )
