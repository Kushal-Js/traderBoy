"""
Tests for structure_break.py's ws_candles_fn hook (added 24 Sep 2026, user
request: "add WS feeds for COPPER should also use in structure_break.py...
just switch from REST to WS (REST for fallback)").

fetch_timeframe now tries an OPTIONAL caller-injected ws_candles_fn(symbol,
interval_minutes) -> candles_dict_or_None BEFORE its existing REST fetch,
for any intraday (non-daily) timeframe - structure_break.py itself stays
import-free of the Swing package (the hook is injected, not a direct
Swing.candle_feed import), so its own CLI and backtest_swing_structure_
break_mtf.py (neither of which passes a hook) are byte-for-byte unaffected.

Covers:
  1. ws_candles_fn returning enough bars is used directly - REST is never
     called at all.
  2. ws_candles_fn returning None falls through to REST (not fresh/
     subscribed yet).
  3. ws_candles_fn returning too FEW bars (< params.atr_len + 1) also
     falls through to REST - a thin WS series must not be trusted just
     because it exists.
  4. ws_candles_fn RAISING falls through to REST too - a bug in the hook
     itself must never break the signal, only cost the REST fallback.
  5. ws_candles_fn=None (the default, what the CLI/backtest script still
     pass) behaves byte-identically to before this change - REST-only.
  6. Swing/signals.py's own _structure_break_ws_candles wrapper: returns
     None when candle_feed isn't fresh, returns the real candles dict
     when it is.

HOW TO RUN:
    uv run python tests/test_structure_break_ws_fallback.py
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import Options.dhan_client as odc
import structure_break as sb

IST = odc.IST
W = odc.dhan_wrapper


def _make_series(n: int, start_price: float = 100.0, interval_minutes: int = 5) -> dict:
    """A long-enough-to-warm-up (>=34-period basis + 14-period ATR),
    mildly trending synthetic OHLCV series - real candle shape, not flat
    (flat highs==lows would divide-by-zero in compute_structure_break's
    own CLV math)."""
    start = datetime(2026, 9, 1, 9, 15, tzinfo=IST)
    opens, highs, lows, closes, volumes, timestamps = [], [], [], [], [], []
    price = start_price
    for i in range(n):
        price += 0.3
        o = price - 0.1
        c = price + 0.1
        h = c + 0.5
        l = o - 0.5
        opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        volumes.append(1000.0 + i)
        timestamps.append(int((start + timedelta(minutes=interval_minutes * i)).timestamp()))
    return {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes, "timestamp": timestamps}


def _install_equity_mocks():
    """Auth/instrument-resolution mocks shared by every test below -
    NSE equity path (mcx=False) keeps the setup simple; the ws_candles_fn
    hook's own behavior doesn't depend on mcx vs equity resolution."""
    saved_client, saved_eqid = W._client, W._equity_security_id
    W._client = object()  # non-None -> _ensure_authenticated() is a no-op
    W._equity_security_id = lambda sym: "SID"
    return saved_client, saved_eqid


def _restore_equity_mocks(saved):
    W._client, W._equity_security_id = saved


def test_1_ws_candles_fn_used_directly_rest_never_called():
    saved = _install_equity_mocks()
    saved_fetch = W.fetch_continuous_intraday
    try:
        rest_calls = []
        W.fetch_continuous_intraday = lambda *a, **k: rest_calls.append(1) or {}
        ws_data = _make_series(60)

        def ws_fn(symbol, interval_minutes):
            assert interval_minutes == 5
            return ws_data

        r = sb.fetch_timeframe("TESTSTOCK", "5m", ws_candles_fn=ws_fn)
        assert not r.error, r.error
        assert r.warm, "60 real bars should be enough to warm up the 34-period basis"
        assert rest_calls == [], f"REST must never be called when ws_candles_fn supplies enough bars, got {rest_calls}"
        print("1. ws_candles_fn returning enough bars is used directly - REST never called: PASSED")
    finally:
        W.fetch_continuous_intraday = saved_fetch
        _restore_equity_mocks(saved)


def test_2_ws_candles_fn_returns_none_falls_through_to_rest():
    saved = _install_equity_mocks()
    saved_fetch = W.fetch_continuous_intraday
    try:
        rest_calls = []
        ws_data = _make_series(60)

        def fake_fetch(security_id, exchange_segment, instrument_type, interval, lookback_days_override=None):
            rest_calls.append(interval)
            return ws_data
        W.fetch_continuous_intraday = fake_fetch

        r = sb.fetch_timeframe("TESTSTOCK", "5m", ws_candles_fn=lambda sym, iv: None)
        assert not r.error, r.error
        assert r.warm
        assert rest_calls == [5], f"expected exactly one REST fetch (interval=5) as the fallback, got {rest_calls}"
        print("2. ws_candles_fn returning None falls through to REST: PASSED")
    finally:
        W.fetch_continuous_intraday = saved_fetch
        _restore_equity_mocks(saved)


def test_3_ws_candles_fn_returns_too_few_bars_falls_through_to_rest():
    saved = _install_equity_mocks()
    saved_fetch = W.fetch_continuous_intraday
    try:
        rest_calls = []
        ws_data = _make_series(60)
        thin_ws_data = _make_series(5)  # well under params.atr_len(14)+1

        def fake_fetch(security_id, exchange_segment, instrument_type, interval, lookback_days_override=None):
            rest_calls.append(interval)
            return ws_data

        W.fetch_continuous_intraday = fake_fetch
        r = sb.fetch_timeframe("TESTSTOCK", "5m", ws_candles_fn=lambda sym, iv: thin_ws_data)
        assert not r.error, r.error
        assert rest_calls == [5], f"a too-thin WS series must not be trusted - expected REST fallback, got {rest_calls}"
        print("3. ws_candles_fn returning too few bars (< atr_len+1) falls through to REST: PASSED")
    finally:
        W.fetch_continuous_intraday = saved_fetch
        _restore_equity_mocks(saved)


def test_4_ws_candles_fn_raising_falls_through_to_rest():
    saved = _install_equity_mocks()
    saved_fetch = W.fetch_continuous_intraday
    try:
        rest_calls = []
        ws_data = _make_series(60)

        def fake_fetch(security_id, exchange_segment, instrument_type, interval, lookback_days_override=None):
            rest_calls.append(interval)
            return ws_data
        W.fetch_continuous_intraday = fake_fetch

        def raising_ws_fn(symbol, interval_minutes):
            raise RuntimeError("simulated bug in the WS hook")

        r = sb.fetch_timeframe("TESTSTOCK", "5m", ws_candles_fn=raising_ws_fn)
        assert not r.error, r.error
        assert rest_calls == [5], "a raising ws_candles_fn must never break the signal, only cost the REST fallback"
        print("4. ws_candles_fn raising an exception falls through to REST, no crash: PASSED")
    finally:
        W.fetch_continuous_intraday = saved_fetch
        _restore_equity_mocks(saved)


def test_5_no_ws_candles_fn_is_byte_identical_to_before():
    """The CLI (main()) and backtest_swing_structure_break_mtf.py never
    pass ws_candles_fn - proves omitting it entirely (the pre-24-Sep
    call shape) is completely unaffected by this change."""
    saved = _install_equity_mocks()
    saved_fetch = W.fetch_continuous_intraday
    try:
        rest_calls = []
        ws_data = _make_series(60)

        def fake_fetch(security_id, exchange_segment, instrument_type, interval, lookback_days_override=None):
            rest_calls.append(interval)
            return ws_data
        W.fetch_continuous_intraday = fake_fetch

        r = sb.fetch_timeframe("TESTSTOCK", "5m")  # no ws_candles_fn at all
        assert not r.error, r.error
        assert r.warm
        assert rest_calls == [5]
        print("5. Omitting ws_candles_fn entirely (existing CLI/backtest call shape) is REST-only, unchanged: PASSED")
    finally:
        W.fetch_continuous_intraday = saved_fetch
        _restore_equity_mocks(saved)


def test_6_swing_signals_ws_candles_wrapper():
    import Swing.config as sc
    import Swing.signals as signals

    saved_use_ws = sc.USE_WS_CANDLES
    saved_is_fresh = signals.candle_feed.is_fresh
    saved_get_candles = signals.candle_feed.get_candles_dict
    try:
        sc.USE_WS_CANDLES = True
        signals.candle_feed.is_fresh = lambda symbol, max_age: False
        result = signals._structure_break_ws_candles("COPPER", 5)
        assert result is None, "must return None (not an empty dict) when candle_feed isn't fresh"

        fake_candles = {"close": [1.0, 2.0]}
        signals.candle_feed.is_fresh = lambda symbol, max_age: True
        signals.candle_feed.get_candles_dict = lambda symbol, interval: fake_candles
        result2 = signals._structure_break_ws_candles("COPPER", 5)
        assert result2 is fake_candles, "must return candle_feed's own dict verbatim when fresh"

        sc.USE_WS_CANDLES = False
        result3 = signals._structure_break_ws_candles("COPPER", 5)
        assert result3 is None, "must return None when USE_WS_CANDLES is off, even if candle_feed itself is fresh"
        print("6. Swing/signals.py's _structure_break_ws_candles wrapper correctly gates on USE_WS_CANDLES "
              "and candle_feed.is_fresh: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_use_ws
        signals.candle_feed.is_fresh = saved_is_fresh
        signals.candle_feed.get_candles_dict = saved_get_candles


def test_7_fetch_one_structure_break_timeframe_subscribes_ws_and_passes_the_hook():
    """The actual live wiring: Swing/signals.py's _fetch_one_structure_
    break_timeframe (the ONLY real caller, via COPPER's structure-break
    refresh loop) must (a) trigger a WS-subscribe for the symbol (COPPER
    otherwise never reaches candle_feed.ensure_subscribed at all - see
    this function's own docstring) and (b) hand structure_break.
    fetch_timeframe its own _structure_break_ws_candles as ws_candles_fn,
    not leave it REST-only."""
    import Swing.config as sc
    import Swing.signals as signals

    saved_use_ws = sc.USE_WS_CANDLES
    saved_ensure_subscribed = signals.candle_feed.ensure_subscribed
    saved_mcx_contract = W.get_mcx_futures_contract
    saved_fetch_timeframe = sb.fetch_timeframe
    try:
        sc.USE_WS_CANDLES = True
        subscribe_calls = []
        signals.candle_feed.ensure_subscribed = lambda symbol, security_id, segment: subscribe_calls.append(
            (symbol, security_id, segment)
        )
        from Options.dhan_client import FuturesContract
        W.get_mcx_futures_contract = lambda sym: FuturesContract(
            trading_symbol=f"{sym} FUT", security_id="571298", lot_size=1, expiry_date=None,
        )
        signals._mcx_contract_cache.clear()

        fetch_timeframe_calls = []

        def fake_fetch_timeframe(symbol, timeframe, mcx=False, ws_candles_fn=None, **kw):
            fetch_timeframe_calls.append((symbol, timeframe, mcx, ws_candles_fn))
            return sb.StructureBreakResult(n=1, basis=[1.0], upper=[1.0], lower=[1.0], regime=[1],
                                            switch_up=[False], switch_down=[False], bull_retest=[False],
                                            bear_retest=[False], strength=[50], warm=True, last_regime=1)
        sb.fetch_timeframe = fake_fetch_timeframe

        result = signals._fetch_one_structure_break_timeframe("COPPER", "5m")
        assert result == 1

        assert ("COPPER", "571298", "MCX_COMM") in subscribe_calls, (
            f"expected a WS-subscribe side effect for COPPER, got {subscribe_calls}"
        )
        assert len(fetch_timeframe_calls) == 1
        _, _, mcx_arg, ws_fn_arg = fetch_timeframe_calls[0]
        assert mcx_arg is True
        assert ws_fn_arg is signals._structure_break_ws_candles, (
            "structure_break.fetch_timeframe must be called with Swing's own _structure_break_ws_candles "
            "as ws_candles_fn, not left REST-only"
        )
        print("7. _fetch_one_structure_break_timeframe WS-subscribes COPPER and passes "
              "_structure_break_ws_candles through to structure_break.fetch_timeframe: PASSED")
    finally:
        sc.USE_WS_CANDLES = saved_use_ws
        signals.candle_feed.ensure_subscribed = saved_ensure_subscribed
        W.get_mcx_futures_contract = saved_mcx_contract
        sb.fetch_timeframe = saved_fetch_timeframe
        signals._mcx_contract_cache.clear()


def main():
    print("=== structure_break.py ws_candles_fn (WS-first/REST-fallback) test suite ===\n")
    test_1_ws_candles_fn_used_directly_rest_never_called()
    test_2_ws_candles_fn_returns_none_falls_through_to_rest()
    test_3_ws_candles_fn_returns_too_few_bars_falls_through_to_rest()
    test_4_ws_candles_fn_raising_falls_through_to_rest()
    test_5_no_ws_candles_fn_is_byte_identical_to_before()
    test_6_swing_signals_ws_candles_wrapper()
    test_7_fetch_one_structure_break_timeframe_subscribes_ws_and_passes_the_hook()
    print("\nALL STRUCTURE_BREAK WS-FALLBACK TESTS PASSED")


if __name__ == "__main__":
    main()
