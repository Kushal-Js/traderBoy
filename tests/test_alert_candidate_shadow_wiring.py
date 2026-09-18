"""
Tests for the alert-candidate shadow-logging WIRING (added 18 Sep 2026) -
does each package's own _handle_chartink_webhook actually call reversal_
filters.log_alert_candidates right after ranking, with the correct
candidate/selected lists, and only when there was a genuine ranking
DECISION to log (more than one candidate)? The underlying function's own
behavior (fetch/log/fail-open) is already covered in tests/test_
reversal_filters_shadow.py - this file only tests each package's own
copy of the webhook handler, since (per this codebase's own established
convention) a wiring mistake in one package's copy wouldn't be caught by
another's tests.

Coverage, for EACH of Options/Futures/Luxury:
  1. A multi-stock alert calls log_alert_candidates exactly once, with
     the full candidate list and the actual selected symbols.
  2. A single-stock alert never calls it at all - no ranking DECISION
     exists to log.

HOW TO RUN:
    uv run python tests/test_alert_candidate_shadow_wiring.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_alert_candidate_shadow_wiring_test_"))
trade_history.HISTORY_DIR = scratch_dir

import reversal_filters
import Options.option_main as om
import Options.position_store as ops
import Futures.futures_main as fm
import Futures.position_store as fps
import Luxury.luxury_main as lm
import Luxury.position_store as lps


def _payload_for(module, stocks):
    return module.ChartinkWebhookPayload(
        stocks=",".join(stocks), trigger_prices=",".join(["1"] * len(stocks)), triggered_at="9:20 am",
        scan_name="wiring-test-scan", scan_url="wiring-test-scan", alert_name="Alert-candidate wiring test",
    )


async def _drive_webhook(module, store_module, store, ranked_result):
    """Common setup: bypass every time-window/capacity gate ahead of
    ranking, mock ranking to a controlled result and entry to a no-op, so
    only the log_alert_candidates call itself is under test."""
    module.position_store = store
    module.config.MAX_LIVE_POSITIONS_CE = 5
    module.config.MAX_LIVE_POSITIONS_PE = 5
    # This suite is about the shadow-logging WIRING (which candidates get
    # logged), not which ranking mechanism produced ranked_result - force
    # the original day-change% ranking path so the rank_and_pick_top_
    # stocks mock below actually takes effect (added 18 Sep 2026: with
    # RIBBON_RANKING_ENABLED on, a CE alert takes reversal_filters.rank_
    # by_ribbon_expansion instead, unmocked here).
    module.config.RIBBON_RANKING_ENABLED = False
    with mock.patch.object(module, "is_within_trading_windows", lambda: True), \
         mock.patch.object(module, "is_past_allowed_trading_time", lambda: False), \
         mock.patch.object(module, "is_past_square_off_time", lambda: False), \
         mock.patch.object(module, "rank_and_pick_top_stocks", lambda *a, **k: ranked_result), \
         mock.patch.object(module, "enter_positions_for_stocks", AsyncMock(return_value=[])), \
         mock.patch.object(reversal_filters, "log_alert_candidates", new=AsyncMock()) as fake_log:
        extra_ce_gate = getattr(module, "dhan_wrapper", None)
        if extra_ce_gate is not None and hasattr(extra_ce_gate, "should_delay_ce_entry"):
            with mock.patch.object(extra_ce_gate, "should_delay_ce_entry", lambda: False):
                await module._handle_chartink_webhook(_payload_for(module, ["RELIANCE", "TCS"]), "CE", True)
        else:
            await module._handle_chartink_webhook(_payload_for(module, ["RELIANCE", "TCS"]), "CE", True)
        await asyncio.sleep(0.05)  # let the fire-and-forget create_task actually run
        return fake_log


async def _drive_webhook_single_stock(module, store_module, store, ranked_result):
    module.position_store = store
    module.config.MAX_LIVE_POSITIONS_CE = 5
    module.config.MAX_LIVE_POSITIONS_PE = 5
    # See _drive_webhook's identical comment.
    module.config.RIBBON_RANKING_ENABLED = False
    with mock.patch.object(module, "is_within_trading_windows", lambda: True), \
         mock.patch.object(module, "is_past_allowed_trading_time", lambda: False), \
         mock.patch.object(module, "is_past_square_off_time", lambda: False), \
         mock.patch.object(module, "rank_and_pick_top_stocks", lambda *a, **k: ranked_result), \
         mock.patch.object(module, "enter_positions_for_stocks", AsyncMock(return_value=[])), \
         mock.patch.object(reversal_filters, "log_alert_candidates", new=AsyncMock()) as fake_log:
        extra_ce_gate = getattr(module, "dhan_wrapper", None)
        if extra_ce_gate is not None and hasattr(extra_ce_gate, "should_delay_ce_entry"):
            with mock.patch.object(extra_ce_gate, "should_delay_ce_entry", lambda: False):
                await module._handle_chartink_webhook(_payload_for(module, ["RELIANCE"]), "CE", True)
        else:
            await module._handle_chartink_webhook(_payload_for(module, ["RELIANCE"]), "CE", True)
        await asyncio.sleep(0.05)
        return fake_log


async def test_1_options_multi_stock_alert_calls_log_alert_candidates():
    store = ops.PositionStore()
    fake_log = await _drive_webhook(om, ops, store, [("TCS", 2.5)])
    fake_log.assert_called_once()
    args = fake_log.call_args
    assert args[0][0] == "Options"
    assert set(args[0][3]) == {"RELIANCE", "TCS"}, "must pass the FULL candidate list, not just the selected ones"
    assert args[0][4] == ["TCS"], "must pass the actually-selected symbols"
    print("1. Options: a multi-stock alert calls log_alert_candidates once with the full candidate "
          "list and the actual selected symbols: PASSED")


async def test_2_options_single_stock_alert_never_calls_it():
    store = ops.PositionStore()
    fake_log = await _drive_webhook_single_stock(om, ops, store, [("RELIANCE", 2.5)])
    fake_log.assert_not_called()
    print("2. Options: a single-stock alert never calls log_alert_candidates - no ranking "
          "decision exists to log: PASSED")


async def test_3_futures_multi_stock_alert_calls_log_alert_candidates():
    store = fps.PositionStore()
    fake_log = await _drive_webhook(fm, fps, store, [("TCS", 2.5)])
    fake_log.assert_called_once()
    args = fake_log.call_args
    assert args[0][0] == "Futures"
    assert set(args[0][3]) == {"RELIANCE", "TCS"}
    assert args[0][4] == ["TCS"]
    print("3. Futures: a multi-stock alert calls log_alert_candidates once with the full candidate "
          "list and the actual selected symbols: PASSED")


async def test_4_futures_single_stock_alert_never_calls_it():
    store = fps.PositionStore()
    fake_log = await _drive_webhook_single_stock(fm, fps, store, [("RELIANCE", 2.5)])
    fake_log.assert_not_called()
    print("4. Futures: a single-stock alert never calls log_alert_candidates: PASSED")


async def test_5_luxury_multi_stock_alert_calls_log_alert_candidates():
    store = lps.PositionStore()
    fake_log = await _drive_webhook(lm, lps, store, [("TCS", 2.5)])
    fake_log.assert_called_once()
    args = fake_log.call_args
    assert args[0][0] == "Luxury"
    assert set(args[0][3]) == {"RELIANCE", "TCS"}
    assert args[0][4] == ["TCS"]
    print("5. Luxury: a multi-stock alert calls log_alert_candidates once with the full candidate "
          "list and the actual selected symbols: PASSED")


async def test_6_luxury_single_stock_alert_never_calls_it():
    store = lps.PositionStore()
    fake_log = await _drive_webhook_single_stock(lm, lps, store, [("RELIANCE", 2.5)])
    fake_log.assert_not_called()
    print("6. Luxury: a single-stock alert never calls log_alert_candidates: PASSED")


async def main():
    print("=== Alert-candidate shadow-logging wiring test suite ===\n")
    await test_1_options_multi_stock_alert_calls_log_alert_candidates()
    await test_2_options_single_stock_alert_never_calls_it()
    await test_3_futures_multi_stock_alert_calls_log_alert_candidates()
    await test_4_futures_single_stock_alert_never_calls_it()
    await test_5_luxury_multi_stock_alert_calls_log_alert_candidates()
    await test_6_luxury_single_stock_alert_never_calls_it()
    print("\nALL alert-candidate shadow-logging wiring tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
