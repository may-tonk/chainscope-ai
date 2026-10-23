"""Final completion must include current balances, not just collected events."""

import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "tools" / "run_bem_history.py"
spec = importlib.util.spec_from_file_location("history_runner", path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize(
    "checked,mark,negative,scanned,expected",
    [
        (True, 100, 0, 100, True),
        (False, 100, 0, 100, False),
        (True, 99, 0, 100, False),
        (True, 100, 1, 100, False),
        (True, 100, 0, 99, False),
    ],
)
def test_final_analysis_completion(checked, mark, negative, scanned, expected):
    analysis = {
        "holders_on_chain_check": {"ok": checked},
        "holders": {"as_of_block": mark, "negative_balances": negative},
        "swaps": {"coverage": {"state": "complete"}},
    }
    assert runner.analysis_complete(analysis, 100, scanned) is expected


def test_complete_transfers_cannot_hide_missing_swap_coverage():
    analysis = {
        "holders_on_chain_check": {"ok": True},
        "holders": {"as_of_block": 100, "negative_balances": 0},
        "swaps": {"coverage": {"state": "partial"}},
    }
    assert not runner.analysis_complete(analysis, 100, 100)
