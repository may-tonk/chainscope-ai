"""Synthetic boundary cases; these are not claims about live chain data."""

import csv
import tempfile
import unittest
from pathlib import Path

from chainscope.records import FIELDS, inspect_csv, normalize_record


def sample():
    return {
        "block_number": "123",
        "log_index": "0",
        "tx_hash": "0x" + "1" * 64,
        "token_address": "0x" + "2" * 40,
        "from_address": "0x" + "0" * 40,
        "to_address": "0x" + "3" * 40,
        "amount_raw": str(2**256 - 1),
    }


class RecordTests(unittest.TestCase):
    def test_exact_large_integer_and_zero(self):
        row = sample()
        self.assertEqual(normalize_record(row)["amount_raw"], str(2**256 - 1))
        row["amount_raw"] = "0"
        self.assertEqual(normalize_record(row)["amount_raw"], "0")

    def test_reject_float_and_negative_amounts(self):
        for value in ("1.5", "-1", "1e18"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_record({**sample(), "amount_raw": value})

    def test_missing_and_invalid_fields(self):
        for field, value in (("tx_hash", "0x1"), ("to_address", "")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalize_record({**sample(), field: value})

    def test_sample_limit_and_log_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows([sample(), {**sample(), "log_index": "1"}, sample()])
            bounded = inspect_csv(path, 2)
            self.assertEqual(bounded["sample_count"], 2)
            self.assertEqual(bounded["duplicate_tx_log_ids_in_sample"], 0)
            self.assertEqual(inspect_csv(path, 3)["duplicate_tx_log_ids_in_sample"], 1)


if __name__ == "__main__":
    unittest.main()
