"""
Regression tests for the 27 Sep 2026 proactive sweep (following the
IndexScalping weekend-polling fix - see tests/test_index_scalping_
weekend_gate.py) that found the SAME gap in two more places:

  1. breakout_paper_engine.py's paper_engine_monitor_loop/_check_one -
     had NO time/weekday check anywhere, and this engine has no EOD or
     Friday square-off of its own either. A real open paper position
     (Options/Luxury, BREAKOUT_PAPER_MODE_ENABLED) would be polled (LTP +
     Supertrend/EMA-cross/liquidity refresh - all real Dhan calls) every
     MONITOR_INTERVAL_SECONDS continuously, nights and weekends included,
     for however many days it took to organically exit.
  2. Options/paper_webhook.py's poll_loop - had a time-of-day-only EOD
     boundary (square_off_at) but no weekday check, so a position that
     existed on or survived into a weekend would either be polled
     continuously (before the EOD time-of-day) or force-closed against
     stale weekend data (after it).

Both are now fixed by checking dhan_wrapper.is_market_open() (itself
fixed the same day to be weekday-aware, see tests/test_mcx_market_hours.
py's own new test_5) before doing anything Dhan-related.

HOW TO RUN:
    uv run python tests/test_paper_engines_weekend_gate.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import breakout_paper_engine as bpe
import Options.paper_webhook as pw


async def _run_briefly(coro, seconds=0.15):
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_1_breakout_paper_engine_skips_check_one_when_market_closed():
    with mock.patch.object(bpe.dhan_wrapper, "is_market_open", return_value=False), \
         mock.patch.object(bpe, "_check_one") as fake_check_one, \
         mock.patch.object(bpe, "_reset_daily_counters_if_new_day"):
        bpe._positions[("Options", "FAKESYM")] = object()  # any non-None value - _check_one is mocked out
        try:
            asyncio.run(_run_briefly(bpe.paper_engine_monitor_loop()))
        finally:
            bpe._positions.pop(("Options", "FAKESYM"), None)
        assert not fake_check_one.called, \
            "market closed - paper_engine_monitor_loop must not call _check_one at all"
    print("1. breakout_paper_engine's monitor loop makes zero _check_one calls while the market "
          "is closed, even with an open paper position present: PASSED")


def test_2_breakout_paper_engine_calls_check_one_when_market_open():
    with mock.patch.object(bpe.dhan_wrapper, "is_market_open", return_value=True), \
         mock.patch.object(bpe, "_check_one", new=mock.AsyncMock()) as fake_check_one, \
         mock.patch.object(bpe, "_reset_daily_counters_if_new_day"):
        bpe._positions[("Options", "FAKESYM")] = object()
        try:
            asyncio.run(_run_briefly(bpe.paper_engine_monitor_loop()))
        finally:
            bpe._positions.pop(("Options", "FAKESYM"), None)
        assert fake_check_one.called, \
            "market open - paper_engine_monitor_loop must still reach _check_one for an open position"
    print("2. breakout_paper_engine's monitor loop still checks open positions normally while the "
          "market is open (the fix doesn't block legitimate polling): PASSED")


def test_3_options_paper_webhook_poll_loop_skips_everything_when_market_closed():
    with mock.patch.object(pw.dhan_wrapper, "is_market_open", return_value=False), \
         mock.patch.object(pw, "_get_ltp", new=mock.AsyncMock()) as fake_get_ltp:
        pw.paper_store.open_positions["FAKESYM"] = object()  # any value - loop body under test never reaches it
        try:
            asyncio.run(_run_briefly(pw.poll_loop()))
        finally:
            pw.paper_store.open_positions.pop("FAKESYM", None)
        assert not fake_get_ltp.called, \
            "market closed - poll_loop must not fetch LTP (or do anything else Dhan-related) at all"
    print("3. Options/paper_webhook's poll_loop makes zero LTP/Dhan calls while the market is "
          "closed, even with an open paper position present: PASSED")


if __name__ == "__main__":
    test_1_breakout_paper_engine_skips_check_one_when_market_closed()
    test_2_breakout_paper_engine_calls_check_one_when_market_open()
    test_3_options_paper_webhook_poll_loop_skips_everything_when_market_closed()
    print("\nAll tests passed.")
