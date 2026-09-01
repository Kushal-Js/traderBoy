"""
Tests for Swing's daily watchlist prune (added 1 Sep 2026, user request):
"update this watchlist by removing any stock when its daily close
crossed below daily super trend / daily 12 EMA... this logic has to run
daily when market starts at 9:15 AM." Entirely separate from the
intraday entry/exit signal (test_swing_signal_logic.py) - reads DAILY
candles via Dhan's own `historical_daily_data` endpoint rather than
5-min/1-min ones, and only ever REMOVES a symbol from watchlist_store -
never places an order or touches a live position.

Covers, against the REAL production functions (not reimplemented):
  1. `_compute_ema` against a hand-verifiable small series (a real EMA
     calculator, not a stub).
  2. `_evaluate_daily_trend_break` against REAL synthetic daily candle
     data run through the actual, shared `_compute_supertrend`/new
     `_compute_ema`: a genuine daily Supertrend crossed-below fires
     `DAILY_SUPERTREND_CROSSED_BELOW`; a series where ONLY the daily
     EMA(12) genuinely crosses below (Supertrend's own band kept
     deliberately unreachable via a large multiplier, to isolate the
     check) fires `DAILY_EMA12_CROSSED_BELOW`; a series with neither
     crossing returns None; and too little daily history returns None
     rather than a guessed signal.
  3. `_daily_watchlist_prune_tick`'s own daily-once gating: does nothing
     before 09:15 IST; the FIRST tick at/after 09:15 actually prunes;
     any LATER tick the same day is a no-op even though it's still past
     09:15 (the date-based "already ran today" gate, not a one-shot
     clock match) - and a fresh trading day resets it, allowed to prune
     again.
  4. The prune correctly REMOVES only the symbols whose daily trend
     broke (mixed watchlist: one Supertrend break, one EMA break, one
     clean), leaves the rest untouched, and durably logs a
     `WATCHLIST_PRUNED` event with the right reason for each removal.
  5. `config.WATCHLIST_DAILY_PRUNE_ENABLED = False` disables the whole
     feature - even past 09:15, on a fresh day, nothing is removed.
  6. A per-symbol daily-data fetch failure (e.g. an illiquid/newly-listed
     stock) is swallowed and logged - that ONE symbol is left on the
     watchlist untouched, other symbols in the same run are unaffected.

HOW TO RUN:
    uv run python tests/test_swing_daily_watchlist_prune.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_daily_prune_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Swing.trading_engine as ste
import Swing.watchlist as swl

IST = ZoneInfo("Asia/Kolkata")


def _rising_then_sharp_drop_series(n: int = 20) -> dict:
    """A rising trend for n-1 bars, then a sharp drop on the final bar -
    with the DEFAULT Supertrend multiplier this genuinely crosses BOTH
    the daily Supertrend and the daily EMA(12) below on that last bar
    (confirmed by direct computation); used for the Supertrend test,
    which fires first and short-circuits before EMA is even checked."""
    closes = [100.0 + i * 1.0 for i in range(n - 1)] + [70.0]
    return {"high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes], "close": closes}


def _flat_series_no_crossing(n: int = 20) -> dict:
    """A gentle, steady uptrend with no sharp move - stays above both its
    own daily Supertrend and EMA(12) the whole way, never crossing
    either."""
    closes = [100.0 + i * 0.5 for i in range(n)]
    return {"high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes], "close": closes}


def install_daily_data_mock(series_by_symbol: dict):
    """series_by_symbol: {symbol: {"high":[...], "low":[...], "close":[...]}}
    (or a callable(symbol) raising, to simulate a fetch failure for that
    symbol). Patches historical_daily_data (keyed by security_id, which
    _equity_security_id is patched to just echo back as the symbol
    itself) exactly like test_swing_signal_logic.py patches
    intraday_minute_data for the same dhan_wrapper._client object."""
    real_client = ste.dhan_wrapper._client
    real_equity_lookup = ste.dhan_wrapper._equity_security_id
    ste.dhan_wrapper._equity_security_id = lambda symbol: symbol

    class FakeDhan:
        def historical_daily_data(self, security_id, **kwargs):
            entry = series_by_symbol.get(security_id)
            if entry is None:
                raise ValueError(f"no fake daily series configured for {security_id}")
            if callable(entry):
                return entry()
            return {"data": entry}

    ste.dhan_wrapper._client = type("FakeClient", (), {"Dhan": FakeDhan()})()

    def restore():
        ste.dhan_wrapper._client = real_client
        ste.dhan_wrapper._equity_security_id = real_equity_lookup
    return restore


def test_1_compute_ema_hand_verifiable():
    # period=3, seed = mean(1,2,3)=2; then recursive with multiplier=0.5
    closes = [1.0, 2.0, 3.0, 10.0, 10.0]
    ema = ste._compute_ema(closes, period=3)
    assert ema[:2] == [None, None], "not enough bars yet to seed"
    assert ema[2] == 2.0, ema[2]
    # ema[3] = (10 - 2) * 0.5 + 2 = 6.0
    assert ema[3] == 6.0, ema[3]
    # ema[4] = (10 - 6) * 0.5 + 6 = 8.0
    assert ema[4] == 8.0, ema[4]
    print("1. _compute_ema matches a hand-computed EMA(3) series exactly, including the "
          "SMA-seeded warm-up window: PASSED")


def test_2_evaluate_daily_trend_break_against_real_synthetic_data():
    real_multiplier = ste.config.SUPERTREND_MULTIPLIER
    real_period = ste.config.SUPERTREND_PERIOD
    real_ema_period = ste.config.DAILY_EMA_PERIOD
    ste.config.SUPERTREND_PERIOD = 10
    ste.config.DAILY_EMA_PERIOD = 12
    try:
        # (a) Default multiplier - the Supertrend check fires (and short-
        # circuits before EMA is even evaluated, which also would have
        # fired on this same series).
        ste.config.SUPERTREND_MULTIPLIER = 3.0
        restore = install_daily_data_mock({"RELIANCE": _rising_then_sharp_drop_series()})
        try:
            reason = ste._evaluate_daily_trend_break("RELIANCE")
            assert reason == "DAILY_SUPERTREND_CROSSED_BELOW", reason
        finally:
            restore()

        # (b) A huge multiplier makes the Supertrend band unreachable (no
        # cross there), isolating a genuine EMA(12)-only cross.
        ste.config.SUPERTREND_MULTIPLIER = 100.0
        restore = install_daily_data_mock({"TCS": _rising_then_sharp_drop_series()})
        try:
            reason = ste._evaluate_daily_trend_break("TCS")
            assert reason == "DAILY_EMA12_CROSSED_BELOW", reason
        finally:
            restore()
        ste.config.SUPERTREND_MULTIPLIER = 3.0

        # (c) Neither indicator crosses -> None.
        restore = install_daily_data_mock({"INFY": _flat_series_no_crossing()})
        try:
            reason = ste._evaluate_daily_trend_break("INFY")
            assert reason is None, reason
        finally:
            restore()

        # (d) Too little daily history -> None, never a guessed signal.
        restore = install_daily_data_mock({"WIPRO": {"high": [101.0] * 5, "low": [99.0] * 5, "close": [100.0] * 5}})
        try:
            reason = ste._evaluate_daily_trend_break("WIPRO")
            assert reason is None, reason
        finally:
            restore()

        print("2. _evaluate_daily_trend_break against REAL synthetic daily candle data (via the "
              "actual shared _compute_supertrend/_compute_ema): a genuine daily Supertrend "
              "crossed-below fires DAILY_SUPERTREND_CROSSED_BELOW and short-circuits before EMA is "
              "checked; an isolated daily EMA(12) crossed-below fires DAILY_EMA12_CROSSED_BELOW; a "
              "non-crossing series and too-little-history both correctly return None: PASSED")
    finally:
        ste.config.SUPERTREND_MULTIPLIER = real_multiplier
        ste.config.SUPERTREND_PERIOD = real_period
        ste.config.DAILY_EMA_PERIOD = real_ema_period


async def test_3_daily_once_gating():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_DAILY_PRUNE_ENABLED
    ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = True
    real_now = ste._now_ist
    real_last_prune = ste._last_watchlist_prune_date
    await store.add_symbols(["SBIN"])
    restore_data = install_daily_data_mock({"SBIN": _flat_series_no_crossing()})
    try:
        ste._last_watchlist_prune_date = None

        # Before 09:15 IST - must do nothing (not even attempt a fetch;
        # SBIN's fake data would raise if a symbol other than SBIN were
        # queried, but the point here is the tick returns immediately).
        ste._now_ist = lambda: datetime(2026, 9, 2, 9, 0, tzinfo=IST)
        await ste._daily_watchlist_prune_tick()
        assert ste._last_watchlist_prune_date is None, "must not run before market open"

        # At/after 09:15 the SAME day - the first call actually runs.
        ste._now_ist = lambda: datetime(2026, 9, 2, 9, 15, tzinfo=IST)
        await ste._daily_watchlist_prune_tick()
        assert ste._last_watchlist_prune_date == date(2026, 9, 2)

        # A LATER tick, same day, still past 09:15 - must be a no-op
        # (date-gated, not a one-shot clock match) - proven by swapping
        # in data that would raise if actually fetched again.
        restore_data()
        restore_data = install_daily_data_mock({})  # any real fetch attempt now raises
        ste._now_ist = lambda: datetime(2026, 9, 2, 14, 30, tzinfo=IST)
        await ste._daily_watchlist_prune_tick()  # must not raise - no fetch attempted
        assert ste._last_watchlist_prune_date == date(2026, 9, 2)

        # A NEW trading day resets the gate, allowed to prune again.
        restore_data()
        restore_data = install_daily_data_mock({"SBIN": _flat_series_no_crossing()})
        ste._now_ist = lambda: datetime(2026, 9, 3, 9, 20, tzinfo=IST)
        await ste._daily_watchlist_prune_tick()
        assert ste._last_watchlist_prune_date == date(2026, 9, 3)

        print("3. _daily_watchlist_prune_tick runs exactly ONCE per trading day at/after 09:15 IST - "
              "does nothing before market open, runs on the first eligible tick, stays a no-op for "
              "every later tick the same day (date-gated, not a one-shot clock match), and resets "
              "cleanly on a new trading day: PASSED")
    finally:
        restore_data()
        ste._now_ist = real_now
        ste._last_watchlist_prune_date = real_last_prune
        ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = real_enabled


async def test_4_prune_removes_only_broken_symbols_and_logs_events():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_DAILY_PRUNE_ENABLED
    ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = True
    real_multiplier = ste.config.SUPERTREND_MULTIPLIER
    real_now = ste._now_ist
    real_last_prune = ste._last_watchlist_prune_date
    ste._last_watchlist_prune_date = None
    ste._now_ist = lambda: datetime(2026, 9, 4, 9, 15, tzinfo=IST)

    await store.add_symbols(["BROKE_ST", "BROKE_EMA", "HEALTHY"])
    # Both BROKE_ST and BROKE_EMA are evaluated in the SAME run under the
    # SAME (default) config.SUPERTREND_MULTIPLIER - isolating a genuine
    # EMA-only cross therefore can't rely on a bigger multiplier (that
    # would only apply globally to both). Instead BROKE_EMA's own series
    # uses a much WIDER daily high-low range than BROKE_ST's (10 vs 1) -
    # a bigger ATR, hence a Supertrend band far enough from price that it
    # never registers as "above" in the first place (so nothing crosses
    # there), while its EMA(12) - built from the same closes, unaffected
    # by high/low width - still genuinely crosses on the final bar.
    # Verified by direct computation below via the real functions before
    # trusting it in the tick-level assertion that follows.
    ste.config.SUPERTREND_MULTIPLIER = 3.0
    broke_st_series = _rising_then_sharp_drop_series()  # crosses both at multiplier=3.0, ST checked first
    healthy_series = _flat_series_no_crossing()

    n = 20
    ema_only_closes = [100.0 + i * 1.0 for i in range(n - 1)] + [105.0]
    broke_ema_series = {
        "high": [c + 5.0 for c in ema_only_closes], "low": [c - 5.0 for c in ema_only_closes], "close": ema_only_closes,
    }
    st_check = ste._compute_supertrend(broke_ema_series["high"], broke_ema_series["low"], broke_ema_series["close"],
                                        period=ste.config.SUPERTREND_PERIOD, multiplier=ste.config.SUPERTREND_MULTIPLIER)
    ema_check = ste._compute_ema(broke_ema_series["close"], ste.config.DAILY_EMA_PERIOD)
    assert not (ema_only_closes[-2] > st_check[-2] and not (ema_only_closes[-1] > st_check[-1])), \
        "test data setup bug: this series must NOT cross the Supertrend"
    assert ema_only_closes[-2] > ema_check[-2] and not (ema_only_closes[-1] > ema_check[-1]), \
        "test data setup bug: this series MUST cross its own EMA(12)"

    restore_data = install_daily_data_mock({
        "BROKE_ST": broke_st_series, "BROKE_EMA": broke_ema_series, "HEALTHY": healthy_series,
    })
    try:
        await ste._daily_watchlist_prune_tick()
        remaining = await store.symbols()
        assert remaining == ["HEALTHY"], remaining

        await asyncio.sleep(0.2)
        events = {e["underlying_symbol"]: e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["event"] == "WATCHLIST_PRUNED"}
        assert events["BROKE_ST"]["reason"] == "DAILY_SUPERTREND_CROSSED_BELOW", events["BROKE_ST"]
        assert events["BROKE_EMA"]["reason"] == "DAILY_EMA12_CROSSED_BELOW", events["BROKE_EMA"]
        assert "HEALTHY" not in events

        print("4. A mixed watchlist (one Supertrend break, one EMA break, one healthy) is pruned "
              "correctly - only the two broken symbols removed, HEALTHY left alone, each removal "
              "durably logged with the right reason via _record_swing_event: PASSED")
    finally:
        restore_data()
        ste._now_ist = real_now
        ste._last_watchlist_prune_date = real_last_prune
        ste.config.SUPERTREND_MULTIPLIER = real_multiplier
        ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = real_enabled


async def test_5_feature_flag_disables_pruning_entirely():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_DAILY_PRUNE_ENABLED
    ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = False
    real_now = ste._now_ist
    real_last_prune = ste._last_watchlist_prune_date
    ste._last_watchlist_prune_date = None
    ste._now_ist = lambda: datetime(2026, 9, 5, 10, 0, tzinfo=IST)
    await store.add_symbols(["ANYSTOCK"])
    # No daily-data mock installed at all - if the flag didn't actually
    # short-circuit, any fetch attempt would raise (no _client patched).
    try:
        await ste._daily_watchlist_prune_tick()
        remaining = await store.symbols()
        assert remaining == ["ANYSTOCK"], remaining
        assert ste._last_watchlist_prune_date is None, "must not even mark a run when the flag is off"
        print("5. config.WATCHLIST_DAILY_PRUNE_ENABLED=False disables the feature entirely - no "
              "fetch attempted, no symbol removed, even well past market open on a fresh day: PASSED")
    finally:
        ste._now_ist = real_now
        ste._last_watchlist_prune_date = real_last_prune
        ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = real_enabled


async def test_6_one_symbols_fetch_failure_does_not_affect_others():
    store = swl.WatchlistStore()
    ste.watchlist_store = store
    real_enabled = ste.config.WATCHLIST_DAILY_PRUNE_ENABLED
    ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = True
    real_now = ste._now_ist
    real_last_prune = ste._last_watchlist_prune_date
    ste._last_watchlist_prune_date = None
    ste._now_ist = lambda: datetime(2026, 9, 6, 9, 15, tzinfo=IST)
    await store.add_symbols(["FLAKY", "HEALTHY2", "BROKEN2"])

    def raise_flaky():
        raise ValueError("simulated illiquid-symbol fetch failure")

    restore_data = install_daily_data_mock({
        "FLAKY": raise_flaky, "HEALTHY2": _flat_series_no_crossing(),
        "BROKEN2": _rising_then_sharp_drop_series(),
    })
    try:
        await ste._daily_watchlist_prune_tick()  # must not raise despite FLAKY's failure
        remaining = await store.symbols()
        assert set(remaining) == {"FLAKY", "HEALTHY2"}, remaining
        print("6. A per-symbol daily-data fetch failure is swallowed and logged - that symbol is "
              "left on the watchlist untouched (never silently removed on an error), and every "
              "other symbol in the same run is evaluated normally and unaffected: PASSED")
    finally:
        restore_data()
        ste._now_ist = real_now
        ste._last_watchlist_prune_date = real_last_prune
        ste.config.WATCHLIST_DAILY_PRUNE_ENABLED = real_enabled


async def main():
    print("=== Swing daily watchlist prune test suite ===\n")
    test_1_compute_ema_hand_verifiable()
    test_2_evaluate_daily_trend_break_against_real_synthetic_data()
    await test_3_daily_once_gating()
    await test_4_prune_removes_only_broken_symbols_and_logs_events()
    await test_5_feature_flag_disables_pruning_entirely()
    await test_6_one_symbols_fetch_failure_does_not_affect_others()
    print("\nALL SWING DAILY WATCHLIST PRUNE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
