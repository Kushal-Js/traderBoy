"""
Tests for Swing/momentum_signal.py - the HH/HL momentum-continuation
composite score (backtest-only as of 2 Sep 2026, user request: "build
and backtest a logic... additional pre-filter/context layer... re-rank
them (soft signal)").

Pure-function tests only, same convention _compute_supertrend/
_compute_ema's own tests use - hand-built synthetic series with a known,
verifiable-by-inspection correct answer, no network/mocking needed.

HOW TO RUN:
    uv run python tests/test_swing_momentum_signal.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Swing.momentum_signal as ms


def test_1_fractal_swing_high_basic():
    # index: 0  1   2   3  4  5  6   7   8  9  10
    highs = [5, 6, 10, 7, 6, 5, 9, 12, 9, 6, 5]
    # idx 2 (10) beats idx 0,1 (5,6) before and idx 3,4 (7,6) after with k=2 -> swing high
    # idx 7 (12) beats idx 5,6 (5,9) before and idx 8,9 (9,6) after with k=2 -> swing high
    result = ms.find_fractal_swing_highs([float(x) for x in highs], k=2)
    assert result == [2, 7], result
    print("1. A fractal swing high is correctly detected only when higher than k bars on both sides: PASSED")


def test_2_fractal_swing_low_basic():
    # index: 0   1  2  3  4  5  6  7  8   9   10
    lows = [10, 8, 4, 7, 8, 9, 3, 6, 8, 10, 11]
    # idx 2 (4) beats idx 0,1 (10,8) before and idx 3,4 (7,8) after with k=2 -> swing low
    # idx 6 (3) beats idx 4,5 (8,9) before and idx 7,8 (6,8) after with k=2 -> swing low
    result = ms.find_fractal_swing_lows([float(x) for x in lows], k=2)
    assert result == [2, 6], result
    print("2. A fractal swing low is correctly detected only when lower than k bars on both sides: PASSED")


def test_3_no_swings_in_a_monotonic_series():
    highs = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    lows = [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    assert ms.find_fractal_swing_highs(highs, k=2) == []
    assert ms.find_fractal_swing_lows(lows, k=2) == []
    print("3. A purely monotonic series has no fractal swing points either direction: PASSED")


def test_4_edges_never_confirmed():
    # A genuine peak sitting inside the first/last k bars can never be
    # confirmed - not enough bars on one side to check yet.
    highs = [20.0, 5.0, 6.0, 7.0, 8.0]
    assert ms.find_fractal_swing_highs(highs, k=2) == [], \
        "a peak within k of the series edge must never be confirmed (not enough bars on one side)"
    print("4. A swing point too close to either edge of the series is never (falsely) confirmed: PASSED")


def _build_hh_hl_series():
    """A textbook 2-leg Higher-High/Higher-Low structure on purpose:
    SH1=12 (idx 2) -> SL1=10 (idx 5, higher than the base) -> SH2=15
    (idx 8, higher than SH1) -> SL2=11 (idx 11, higher than SL1) -> then
    price breaks back above SH2=15 at idx 14 - THIS is the breakout this
    module should detect. k=2 throughout for clean, easy-to-verify-by-
    hand confirmation timing."""
    highs = [8, 9, 12, 9, 8, 9, 11, 13, 15, 12, 11, 12, 13, 14, 16, 15, 14]
    lows = [7, 8, 10, 7, 6, 7, 10, 11, 13, 10, 9, 10, 11, 12, 14, 13, 12]
    closes = [7.5, 8.5, 11.5, 8.5, 7, 8, 10.5, 12, 14.5, 11, 10, 11, 12, 13.5, 15.5, 14.5, 13.5]
    return [float(x) for x in highs], [float(x) for x in lows], [float(x) for x in closes]


def test_5_hh_hl_breakout_detected_at_the_right_bar():
    highs, lows, closes = _build_hh_hl_series()
    events = ms.detect_hh_hl_breakouts(highs, lows, closes, k=2)
    assert len(events) == 1, events
    ev = events[0]
    assert ev.index == 14, f"expected the breakout to fire at bar 14 (close 15.5 > pivot 15), got {ev.index}"
    assert ev.pivot_price == 15.0
    assert ev.prior_swing_high == 12.0
    assert ev.swing_low_price == 9.0
    assert ev.prior_swing_low == 6.0
    print("5. A genuine 2-leg HH/HL structure's breakout fires at the exact bar price first crosses "
          "the confirmed pivot, with the right pivot/prior-swing values attached: PASSED")


def test_6_breakout_never_fires_twice_for_the_same_pivot():
    highs, lows, closes = _build_hh_hl_series()
    # Extend a few more bars that stay above the pivot without forming a
    # NEW higher confirmed swing high - must not re-fire.
    highs2 = highs + [15.5, 15.2, 15.8]
    lows2 = lows + [14.0, 14.0, 14.5]
    closes2 = closes + [15.2, 15.0, 15.6]
    events = ms.detect_hh_hl_breakouts(highs2, lows2, closes2, k=2)
    assert len(events) == 1, f"must not re-fire on later bars still sitting above the same pivot, got {events}"
    print("6. The same pivot never fires a second breakout event just because price stays above it: PASSED")


def test_7_no_breakout_without_both_hh_and_hl():
    # Swing highs here (confirmed at idx 2, 8, 14 with k=2) DO ascend
    # (12 -> 15 -> 16), but the two confirmed swing lows (idx 4 and 10)
    # are EQUAL (6 and 6, not ascending) - a higher-high without a
    # genuinely higher low is not a real Dow-Theory uptrend structure,
    # and must not fire a breakout no matter how clean the highs look.
    highs = [8, 9, 12, 9, 8, 9, 11, 13, 15, 12, 9, 10, 11, 12, 16, 15, 14]
    lows = [7, 8, 10, 7, 6, 7, 10, 11, 13, 10, 6, 7, 8, 9, 14, 13, 12]
    closes = [7.5, 8.5, 11.5, 8.5, 7, 8, 10.5, 12, 14.5, 11, 7, 8, 9, 10.5, 15.5, 14.5, 13.5]
    events = ms.detect_hh_hl_breakouts(
        [float(x) for x in highs], [float(x) for x in lows], [float(x) for x in closes], k=2
    )
    assert events == [], f"a higher-high without a genuinely higher low must not count as a confirmed uptrend structure, got {events}"
    print("7. A higher swing high WITHOUT a corresponding (strictly) higher swing low never fires a breakout: PASSED")


def test_8_true_range_series_basic():
    highs = [10.0, 12.0, 11.0]
    lows = [9.0, 10.5, 9.5]
    closes = [9.5, 11.5, 10.0]
    tr = ms.true_range_series(highs, lows, closes)
    assert tr[0] == 1.0  # bar 0: just high-low
    # bar 1: max(12-10.5=1.5, |12-9.5|=2.5, |10.5-9.5|=1.0) = 2.5
    assert tr[1] == 2.5, tr[1]
    # bar 2: max(11-9.5=1.5, |11-11.5|=0.5, |9.5-11.5|=2.0) = 2.0
    assert tr[2] == 2.0, tr[2]
    print("8. True Range is computed correctly including the previous-close gap terms: PASSED")


def test_9_coil_tightness_score_detects_a_real_contraction():
    # 60 baseline bars of wide true range (~4.0), then 12 bars of a tight
    # coil (~0.5) right before the breakout at index 72.
    n_baseline, n_coil = 60, 12
    highs, lows, closes = [], [], []
    price = 100.0
    for _ in range(n_baseline):
        highs.append(price + 2.0); lows.append(price - 2.0); closes.append(price)
        price += 0.1
    for _ in range(n_coil):
        highs.append(price + 0.25); lows.append(price - 0.25); closes.append(price)
        price += 0.05
    breakout_index = len(closes)
    highs.append(price + 0.25); lows.append(price - 0.25); closes.append(price)  # the breakout bar itself

    score = ms.coil_tightness_score(highs, lows, closes, breakout_index, coil_bars=n_coil, baseline_bars=n_baseline)
    assert score is not None
    assert score > 0.7, f"a genuine ~8x range contraction should score high, got {score}"
    print(f"9. A genuine pre-breakout range contraction scores high coil-tightness ({score:.2f}): PASSED")


def test_10_coil_tightness_score_low_when_no_contraction():
    n_baseline, n_coil = 60, 12
    highs, lows, closes = [], [], []
    price = 100.0
    for _ in range(n_baseline + n_coil):
        highs.append(price + 2.0); lows.append(price - 2.0); closes.append(price)
        price += 0.1
    breakout_index = len(closes)
    highs.append(price + 2.0); lows.append(price - 2.0); closes.append(price)

    score = ms.coil_tightness_score(highs, lows, closes, breakout_index, coil_bars=n_coil, baseline_bars=n_baseline)
    assert score is not None
    assert score < 0.2, f"uniform range (no contraction at all) should score near zero, got {score}"
    print(f"10. Uniform range with no real contraction scores near-zero coil-tightness ({score:.2f}): PASSED")


def test_11_coil_tightness_none_when_not_enough_history():
    highs = [10.0] * 20
    lows = [9.0] * 20
    closes = [9.5] * 20
    assert ms.coil_tightness_score(highs, lows, closes, breakout_index=10, coil_bars=12, baseline_bars=60) is None
    print("11. Not enough history for the baseline window correctly returns None, not a misleading score: PASSED")


def test_12_rvol_score_basic():
    # bars_per_day=4; breakout is at the SAME slot (slot 1) as 5 prior
    # days, each with volume 100 at that slot - breakout volume 250 ->
    # RVOL = 2.5.
    bars_per_day = 4
    volumes = []
    for day in range(6):
        volumes.extend([50.0, 100.0, 70.0, 60.0])
    volumes[-3] = 250.0  # override day 5's slot-1 volume to be the "breakout" bar
    breakout_index = len(volumes) - 3
    score = ms.rvol_score(volumes, breakout_index, bars_per_day=bars_per_day, min_days=5)
    assert score is not None
    assert abs(score - 2.5) < 0.01, score
    print(f"12. RVOL correctly compares the breakout bar's volume only against the SAME time-of-day slot ({score:.2f}x): PASSED")


def test_13_rvol_score_none_when_too_few_prior_days():
    volumes = [10.0, 20.0, 30.0, 40.0, 999.0]
    assert ms.rvol_score(volumes, breakout_index=4, bars_per_day=4, min_days=5) is None
    print("13. RVOL correctly returns None rather than a misleading ratio when too few prior same-slot bars exist: PASSED")


def test_14_freshness_score_decays_linearly():
    assert ms.freshness_score(breakout_index=10, current_index=10, decay_bars=12) == 1.0
    assert ms.freshness_score(breakout_index=10, current_index=16, decay_bars=12) == 0.5
    assert ms.freshness_score(breakout_index=10, current_index=22, decay_bars=12) == 0.0
    assert ms.freshness_score(breakout_index=10, current_index=100, decay_bars=12) == 0.0, \
        "must clamp at 0, never go negative"
    print("14. Freshness score is 1.0 right at the breakout, decays linearly, and clamps at 0: PASSED")


def test_15_extension_score_penalizes_a_chased_move():
    # atr_at_breakout = 2.0, cap = 2.0 ATRs
    assert ms.extension_score(pivot_price=100.0, current_price=100.0, atr_at_breakout=2.0) == 1.0
    assert ms.extension_score(pivot_price=100.0, current_price=102.0, atr_at_breakout=2.0) == 0.5
    assert ms.extension_score(pivot_price=100.0, current_price=104.0, atr_at_breakout=2.0) == 0.0
    assert ms.extension_score(pivot_price=100.0, current_price=99.0, atr_at_breakout=2.0) == 1.0, \
        "price still at/below the pivot should score the full 1.0, not a meaningless negative extension"
    print("15. Extension score is 1.0 right at the pivot, decays over the ATR cap, and never penalizes below zero: PASSED")


def test_16_composite_score_combines_and_handles_missing_components():
    score = ms.composite_momentum_score(coil=0.9, freshness=1.0, rvol=2.0, extension=1.0)
    assert score is not None
    assert 0.95 <= score <= 1.0, f"near-max on every component should score near 1.0, got {score}"

    low_score = ms.composite_momentum_score(coil=0.1, freshness=0.0, rvol=0.5, extension=0.2)
    assert low_score is not None and low_score < score, (low_score, score)

    assert ms.composite_momentum_score(coil=None, freshness=1.0, rvol=2.0, extension=1.0) is None
    assert ms.composite_momentum_score(coil=0.8, freshness=1.0, rvol=None, extension=1.0) is None
    print("16. The composite score rewards strong components, ranks a weak setup lower, and returns None "
          "(never a misleadingly-low number) when coil/RVOL couldn't be computed at all: PASSED")


def main():
    print("=== Swing momentum-signal (HH/HL composite score) test suite ===\n")
    test_1_fractal_swing_high_basic()
    test_2_fractal_swing_low_basic()
    test_3_no_swings_in_a_monotonic_series()
    test_4_edges_never_confirmed()
    test_5_hh_hl_breakout_detected_at_the_right_bar()
    test_6_breakout_never_fires_twice_for_the_same_pivot()
    test_7_no_breakout_without_both_hh_and_hl()
    test_8_true_range_series_basic()
    test_9_coil_tightness_score_detects_a_real_contraction()
    test_10_coil_tightness_score_low_when_no_contraction()
    test_11_coil_tightness_none_when_not_enough_history()
    test_12_rvol_score_basic()
    test_13_rvol_score_none_when_too_few_prior_days()
    test_14_freshness_score_decays_linearly()
    test_15_extension_score_penalizes_a_chased_move()
    test_16_composite_score_combines_and_handles_missing_components()
    print("\nALL SWING MOMENTUM-SIGNAL CHECKS PASSED")


if __name__ == "__main__":
    main()
