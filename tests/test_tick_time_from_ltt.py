"""
Tests for Options/dhan_client.py's _tick_time_from_ltt - bucketing a WS
tick by the exchange's own LTT (Last Trade Time) instead of local receipt
time. Real incident (22 Sep 2026): underlying_candle_feed.py's 5-min bar
reconstruction used local receipt time for bucketing, confirmed live to
misattribute a meaningful fraction of bars' OPEN specifically (up to 75%
wrong on some symbols) while close/volume stayed accurate - exactly the
signature of a boundary-timing bug, not an aggregation bug. LTT's
timezone was empirically confirmed live (not guessed): the SDK's own
utc_time() output, taken as a plain IST HH:MM:SS string, needs NO further
UTC->IST conversion - see _tick_time_from_ltt's own docstring.

HOW TO RUN:
    uv run python tests/test_tick_time_from_ltt.py
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import _tick_time_from_ltt  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def test_1_ltt_present_and_well_formed_is_used_verbatim_no_conversion():
    """The empirically-confirmed case: LTT is already IST wall-clock -
    used as-is, no +5:30 or any other offset applied."""
    received_at = datetime(2026, 9, 22, 10, 32, 45, 123456, tzinfo=IST)
    result = _tick_time_from_ltt("10:29:59", received_at)
    assert result == datetime(2026, 9, 22, 10, 29, 59, tzinfo=IST), f"got {result}"
    print("1. Well-formed LTT is used verbatim as IST wall-clock time, no conversion: PASSED")


def test_2_ltt_keeps_received_at_own_calendar_date():
    """LTT is HH:MM:SS only (no date) - must combine with received_at's
    own date, not silently default to some other day (e.g. epoch day 1)."""
    received_at = datetime(2026, 9, 22, 14, 0, 0, tzinfo=IST)
    result = _tick_time_from_ltt("13:55:10", received_at)
    assert result.date() == received_at.date(), f"expected date 2026-09-22, got {result.date()}"
    assert result.tzinfo == received_at.tzinfo
    print("2. LTT combines with received_at's own calendar date, correct tzinfo: PASSED")


def test_3_missing_ltt_falls_back_to_received_at():
    """A missing/None/empty LTT must degrade gracefully to the OLD
    behavior (local receipt time), never drop the tick or raise."""
    received_at = datetime(2026, 9, 22, 11, 0, 0, 500000, tzinfo=IST)
    assert _tick_time_from_ltt(None, received_at) == received_at
    assert _tick_time_from_ltt("", received_at) == received_at
    print("3. Missing/empty LTT falls back to received_at (old behavior), no crash: PASSED")


def test_4_malformed_ltt_falls_back_to_received_at():
    """A malformed LTT (wrong format, garbage, wrong type) must also
    degrade gracefully, not raise and take down tick processing - this
    runs on the MarketFeed's own background thread, where an unhandled
    exception would be far more damaging than a slightly-stale bucket."""
    received_at = datetime(2026, 9, 22, 12, 15, 0, tzinfo=IST)
    for bad in ["not-a-time", "25:99:99", 12345, ["10:00:00"], {}]:
        result = _tick_time_from_ltt(bad, received_at)
        assert result == received_at, f"malformed LTT {bad!r} should fall back to received_at, got {result}"
    print("4. Malformed LTT (wrong format/type) falls back to received_at, never raises: PASSED")


def test_5_boundary_case_ltt_just_before_a_5min_mark():
    """THE actual case this fix targets: a trade at 10:29:59 (LTT)
    processed a moment after 10:30:00 (received_at) must bucket to the
    10:29 series (via a caller's own _candle_start_for flooring to
    10:25), not get misattributed to the 10:30 bar the old
    local-receipt-time behavior would have produced."""
    received_at = datetime(2026, 9, 22, 10, 30, 0, 87000, tzinfo=IST)  # processed 87ms after the boundary
    result = _tick_time_from_ltt("10:29:59", received_at)
    assert result.minute == 29 and result.hour == 10, (
        f"a trade whose real LTT was 10:29:59 must bucket into the 10:25-10:30 window, not 10:30-10:35 - "
        f"got {result} (old behavior would have used received_at={received_at}, the wrong window)"
    )
    print("5. A trade just before a 5-min boundary, processed just after it, still bucks to its own "
          "true window via LTT - the exact incident this fix targets: PASSED")


def main():
    print("=== Options.dhan_client._tick_time_from_ltt test suite ===\n")
    test_1_ltt_present_and_well_formed_is_used_verbatim_no_conversion()
    test_2_ltt_keeps_received_at_own_calendar_date()
    test_3_missing_ltt_falls_back_to_received_at()
    test_4_malformed_ltt_falls_back_to_received_at()
    test_5_boundary_case_ltt_just_before_a_5min_mark()
    print("\nALL TICK_TIME_FROM_LTT TESTS PASSED")


if __name__ == "__main__":
    main()
