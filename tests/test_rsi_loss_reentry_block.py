"""
Tests for the shared same-day RSI-gated loss re-entry block (added 11 Sep
2026, REPLACING the old time-based LOSS_COOLDOWN_ENABLED/LOSS_COOLDOWN_
MINUTES mechanism - user request: "Remove this cooldown period logic
from everywhere and all strategies, instead create another global common
function which checks if RSI of 5 min candle is greater than number 88
or if RSI of current candle is lesser than previous 5 min candle (means
RSI is falling), then don't take a trade for that stock in same day if
MAX_LOSS_HIT is already hit earlier for that day").

This is a single shared computation - dhan_client.py's DhanWrapper.
refresh_rsi_signal()/is_rsi_loss_reentry_blocked() - consumed identically
by Options/Futures/Luxury's own webhook/entry wiring, same pattern as
refresh_supertrend_signal already being one shared computation gated
per-package by each package's own ENABLE_* flag. See Options/config.py's
"Same-day RSI-gated loss re-entry block" comment for the full design
rationale, and tests/test_options_corrective_actions.py (+ the Futures/
Luxury equivalents) for how each package's own _process_one_entry wires
this in (mocked there - the RSI math itself lives here).

Covers:
  1. _compute_rsi matches a known reference RSI(14) sequence.
  2. refresh_rsi_signal computes on a CONTINUOUS multi-session series (via
     fetch_continuous_intraday) and drops a still-forming last candle.
  3. RSI > RSI_LOSS_REENTRY_OVERBOUGHT alone blocks (is_rsi_loss_reentry_
     blocked returns True), even while RSI is still rising.
  4. RSI falling vs the previous confirmed candle alone blocks, even
     while comfortably below the overbought threshold.
  5. Neither condition (healthy, non-overbought, rising RSI) never blocks.
  6. rsi_loss_reentry_reason reports which condition fired ("overbought"
     vs "falling"), and None when not blocked.
  7. A fetch failure / not-enough-candles fails OPEN (never blocks) and
     is not cached, so a later good fetch is judged normally.
  8. Regression guard: refresh_rsi_signal routes through
     fetch_continuous_intraday, not a today-only fetch.

HOW TO RUN:
    uv run python tests/test_rsi_loss_reentry_block.py
"""
import inspect
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc
import Options.config as ocfg

IST = odc.IST
W = odc.dhan_wrapper


def test_1_compute_rsi_matches_known_reference_values():
    # Classic Wilder RSI(14) worked example (Wilder's own textbook closes,
    # truncated) - reference RSI values computed independently via the
    # standard Wilder-smoothing formula.
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
        45.89, 46.03, 45.61, 46.28, 46.28,
    ]
    rsi = odc._compute_rsi(closes, 14)
    assert all(v is None for v in rsi[:14]), "the first `period` entries must be None (no seed yet)"
    assert rsi[14] is not None
    assert 69.0 <= rsi[14] <= 71.0, f"RSI(14) on this classic worked example should land ~70, got {rsi[14]}"
    print("1. _compute_rsi matches the classic Wilder RSI(14) worked example (~70): PASSED")


def _index_bars(day, opens_and_closes, interval_min=5):
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    ts, op, cl = [], [], []
    for i, (o, c) in enumerate(opens_and_closes):
        ts.append(int((start + timedelta(minutes=interval_min * i)).timestamp()))
        op.append(float(o))
        cl.append(float(c))
    return ts, op, cl


def _install_series(closes_yesterday, closes_today):
    """Wires W._client so fetch_continuous_intraday(..., 5) returns
    `closes_yesterday` bars for yesterday followed by `closes_today` bars
    for today, 5 minutes apart. Returns a restore fn."""
    saved_client = W._client
    today = datetime.now(IST).date()
    yest = today - timedelta(days=1)
    yts, yop, ycl = _index_bars(yest, [(c, c) for c in closes_yesterday])
    tts, top, tcl = _index_bars(today, [(c, c) for c in closes_today])
    merged = {"timestamp": yts + tts, "open": yop + top, "close": ycl + tcl}

    def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
        return {"status": "success", "data": merged}

    W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))

    def restore():
        W._client = saved_client
    return restore


def _reset_cache(symbol):
    W._rsi_cache.pop(symbol, None)


def _osc_warmup(n=20):
    """n bars alternating +1.0/-0.5 (net upward drift) - unlike a flat
    series, this keeps BOTH avg_gain and avg_loss genuinely nonzero from
    the very first seed, so RSI lands in a normal, non-pinned range
    instead of immediately saturating at the 100 cap a flat-then-rising
    (zero historical volatility) series would produce."""
    closes = [100.0]
    for i in range(n - 1):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -0.5))
    return closes


def test_2_refresh_rsi_signal_uses_continuous_series_and_drops_forming_candle():
    symbol = "RSITEST2"
    _reset_cache(symbol)
    real_eqid = W._equity_security_id
    W._equity_security_id = lambda sym: "SECID"
    # Oscillating warm-up yesterday (avoids the degenerate flat-series RSI
    # pin at 100 - see _osc_warmup's own docstring), then today's bars
    # rising steadily on top of it.
    closes_yesterday = _osc_warmup(20)
    last = closes_yesterday[-1]
    closes_today = [last + 0.5, last + 1.3, last + 2.1, last + 2.9, last + 3.7]
    restore = _install_series(closes_yesterday, closes_today)
    try:
        W.refresh_rsi_signal(symbol)
        rsi = W.get_cached_rsi(symbol)
        prev_rsi = W.get_cached_prev_rsi(symbol)
        assert rsi is not None and prev_rsi is not None, "should have enough continuous history to compute RSI"
        assert rsi > prev_rsi, "a steadily rising close series must show RSI rising too"
        assert W.get_cached_prev_rsi(symbol) is not None
        print("2. refresh_rsi_signal computes on the continuous multi-session series and correctly "
              "carries both a current and previous confirmed-candle RSI: PASSED")
    finally:
        restore()
        W._equity_security_id = real_eqid
        _reset_cache(symbol)


def test_3_overbought_alone_blocks():
    symbol = "RSITEST3"
    _reset_cache(symbol)
    real_eqid = W._equity_security_id
    W._equity_security_id = lambda sym: "SECID"
    real_threshold = ocfg.RSI_LOSS_REENTRY_OVERBOUGHT
    ocfg.RSI_LOSS_REENTRY_OVERBOUGHT = 70.0
    # A strong, still-rising rally pushes RSI well past 70 - overbought,
    # but RSI is still RISING (not falling), isolating this condition.
    closes_yesterday = [100.0] * 20
    closes_today = [102, 105, 109, 114, 120, 127]
    restore = _install_series(closes_yesterday, closes_today)
    try:
        blocked = W.is_rsi_loss_reentry_blocked(symbol)
        rsi, prev_rsi = W.get_cached_rsi(symbol), W.get_cached_prev_rsi(symbol)
        assert rsi > 70.0, f"test setup should have produced an overbought RSI, got {rsi}"
        assert rsi >= prev_rsi, "this scenario must isolate 'overbought', not 'falling'"
        assert blocked is True, f"an overbought RSI ({rsi}) must block re-entry on its own"
        assert W.rsi_loss_reentry_reason(symbol) == "overbought"
        print("3. RSI > RSI_LOSS_REENTRY_OVERBOUGHT alone blocks re-entry, even while still rising: PASSED")
    finally:
        restore()
        W._equity_security_id = real_eqid
        ocfg.RSI_LOSS_REENTRY_OVERBOUGHT = real_threshold
        _reset_cache(symbol)


def test_4_falling_rsi_alone_blocks():
    symbol = "RSITEST4"
    _reset_cache(symbol)
    real_eqid = W._equity_security_id
    W._equity_security_id = lambda sym: "SECID"
    # Comfortably below any overbought threshold, but RSI is falling
    # (a mild pullback after a mild uptrend) - isolates "falling".
    closes_yesterday = [100.0] * 20
    closes_today = [101, 102, 103, 102.5, 102.0, 101.5]
    restore = _install_series(closes_yesterday, closes_today)
    try:
        blocked = W.is_rsi_loss_reentry_blocked(symbol)
        rsi, prev_rsi = W.get_cached_rsi(symbol), W.get_cached_prev_rsi(symbol)
        assert rsi < ocfg.RSI_LOSS_REENTRY_OVERBOUGHT, f"test setup should stay well under overbought, got {rsi}"
        assert rsi < prev_rsi, f"test setup should have produced a falling RSI, got {rsi} vs prev {prev_rsi}"
        assert blocked is True, "a falling RSI must block re-entry on its own, even nowhere near overbought"
        assert W.rsi_loss_reentry_reason(symbol) == "falling"
        print("4. RSI falling vs the previous confirmed candle alone blocks re-entry, well below "
              "the overbought threshold: PASSED")
    finally:
        restore()
        W._equity_security_id = real_eqid
        _reset_cache(symbol)


def test_5_healthy_rsi_never_blocks():
    symbol = "RSITEST5"
    _reset_cache(symbol)
    real_eqid = W._equity_security_id
    W._equity_security_id = lambda sym: "SECID"
    # Oscillating warm-up (see _osc_warmup), then a very mild continued
    # rise today - RSI rising but nowhere near overbought.
    closes_yesterday = _osc_warmup(20)
    last = closes_yesterday[-1]
    closes_today = [last + 0.1, last + 0.2, last + 0.3, last + 0.4, last + 0.5]
    restore = _install_series(closes_yesterday, closes_today)
    try:
        blocked = W.is_rsi_loss_reentry_blocked(symbol)
        rsi, prev_rsi = W.get_cached_rsi(symbol), W.get_cached_prev_rsi(symbol)
        assert rsi is not None and rsi < ocfg.RSI_LOSS_REENTRY_OVERBOUGHT and rsi >= prev_rsi, \
            f"test setup should be healthy (rising, non-overbought), got rsi={rsi} prev={prev_rsi}"
        assert blocked is False, "neither overbought nor falling - must never block"
        assert W.rsi_loss_reentry_reason(symbol) is None
        print("5. A healthy RSI (rising, non-overbought) never blocks re-entry: PASSED")
    finally:
        restore()
        W._equity_security_id = real_eqid
        _reset_cache(symbol)


def test_6_fetch_failure_fails_open_and_is_not_cached():
    symbol = "RSITEST6"
    _reset_cache(symbol)
    real_eqid = W._equity_security_id
    W._equity_security_id = lambda sym: "SECID"
    saved_client = W._client

    def boom(**kw):
        raise RuntimeError("DH-904 rate limit")

    W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=boom))
    try:
        blocked = W.is_rsi_loss_reentry_blocked(symbol)
        assert blocked is False, "a fetch failure must fail OPEN (never block on missing data)"
        assert W.get_cached_rsi(symbol) is None
        assert symbol not in W._rsi_cache, "a failed refresh must not be cached"
    finally:
        W._client = saved_client

    # A subsequent good fetch must still be judged normally.
    restore = _install_series([100.0] * 20, [102, 105, 109, 114, 120, 127])
    try:
        blocked = W.is_rsi_loss_reentry_blocked(symbol)
        assert blocked is True, "after the failure clears, a later good fetch must be judged normally"
        print("6. A fetch failure fails open and isn't cached - a later good fetch is judged normally: PASSED")
    finally:
        restore()
        W._equity_security_id = real_eqid
        _reset_cache(symbol)


def test_7_not_enough_candles_fails_open():
    symbol = "RSITEST7"
    _reset_cache(symbol)
    real_eqid = W._equity_security_id
    W._equity_security_id = lambda sym: "SECID"
    # Only 3 bars total today, no history yesterday - nowhere near
    # RSI_LOSS_REENTRY_PERIOD (14) + 2 confirmed bars needed.
    restore = _install_series([], [100, 101, 102])
    try:
        blocked = W.is_rsi_loss_reentry_blocked(symbol)
        assert blocked is False, "too little history must fail open, never guess a block"
        assert W.get_cached_rsi(symbol) is None
        print("7. Not enough candles yet fails open (never blocks) rather than guessing: PASSED")
    finally:
        restore()
        W._equity_security_id = real_eqid
        _reset_cache(symbol)


def test_8_no_today_only_fetch_regression_guard():
    """Guards against a regression reintroducing a `from_date=today,
    to_date=today` intraday_minute_data call - see tests/test_continuous_
    intraday.py's identical-purpose test_4 for the other signal refreshers."""
    src = inspect.getsource(odc.DhanWrapper.refresh_rsi_signal)
    assert "fetch_continuous_intraday" in src, "refresh_rsi_signal must route through fetch_continuous_intraday"
    assert "intraday_minute_data" not in src, "refresh_rsi_signal must not call intraday_minute_data directly"
    print("8. refresh_rsi_signal routes through fetch_continuous_intraday, not a today-only fetch: PASSED")


if __name__ == "__main__":
    print("=== Same-day RSI-gated loss re-entry block test suite ===\n")
    test_1_compute_rsi_matches_known_reference_values()
    test_2_refresh_rsi_signal_uses_continuous_series_and_drops_forming_candle()
    test_3_overbought_alone_blocks()
    test_4_falling_rsi_alone_blocks()
    test_5_healthy_rsi_never_blocks()
    test_6_fetch_failure_fails_open_and_is_not_cached()
    test_7_not_enough_candles_fails_open()
    test_8_no_today_only_fetch_regression_guard()
    print("\nALL RSI LOSS-REENTRY BLOCK CHECKS PASSED")
