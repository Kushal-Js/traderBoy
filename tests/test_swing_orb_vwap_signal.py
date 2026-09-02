"""
Tests for Swing/orb_vwap_signal.py - the ORB + VWAP + Volume confluence
re-ranking signal (backtest-only, 2 Sep 2026, the second "different
signal idea" explored after putting HH/HL on hold).

Pure-function tests only, hand-verifiable synthetic series.

HOW TO RUN:
    uv run python tests/test_swing_orb_vwap_signal.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Swing.orb_vwap_signal as ovs


def _ts(day_offset: int, hour: int, minute: int) -> int:
    base = datetime(2026, 6, 1, 9, 15)
    return int((base + timedelta(days=day_offset, hours=hour - 9, minutes=minute - 15)).timestamp())


def test_1_day_boundaries_splits_correctly():
    # Day 0: 3 bars, Day 1: 2 bars, Day 2: 4 bars
    timestamps = (
        [_ts(0, 9, 15 + 5 * i) for i in range(3)]
        + [_ts(1, 9, 15 + 5 * i) for i in range(2)]
        + [_ts(2, 9, 15 + 5 * i) for i in range(4)]
    )
    bounds = ovs.day_boundaries(timestamps)
    assert bounds == [(0, 3), (3, 5), (5, 9)], bounds
    print("1. day_boundaries correctly splits a multi-day timestamp series into per-day (start, end) ranges: PASSED")


def test_2_opening_range_basic():
    highs = [10.0, 12.0, 11.0, 15.0, 9.0]
    lows = [9.0, 9.5, 10.0, 8.0, 8.5]
    or_high, or_low = ovs.opening_range(highs, lows, day_start=0, or_bars=3)
    assert or_high == 12.0 and or_low == 9.0, (or_high, or_low)
    print("2. opening_range correctly takes the high/low of only the first N bars of the day: PASSED")


def test_3_opening_range_none_when_day_too_short():
    highs = [10.0, 12.0]
    lows = [9.0, 9.5]
    assert ovs.opening_range(highs, lows, day_start=0, or_bars=5) is None
    print("3. opening_range returns None (not a misleading partial range) when the day has fewer than N bars: PASSED")


def test_4_vwap_resets_each_day():
    # Day 0: 2 bars, Day 1: 2 bars - VWAP must not carry over between days.
    highs = [10.0, 12.0, 100.0, 100.0]
    lows = [10.0, 12.0, 100.0, 100.0]
    closes = [10.0, 12.0, 100.0, 100.0]
    volumes = [100.0, 100.0, 50.0, 50.0]
    bounds = [(0, 2), (2, 4)]
    vwap = ovs.vwap_series(highs, lows, closes, volumes, bounds)
    # Day 0 bar 1: typical=10, cum_pv=1000, cum_vol=100 -> vwap=10
    assert abs(vwap[0] - 10.0) < 0.01, vwap[0]
    # Day 0 bar 2: typical=12, cum_pv=1000+1200=2200, cum_vol=200 -> vwap=11
    assert abs(vwap[1] - 11.0) < 0.01, vwap[1]
    # Day 1 bar 1: typical=100, cum_pv=5000, cum_vol=50 -> vwap=100 (reset, not polluted by day 0's ~11)
    assert abs(vwap[2] - 100.0) < 0.01, vwap[2]
    print("4. vwap_series correctly resets its cumulative sums at the start of each new trading day: PASSED")


def test_5_orb_breakout_score_requires_a_real_close_beyond_the_range():
    # ATR=2.0, cap=1.0 ATR
    assert ovs.orb_breakout_score(entry_close=105.0, or_high=105.0, atr=2.0) == 0.0, \
        "sitting exactly AT the range high must not count as having broken out"
    assert ovs.orb_breakout_score(entry_close=104.0, or_high=105.0, atr=2.0) == 0.0
    assert ovs.orb_breakout_score(entry_close=106.0, or_high=105.0, atr=2.0) == 0.5  # 1 point / 2 ATR = 0.5 ATRs -> 0.5
    assert ovs.orb_breakout_score(entry_close=107.0, or_high=105.0, atr=2.0) == 1.0  # 1 full ATR -> saturates at cap
    print("5. orb_breakout_score is 0 until price genuinely CLOSES beyond the opening range's high, then scales up "
          "to 1.0 as the margin grows, saturating at the ATR cap: PASSED")


def test_6_vwap_confluence_score_basic():
    assert ovs.vwap_confluence_score(entry_close=100.0, vwap_at_entry=100.0, atr=2.0) == 0.0
    assert ovs.vwap_confluence_score(entry_close=99.0, vwap_at_entry=100.0, atr=2.0) == 0.0, \
        "price below VWAP must score 0, not a negative number"
    # margin=0.5 ATR, cap=0.5 ATR -> saturates at 1.0
    assert ovs.vwap_confluence_score(entry_close=101.0, vwap_at_entry=100.0, atr=2.0) == 1.0
    assert ovs.vwap_confluence_score(entry_close=100.5, vwap_at_entry=100.0, atr=2.0) == 0.5
    assert ovs.vwap_confluence_score(entry_close=105.0, vwap_at_entry=None, atr=2.0) is None, \
        "VWAP itself unavailable must return None, never a fabricated score"
    print("6. vwap_confluence_score is 0 at/below VWAP, scales to 1.0 over a smaller ATR cap than the ORB score, "
          "and returns None when VWAP itself isn't available: PASSED")


def test_7_composite_score_missing_components_returns_none():
    score = ovs.orb_vwap_composite_score(orb=0.8, vwap=0.9, rvol=2.0)
    assert score is not None and score > 0.8
    assert ovs.orb_vwap_composite_score(orb=None, vwap=0.9, rvol=2.0) is None
    assert ovs.orb_vwap_composite_score(orb=0.8, vwap=None, rvol=2.0) is None
    assert ovs.orb_vwap_composite_score(orb=0.8, vwap=0.9, rvol=None) is None
    # orb=0.0 (a real "didn't break out" answer) must NOT be treated as missing.
    zero_orb_score = ovs.orb_vwap_composite_score(orb=0.0, vwap=0.9, rvol=2.0)
    assert zero_orb_score is not None and zero_orb_score < score
    print("7. The composite score combines all three components, returns None only when VWAP/RVOL are genuinely "
          "unavailable, and correctly treats orb=0.0 ('didn't break out') as a real low score, not missing data: PASSED")


def main():
    print("=== Swing ORB+VWAP+Volume confluence signal test suite ===\n")
    test_1_day_boundaries_splits_correctly()
    test_2_opening_range_basic()
    test_3_opening_range_none_when_day_too_short()
    test_4_vwap_resets_each_day()
    test_5_orb_breakout_score_requires_a_real_close_beyond_the_range()
    test_6_vwap_confluence_score_basic()
    test_7_composite_score_missing_components_returns_none()
    print("\nALL SWING ORB+VWAP+VOLUME SIGNAL CHECKS PASSED")


if __name__ == "__main__":
    main()
