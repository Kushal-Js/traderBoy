"""
Tests for Swing/relative_strength.py - the Relative Strength momentum-
ranking signal (backtest-only, 2 Sep 2026, user request: "explore a
different signal idea" after putting the HH/HL signal on hold).

Pure-function tests only, hand-verifiable synthetic series, same
convention every other pure-function test file in this repo follows.

HOW TO RUN:
    uv run python tests/test_swing_relative_strength.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Swing.relative_strength as rs


def test_1_align_keeps_only_common_dates():
    dates_a = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
    values_a = [10.0, 11.0, 12.0, 13.0]
    dates_b = ["2026-01-01", "2026-01-03", "2026-01-06", "2026-01-07"]  # missing 01-02, has an extra 01-07
    values_b = [100.0, 102.0, 104.0, 105.0]
    dates, a, b = rs.align_series_by_date(dates_a, values_a, dates_b, values_b)
    assert dates == ["2026-01-01", "2026-01-03", "2026-01-06"], dates
    assert a == [10.0, 12.0, 13.0], a
    assert b == [100.0, 102.0, 104.0], b
    print("1. align_series_by_date keeps only dates present in BOTH series, sorted chronologically: PASSED")


def test_2_align_with_no_overlap_returns_empty():
    dates, a, b = rs.align_series_by_date(["2026-01-01"], [10.0], ["2026-02-01"], [20.0])
    assert dates == [] and a == [] and b == []
    print("2. Two series with zero overlapping dates align to empty (no crash, no fabricated pairing): PASSED")


def test_3_relative_strength_basic_outperformance():
    # Stock up 10% over the window, benchmark up 4% -> RS = +6 points.
    stock = [100.0, 101.0, 103.0, 105.0, 110.0]
    bench = [1000.0, 1005.0, 1015.0, 1030.0, 1040.0]
    score = rs.relative_strength_score(stock, bench, as_of_index=4, lookback_days=4)
    assert score is not None
    assert abs(score - 6.0) < 0.01, score
    print(f"3. A stock outperforming the benchmark scores the correct positive excess return ({score:.2f} points): PASSED")


def test_4_relative_strength_underperformance_is_negative():
    stock = [100.0, 99.0, 98.0, 97.0, 96.0]  # -4%
    bench = [1000.0, 1005.0, 1010.0, 1015.0, 1020.0]  # +2%
    score = rs.relative_strength_score(stock, bench, as_of_index=4, lookback_days=4)
    assert score is not None
    assert abs(score - (-6.0)) < 0.01, score
    print(f"4. A stock lagging the benchmark scores the correct negative excess return ({score:.2f} points): PASSED")


def test_5_relative_strength_none_when_not_enough_history():
    stock = [100.0, 101.0, 102.0]
    bench = [1000.0, 1005.0, 1010.0]
    assert rs.relative_strength_score(stock, bench, as_of_index=2, lookback_days=5) is None
    print("5. Not enough history before as_of_index correctly returns None, never a fabricated 0: PASSED")


def test_6_relative_strength_exact_match_scores_zero_not_none():
    # Both up exactly 5% - a genuine "matched the benchmark" result must
    # be a real 0.0, distinguishable from "not enough history" (None).
    stock = [100.0, 105.0]
    bench = [1000.0, 1050.0]
    score = rs.relative_strength_score(stock, bench, as_of_index=1, lookback_days=1)
    assert score is not None
    assert abs(score - 0.0) < 0.001, score
    print("6. A stock that exactly matched the benchmark's own return scores a genuine 0.0 (not confused with None): PASSED")


def main():
    print("=== Swing relative-strength (momentum-ranking) test suite ===\n")
    test_1_align_keeps_only_common_dates()
    test_2_align_with_no_overlap_returns_empty()
    test_3_relative_strength_basic_outperformance()
    test_4_relative_strength_underperformance_is_negative()
    test_5_relative_strength_none_when_not_enough_history()
    test_6_relative_strength_exact_match_scores_zero_not_none()
    print("\nALL SWING RELATIVE-STRENGTH CHECKS PASSED")


if __name__ == "__main__":
    main()
