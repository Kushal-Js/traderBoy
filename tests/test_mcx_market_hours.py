"""
Tests for the MCX-aware is_market_open() fix (Options/dhan_client.py,
added 15 Sep 2026, real incident): is_market_open() used to check ONLY
NSE F&O hours (config.MARKET_OPEN_TIME/_CLOSE_TIME, 09:15-15:30)
regardless of which segment the caller actually cared about. MCX
(commodities, e.g. Copper) trades a materially longer session into the
evening - place_mcx_market_order's own is_amo = not self.is_market_open()
call was therefore wrongly tagging a genuinely-live MCX order as AMO any
time after 15:30 IST. Swing's own TRADED-only fill discipline then
treated that stuck-PENDING AMO as a failed entry and retried on the very
next monitor tick (see tests/test_swing_entry_retry_cooldown.py for that
half of the fix) - real result: 4 duplicate live BUY orders placed for
COPPER at ~20:10 IST before Dhan's own margin engine started hard-
rejecting further attempts.

Coverage:
  1. Default call (no exchange_segment) is unchanged - still NSE F&O
     hours, so every existing NSE-only caller (Options/Futures/Luxury's
     own EOD-gating logic, plus the equity/NSE order placers) keeps its
     exact current behavior.
  2. exchange_segment="MCX_COMM" checks config.MCX_MARKET_OPEN_TIME/
     _CLOSE_TIME instead - reproduces the actual incident time (20:10
     IST): NSE hours say closed, MCX hours say open.
  3. place_mcx_market_order actually passes exchange_segment="MCX_COMM"
     through to is_market_open (not just that the method itself works in
     isolation) - the real incident was this call site using the wrong
     (default/NSE) check, not the method's own logic.
  4. Both segments still correctly report closed outside their own hours
     (e.g. the middle of the night) - the fix widens MCX's window, it
     doesn't make everything look perpetually open.

HOW TO RUN:
    uv run python tests/test_mcx_market_hours.py
"""
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.config as config
import Options.dhan_client as odc
from Options.dhan_client import IST, DhanWrapper


def _at(hh: int, mm: int):
    """A datetime on today's date at the given IST wall-clock time, for
    patching datetime.now(IST) inside dhan_client's own module namespace."""
    today = datetime.now(IST).date()
    return datetime.combine(today, dtime(hh, mm), tzinfo=IST)


def test_1_default_segment_is_unchanged_nse_hours():
    w = DhanWrapper.__new__(DhanWrapper)
    with mock.patch("Options.dhan_client.datetime") as fake_dt:
        fake_dt.now.return_value = _at(20, 10)  # the real incident's actual time
        fake_dt.strptime = datetime.strptime
        assert w.is_market_open() is False, \
            "20:10 IST is well past NSE's 15:30 close - the default (no exchange_segment) call must still say closed"
        fake_dt.now.return_value = _at(11, 0)
        assert w.is_market_open() is True, "11:00 IST is within NSE's 09:15-15:30 window"
    print("1. Default is_market_open() call (no exchange_segment) is unchanged - still NSE F&O hours: PASSED")


def test_2_mcx_segment_checks_mcx_hours_and_catches_the_real_incident_time():
    w = DhanWrapper.__new__(DhanWrapper)
    with mock.patch("Options.dhan_client.datetime") as fake_dt:
        fake_dt.strptime = datetime.strptime
        fake_dt.now.return_value = _at(20, 10)  # real incident: COPPER order placed at 20:10 IST
        assert w.is_market_open(exchange_segment="MCX_COMM") is True, \
            "20:10 IST is well within MCX's evening session (09:00-23:30) - must report open, " \
            "reproducing the exact gap that caused the real incident"
    print("2. exchange_segment='MCX_COMM' checks MCX hours instead - correctly reports OPEN at the "
          "real incident's actual time (20:10 IST), where the old NSE-only check said closed: PASSED")


def test_3_place_mcx_market_order_passes_mcx_segment_through():
    """The real incident wasn't a bug in is_market_open() itself - it was
    place_mcx_market_order calling it with no argument (defaulting to NSE
    hours). This confirms the actual call site was fixed, not just the
    method's own logic in isolation."""
    w = DhanWrapper.__new__(DhanWrapper)
    seen_segments = []
    original = DhanWrapper.is_market_open

    def _spy(self, exchange_segment="NSE_FNO"):
        seen_segments.append(exchange_segment)
        return True  # pretend open, so this order is NOT tagged AMO

    w._client = mock.Mock()
    w._client.order_placement.return_value = "ORDER123"
    with mock.patch.object(DhanWrapper, "is_market_open", _spy):
        result = w.place_mcx_market_order("COPPER 23 SEP 1360 PUT", 1, "BUY")
    assert seen_segments == ["MCX_COMM"], \
        f"place_mcx_market_order must call is_market_open(exchange_segment='MCX_COMM'), got {seen_segments}"
    assert result["is_amo"] is False
    print("3. place_mcx_market_order passes exchange_segment='MCX_COMM' through to is_market_open "
          "(the actual call site the real incident's bug was in): PASSED")


def test_4_both_segments_still_report_closed_outside_their_own_hours():
    w = DhanWrapper.__new__(DhanWrapper)
    with mock.patch("Options.dhan_client.datetime") as fake_dt:
        fake_dt.strptime = datetime.strptime
        fake_dt.now.return_value = _at(2, 0)  # 2 AM - both segments genuinely closed
        assert w.is_market_open() is False
        assert w.is_market_open(exchange_segment="MCX_COMM") is False
    print("4. Both NSE and MCX segments still correctly report CLOSED outside their own hours "
          "(the fix widens MCX's window, it doesn't make everything look perpetually open): PASSED")


def main():
    print("=== MCX-aware is_market_open() fix test suite ===\n")
    test_1_default_segment_is_unchanged_nse_hours()
    test_2_mcx_segment_checks_mcx_hours_and_catches_the_real_incident_time()
    test_3_place_mcx_market_order_passes_mcx_segment_through()
    test_4_both_segments_still_report_closed_outside_their_own_hours()
    print("\nALL MCX market-hours FIX CHECKS PASSED")


if __name__ == "__main__":
    main()
