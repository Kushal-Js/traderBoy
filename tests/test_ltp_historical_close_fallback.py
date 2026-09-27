"""
Tests for _get_ltp's new historical-close fallback tier - added 18 Sep
2026 after a real incident: ABB 29 SEP 7200 CALL (Futures) had genuine
trading volume the whole time it was held (confirmed from its own 1-min
candles: hundreds to thousands of contracts trading most minutes), but
get_option_ltp's name-based get_ltp_data() lookup failed ~144 times
across the day ("No LTP returned for ABB 29 SEP 7200 CALL"). With no
fallback, _get_ltp simply raised on each failure, leaving MAX_LOSS_HIT
blind for minutes at a stretch - the position finally closed at a real
Rs 2737.50 loss against a configured Rs 2100 cap once a live LTP read
happened to succeed again.

The fix: when get_option_ltp fails, _get_ltp now tries
dhan_wrapper.get_last_historical_close (security_id-based, already
proven reliable during a live-quote outage - see that function's own
docstring for the ICICIPRULI incident it was originally built for)
before giving up. This keeps the regular poll-loop exit check
evaluating on SOME real price instead of going completely dark.

Covers, against the REAL production functions (not reimplemented):
  1. Options/Luxury _get_ltp both fall back to the historical close
     when get_option_ltp fails, and do NOT cache that fallback value via
     note_rest_ltp (it's a one-tick reading only).
  2. _get_ltp still raises (preserving the existing LTP-staleness
     escalation path) when BOTH get_option_ltp AND the historical-close
     fallback fail - a true, complete data blackout.

(A third scenario - the full ABB incident replayed end-to-end through
Futures' own _process_one_entry/_check_one_position - was removed 27
Sep 2026 when the Futures package was deleted entirely; the real
incident was specific to Futures' own position, so it wasn't ported to
another package rather than fabricating a scenario that never happened.
Scenarios 1-2 above still cover the underlying _get_ltp mechanism, which
is identical across every remaining package.)

HOW TO RUN:
    uv run python tests/test_ltp_historical_close_fallback.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import cross_strategy_registry

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_ltp_fallback_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.trading_engine as ote
import Luxury.trading_engine as lte


async def _assert_falls_back(engine, label):
    """Shared body for tests 1a/1c - one per package's own _get_ltp,
    since each package keeps its own copy of trading_engine.py."""
    real_cached = odc.dhan_wrapper.get_cached_option_ltp
    real_get_ltp = odc.dhan_wrapper._get_option_ltp_once
    real_historical = odc.dhan_wrapper.get_last_historical_close
    real_note = odc.dhan_wrapper.note_rest_ltp
    note_calls = []
    odc.dhan_wrapper.get_cached_option_ltp = lambda ts: None
    odc.dhan_wrapper._get_option_ltp_once = lambda ts: (_ for _ in ()).throw(
        ValueError(f"No LTP returned for {ts}")
    )
    odc.dhan_wrapper.get_last_historical_close = lambda ts: 125.45
    odc.dhan_wrapper.note_rest_ltp = lambda ts, ltp: note_calls.append((ts, ltp))
    try:
        result = await engine._get_ltp("ABB 29 SEP 7200 CALL")
        assert result == 125.45, f"{label}: expected the historical-close fallback value, got {result}"
        assert note_calls == [], \
            f"{label}: the fallback price must NOT be cached via note_rest_ltp, got {note_calls}"
        print(f"{label}: _get_ltp falls back to the historical close when get_option_ltp fails, "
              f"without caching it as a live LTP: PASSED")
    finally:
        odc.dhan_wrapper.get_cached_option_ltp = real_cached
        odc.dhan_wrapper._get_option_ltp_once = real_get_ltp
        odc.dhan_wrapper.get_last_historical_close = real_historical
        odc.dhan_wrapper.note_rest_ltp = real_note


async def test_1a_options_get_ltp_falls_back_to_historical_close():
    await _assert_falls_back(ote, "1a. Options")


async def test_1c_luxury_get_ltp_falls_back_to_historical_close():
    await _assert_falls_back(lte, "1c. Luxury")


async def test_2_get_ltp_still_raises_when_both_sources_fail():
    """A true, complete data blackout (both get_option_ltp AND the
    historical-close fallback fail) must still raise, so the existing
    LTP-staleness escalation (_handle_ltp_staleness, forces a market
    exit after config.LTP_STALE_FORCE_EXIT_MINUTES of CONTINUOUS
    failure) keeps working exactly as before this fix."""
    real_cached = odc.dhan_wrapper.get_cached_option_ltp
    real_get_ltp = odc.dhan_wrapper._get_option_ltp_once
    real_historical = odc.dhan_wrapper.get_last_historical_close
    odc.dhan_wrapper.get_cached_option_ltp = lambda ts: None
    odc.dhan_wrapper._get_option_ltp_once = lambda ts: (_ for _ in ()).throw(
        ValueError(f"No LTP returned for {ts}")
    )
    odc.dhan_wrapper.get_last_historical_close = lambda ts: None
    try:
        try:
            await ote._get_ltp("ABB 29 SEP 7200 CALL")
            assert False, "expected _get_ltp to raise when both the live LTP and the historical " \
                           "close fallback are unavailable"
        except ValueError as e:
            assert "No LTP returned" in str(e), str(e)
        print("2. _get_ltp still raises (preserving the existing LTP-staleness escalation path) when "
              "BOTH get_option_ltp AND the historical-close fallback fail: PASSED")
    finally:
        odc.dhan_wrapper.get_cached_option_ltp = real_cached
        odc.dhan_wrapper._get_option_ltp_once = real_get_ltp
        odc.dhan_wrapper.get_last_historical_close = real_historical


async def main():
    print("=== _get_ltp historical-close fallback test suite ===\n")
    await test_1a_options_get_ltp_falls_back_to_historical_close()
    await test_1c_luxury_get_ltp_falls_back_to_historical_close()
    await test_2_get_ltp_still_raises_when_both_sources_fail()
    print("\nALL LTP HISTORICAL-CLOSE FALLBACK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
