"""
Tests for Bollinger/signals.py's pending-order state machine
(_replay_pending_order_loop) - the one genuinely new/risky piece ported
from backtest_bollinger_vortex_9symbols_30day.py into this package's live
signal computation (see Bollinger/signals.py's own module docstring for
the "must replay the FULL retained series every cycle, never a short
window" correctness requirement this covers).

Exercises the state machine directly against hand-crafted valid_bullish/
valid_bearish/swing_high/swing_low arrays, independent of the real BB/
Vortex math that derives those arrays in production (already validated
via the backtest's own reviewed 30-day results) - this isolates exactly
the part of the port that's genuinely new: the pullback-arming/fire/
cancel logic itself.

Coverage:
  1. A clean 2-candle pullback after a confirmed swing high, followed by
     a break back above the swing high, FIRES on that breakout bar.
  2. A single-candle pullback (only 1 down-close) does NOT arm anything -
     MIN_PULLBACK_CANDLES=2 requires at least 2 consecutive counter-trend
     closes, matching the video's own "does not qualify" example.
  3. A trend flip (valid_bullish -> False) while an order is armed
     CANCELS it - a later break above the old trigger price must NOT fire.
  4. A fire that happened several bars ago must NOT keep re-appearing on
     later bars - only the LAST bar's own outcome is ever "fired".

HOW TO RUN:
    uv run python tests/test_bollinger_signals.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Bollinger.signals import _replay_pending_order_loop  # noqa: E402

LOOKBACK = 2
MIN_PULLBACK = 2


def _bars(n):
    """Flat baseline OHLC + all-False valid/swing arrays, n bars - tests
    overwrite exactly the bars they care about."""
    highs = [100.0] * n
    lows = [99.0] * n
    closes = [99.5] * n
    valid_bullish = [False] * n
    valid_bearish = [False] * n
    swing_high = [False] * n
    swing_low = [False] * n
    return highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low


def test_1_clean_pullback_then_breakout_fires():
    """Bar 5: confirmed swing high at price 110 (valid_bullish all along).
    Bars 6-7: two consecutive lower closes (the "genuine pullback").
    Bar 8: price breaks back above 110 -> should FIRE BULLISH on bar 8,
    with trigger_price=110 (the swing high) and stop_price = the lowest
    low reached during the pullback (bar 7's low)."""
    # n=9 (indices 0-8) so the fire bar (8) IS the array's last index -
    # _replay_pending_order_loop only ever reports a fire that happens on
    # the NEWEST bar (see this module's own docstring for why).
    n = 9
    highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low = _bars(n)
    for i in range(n):
        valid_bullish[i] = True
    swing_high[5] = True
    highs[5], closes[5], lows[5] = 110.0, 108.0, 107.0
    # Confirmed at bar 5+LOOKBACK=7 (see compute_fractal_swings' own lag).
    closes[6], lows[6] = 106.0, 105.0   # down-streak candle 1 (relative to swing bar's close)
    closes[7], lows[7] = 104.0, 103.0   # down-streak candle 2 -> arms here (confirmation bar)
    highs[8], closes[8] = 111.0, 110.5  # breaks back above 110 on the LAST bar -> FIRES

    pending, fired = _replay_pending_order_loop(
        highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low, LOOKBACK, MIN_PULLBACK,
    )
    assert fired is not None, "expected a BULLISH fire on the breakout bar, got no fire at all"
    side, trigger_price, stop_price = fired
    assert side == "BULLISH", f"expected BULLISH, got {side!r}"
    assert trigger_price == 110.0, f"expected trigger_price=110.0 (the swing high), got {trigger_price}"
    assert stop_price == 103.0, f"expected stop_price=103.0 (lowest low during the pullback), got {stop_price}"
    print("1. Clean 2-candle pullback then breakout FIRES with the correct trigger/stop: PASSED")


def test_2_single_candle_pullback_does_not_arm():
    """Same setup as test 1, but only ONE down-close before price
    immediately breaks back above the swing high - MIN_PULLBACK_CANDLES=2
    means this must NOT arm/fire, matching the video's own explicit
    'a single opposite candle does not qualify' example."""
    # n=8 (indices 0-7) so the "breaks back above immediately" bar (7) IS
    # the array's last index.
    n = 8
    highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low = _bars(n)
    for i in range(n):
        valid_bullish[i] = True
    swing_high[5] = True
    highs[5], closes[5], lows[5] = 110.0, 108.0, 107.0
    closes[6], lows[6] = 106.0, 105.0   # only ONE down-close
    highs[7], closes[7] = 111.0, 110.5  # breaks back above 110 immediately, on the LAST bar - should NOT fire

    pending, fired = _replay_pending_order_loop(
        highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low, LOOKBACK, MIN_PULLBACK,
    )
    assert fired is None, f"a single-candle pullback must NOT fire an entry, got {fired}"
    assert pending is None, f"a single-candle pullback must NOT even arm a pending order, got {vars(pending) if pending else None}"
    print("2. A single-candle pullback does NOT arm or fire (matches the video's own 'does not qualify' example): PASSED")


def test_3_trend_flip_cancels_pending_order():
    """Same armed setup as test 1, but the trend filter flips to NOT
    valid_bullish before price breaks back above the trigger - the
    pending order must be CANCELLED, and the later break above the old
    trigger price must NOT fire (the video's own 'if the trend
    invalidates, there is simply no trade' rule)."""
    # n=9 (indices 0-8) so the "would have fired" bar (8) IS the array's
    # last index.
    n = 9
    highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low = _bars(n)
    for i in range(8):
        valid_bullish[i] = True
    # valid_bullish flips False on bar 8 - trend invalidated right after
    # the pullback armed the order (bar 7).
    swing_high[5] = True
    highs[5], closes[5], lows[5] = 110.0, 108.0, 107.0
    closes[6], lows[6] = 106.0, 105.0
    closes[7], lows[7] = 104.0, 103.0   # arms here, same as test 1
    highs[8], closes[8] = 111.0, 110.5  # would have fired, but valid_bullish[8] is now False -> cancelled

    pending, fired = _replay_pending_order_loop(
        highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low, LOOKBACK, MIN_PULLBACK,
    )
    assert fired is None, f"a trend flip must cancel the pending order before it can fire, got {fired}"
    assert pending is None, f"the pending order must be cleared once the trend filter flips, got {vars(pending) if pending else None}"
    print("3. A trend flip cancels an armed pending order before it can fire: PASSED")


def test_4_stale_fire_does_not_keep_reappearing():
    """Fire happens on bar 8 (same as test 1), then 3 more bars pass with
    nothing new happening - the fire must be reported ONLY as the outcome
    of a replay whose LAST bar is bar 8, never on a later replay whose
    last bar is 9, 10, or 11 (a stale historical fire must never be
    re-attempted as if it just happened)."""
    n = 12
    highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low = _bars(n)
    for i in range(n):
        valid_bullish[i] = True
    swing_high[5] = True
    highs[5], closes[5], lows[5] = 110.0, 108.0, 107.0
    closes[6], lows[6] = 106.0, 105.0
    closes[7], lows[7] = 104.0, 103.0
    highs[8], closes[8] = 111.0, 110.5  # fires here
    highs[9], closes[9], lows[9] = 111.0, 110.5, 110.0
    highs[10], closes[10], lows[10] = 111.0, 110.5, 110.0
    highs[11], closes[11], lows[11] = 111.0, 110.5, 110.0

    # Replaying the FULL 12-bar series (bar 8's fire is now 3 bars in the
    # past) must NOT report it as fired on the newest (11th) bar.
    pending, fired = _replay_pending_order_loop(
        highs, lows, closes, valid_bullish, valid_bearish, swing_high, swing_low, LOOKBACK, MIN_PULLBACK,
    )
    assert fired is None, f"a fire from 3 bars ago must not still be reported as 'fired' on the newest bar, got {fired}"

    # But replaying only up through bar 8 (i.e. bar 8 IS the newest bar)
    # must still report the very same fire test 1 already confirmed.
    pending_at_8, fired_at_8 = _replay_pending_order_loop(
        highs[:9], lows[:9], closes[:9], valid_bullish[:9], valid_bearish[:9],
        swing_high[:9], swing_low[:9], LOOKBACK, MIN_PULLBACK,
    )
    assert fired_at_8 is not None and fired_at_8[0] == "BULLISH", \
        f"expected the same BULLISH fire when bar 8 IS the newest bar, got {fired_at_8}"
    print("4. A stale fire from earlier bars is never re-reported on a later replay - only genuinely new fires "
          "on the newest bar are actionable: PASSED")


if __name__ == "__main__":
    test_1_clean_pullback_then_breakout_fires()
    test_2_single_candle_pullback_does_not_arm()
    test_3_trend_flip_cancels_pending_order()
    test_4_stale_fire_does_not_keep_reappearing()
    print("\nAll tests passed.")
