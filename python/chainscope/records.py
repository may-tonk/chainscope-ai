"""Inspect a bounded sample of historical, normalized Transfer CSV records."""

import csv
import re
from itertools import islice
from pathlib import Path

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
FIELDS = (
    "block_number",
    "log_index",
    "tx_hash",
    "token_address",
    "from_address",
    "to_address",
    "amount_raw",
)


def normalize_record(row: dict[str, str]) -> dict:
    missing = [field for field in FIELDS if not row.get(field)]
    if missing:
        raise ValueError("缺少字段：" + ", ".join(missing))

    result = {}
    for field in ("block_number", "log_index", "amount_raw"):
        value = row[field]
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError(f"{field} 必须是非负十进制整数")
        number = int(value)
        result[field] = str(number) if field == "amount_raw" else number

    for field in ("token_address", "from_address", "to_address", "tx_hash"):
        pattern = TX_HASH if field == "tx_hash" else ADDRESS
        if not pattern.fullmatch(row[field]):
            raise ValueError(f"{field} 格式无效")
        result[field] = row[field].lower()
    return result


def inspect_csv(path: Path, limit: int) -> dict:
    if not 1 <= limit <= 1000:
        raise ValueError("limit 必须在 1–1000 之间")

    records = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("CSV 缺少列：" + ", ".join(sorted(missing)))
        for line, row in enumerate(islice(reader, limit), start=2):
            try:
                records.append(normalize_record(row))
            except ValueError as exc:
                raise ValueError(f"CSV 第 {line} 行：{exc}") from exc

    seen = set()
    duplicates = 0
    for row in records:
        # This command assumes a single-chain input, as documented in the W1 scope.
        key = (row["tx_hash"], row["log_index"])
        if key in seen:
            duplicates += 1
        seen.add(key)

    blocks = [row["block_number"] for row in records]
    return {
        "scope": "仅检查指定文件的前 N 条记录；未验证全量完整性或链上规范性",
        "sample_count": len(records),
        "block_range_in_sample": [min(blocks), max(blocks)] if blocks else None,
        "duplicate_tx_log_ids_in_sample": duplicates,
        "records": records,
    }
