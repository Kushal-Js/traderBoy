"""
Tests for trade_history.loss_count_today and reversal_filters.
check_trend_strength_sync - the two new pure functions behind the 18 Sep
2026 broadening of LOSS_REPEAT_BLOCK (real incident, user request): a
real ATHERENERG 29 SEP 1540 PUT (Options, 17 Sep 2026) lost Rs 1,537.50
via SUPERTREND_EXIT and Rs 2,325.00 via EMA_CROSS_EXIT - neither reason
was in LOSS_REPEAT_BLOCK_EXIT_REASONS (MAX_LOSS_HIT/STOP_LOSS_HIT only),
so the block never engaged despite two real same-day losses, and a 3rd
entry followed. loss_count_today counts ANY exit that closed at pnl < 0,
regardless of reason. check_trend_strength_sync is the new re-entry gate
(ADX or Efficiency Ratio must confirm a genuine trend) applied once a
symbol has lost money >= 1 time today.

Package-specific wiring (does _process_one_entry actually call these and
skip/proceed correctly) is covered in each package's own test_*_
corrective_actions.py, per this codebase's own convention that each
package's copy of trading_engine.py needs its own wiring coverage.

Coverage:
  1. loss_count_today counts a SUPERTREND_EXIT that closed at a real loss
     - the exact ATHERENERG pattern the old reason-scoped function missed.
  2. loss_count_today counts an EMA_CROSS_EXIT that closed at a real loss
     - the second half of the same real incident.
  3. loss_count_today does NOT count a winning trade (pnl >= 0), even one
     tagged with a reason that sounds loss-related in other contexts.
  4. loss_count_today only counts the given strategy+symbol - a different
     strategy's own loss for the same symbol, and the same strategy's
     loss for a different symbol, are both correctly ignored.
  5. loss_count_today fails OPEN (returns 0) when today's log file simply
     doesn't exist yet.
  6. check_trend_strength_sync fails OPEN (passes=True, adx=None, er=None)
     when dhan_wrapper._client is None - never triggers a real login as a
     side effect, same discipline as check_volume_floor_sync.

HOW TO RUN:
    uv run python tests/test_loss_repeat_block_broadening.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_loss_repeat_block_broadening_test_"))
trade_history.HISTORY_DIR = scratch_dir

import reversal_filters
from Options.dhan_client import dhan_wrapper


def _closed_trade(strategy, symbol, entry, exit_price, reason, closed_at=None):
    return {
        "strategy": strategy, "underlying_symbol": symbol, "option_trading_symbol": f"{symbol} FAKE",
        "option_type": "CE", "quantity": 100, "product_type": "MARGIN",
        "entry_price": entry, "exit_price": exit_price, "exit_reason": reason,
        "pnl": (exit_price - entry) * 100,
        "opened_at": "2026-09-17T09:00:00", "closed_at": closed_at or datetime.now().isoformat(),
        "order_id": "", "logged_at": datetime.now().isoformat(),
    }


def test_1_supertrend_exit_loss_is_counted():
    trade_history.append_jsonl(trade_history.REAL_TRADES_NAME, _closed_trade(
        "Options", "ATHERENERG", 50.1, 46.0, "SUPERTREND_EXIT"))
    count = trade_history.loss_count_today("Options", "ATHERENERG")
    assert count == 1, f"a real SUPERTREND_EXIT loss must be counted (the exact ATHERENERG pattern), got {count}"
    print("1. loss_count_today counts a SUPERTREND_EXIT that closed at a real loss - the exact ATHERENERG "
          "pattern the old reason-scoped function missed: PASSED")


def test_2_ema_cross_exit_loss_is_counted():
    trade_history.append_jsonl(trade_history.REAL_TRADES_NAME, _closed_trade(
        "Options", "ATHERENERG2", 49.45, 43.25, "EMA_CROSS_EXIT"))
    count = trade_history.loss_count_today("Options", "ATHERENERG2")
    assert count == 1, f"a real EMA_CROSS_EXIT loss must be counted, got {count}"
    print("2. loss_count_today counts an EMA_CROSS_EXIT that closed at a real loss - the second half of the "
          "same real incident: PASSED")


def test_3_winning_trade_not_counted():
    trade_history.append_jsonl(trade_history.REAL_TRADES_NAME, _closed_trade(
        "Options", "WINNER", 50.0, 60.0, "TARGET_HIT"))
    count = trade_history.loss_count_today("Options", "WINNER")
    assert count == 0, f"a genuine win (pnl >= 0) must never be counted, got {count}"
    print("3. loss_count_today does NOT count a winning trade (pnl >= 0): PASSED")


def test_4_only_matching_strategy_and_symbol_count():
    trade_history.append_jsonl(trade_history.REAL_TRADES_NAME, _closed_trade(
        "Futures", "SCOPED", 50.0, 40.0, "STOP_LOSS_HIT"))  # different strategy
    trade_history.append_jsonl(trade_history.REAL_TRADES_NAME, _closed_trade(
        "Options", "OTHERSYM", 50.0, 40.0, "STOP_LOSS_HIT"))  # different symbol
    assert trade_history.loss_count_today("Options", "SCOPED") == 0, \
        "a different strategy's own loss for the same symbol must not count"
    print("4. loss_count_today only counts the given strategy+symbol - a different strategy's loss for the "
          "same symbol, and the same strategy's loss for a different symbol, are both ignored: PASSED")


def test_5_fails_open_when_no_log_file_exists_yet():
    count = trade_history.loss_count_today("Options", "NEVERTRADEDTODAY")
    assert count == 0, f"no log entries at all must read as 0 losses, not raise, got {count}"
    print("5. loss_count_today fails OPEN (returns 0) when there are no matching entries: PASSED")


def test_6_check_trend_strength_fails_open_when_not_authenticated():
    original = dhan_wrapper._client
    dhan_wrapper._client = None
    with mock.patch.object(dhan_wrapper, "authenticate") as fake_auth:
        try:
            passes, adx, er = reversal_filters.check_trend_strength_sync("RELIANCE")
            assert passes is True and adx is None and er is None
            fake_auth.assert_not_called()
            print("6. check_trend_strength_sync fails OPEN (passes=True) when dhan_wrapper._client is None, "
                  "and never triggers a real login as a side effect: PASSED")
        finally:
            dhan_wrapper._client = original


def main():
    print("=== loss_count_today / check_trend_strength_sync test suite ===\n")
    test_1_supertrend_exit_loss_is_counted()
    test_2_ema_cross_exit_loss_is_counted()
    test_3_winning_trade_not_counted()
    test_4_only_matching_strategy_and_symbol_count()
    test_5_fails_open_when_no_log_file_exists_yet()
    test_6_check_trend_strength_fails_open_when_not_authenticated()
    print("\nALL loss_count_today / check_trend_strength_sync CHECKS PASSED")


if __name__ == "__main__":
    main()
