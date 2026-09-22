"""
Tests for underlying_candle_feed.py's _update_bar - the pure tick-to-5min-
bar bucketing function. Covers the 22 Sep 2026 live incident: a symbol's
first tick used to always reset the volume baseline to 0.0, correct only
if the subscription itself started at market open. A mid-day subscribe
(confirmed live: production subscribes a symbol whenever it first enters
the dispatcher's pool, any time in the session) instead had its first
bar's "volume" computed as the ENTIRE day's cumulative volume so far -
confirmed live as a 25-60x overshoot on every test symbol's first
reconstructed bar via /debug/underlying-feed/parity.

A completed bar's volume is always (cum_volume as of that bar's own LAST
tick) - (cum_volume baseline set when that bar opened) - a tick that
starts the NEXT bar does NOT contribute to the bar it closes, since that
tick's own volume belongs to the new bar instead. Every test below uses
at least 2 ticks per bar so this is unambiguous.

test_mid_day_subscribe_first_bar_volume_excludes_pre_subscription_volume
is the regression test for exactly this - deliberately NOT coverable by
backtest_ws_candle_reconstruction_parity.py's own REST-replay method,
which always starts from the beginning of a symbol's day (cum_volume
naturally ~0 at the first synthetic tick either way, the one condition
under which the old buggy assumption happened to be correct).

Also covers the 22 Sep 2026 disk-reconciliation feature (_persist_bar /
_load_persisted_bars / _restore_from_disk): a real unplanned restart that
day wiped this module's entire in-memory state, silently zeroing out a
live dry-run and making the automated parity check report a false
"recon_bar_count: 0" for every symbol - indistinguishable from a
genuinely broken feed. Every completed bar is now appended to
history/<date>_underlying_candles_<symbol>.log, and subscribe() restores
from those logs before the first live tick can arrive in a fresh
process.

HOW TO RUN:
    uv run python tests/test_underlying_candle_feed.py
"""
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import underlying_candle_feed as ucf  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _t(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 22, hour, minute, second, tzinfo=IST)


def test_1_true_day_open_subscribe_first_bar_volume_is_correct():
    """Subscribing right at market open (cum_volume ~0 already) must
    still work exactly as before - the fix must not regress this case."""
    st = ucf._SymbolState()
    assert ucf._update_bar(st, ltp=100.0, cum_volume=0.0, t=_t(9, 15, 5)) is None  # opens bar
    assert ucf._update_bar(st, ltp=101.0, cum_volume=500.0, t=_t(9, 15, 30)) is None  # last tick of this bar
    completed = ucf._update_bar(st, ltp=102.0, cum_volume=1200.0, t=_t(9, 20, 0))  # next bar starts, closes 9:15
    assert completed is not None
    assert completed["volume"] == 500.0, (
        f"true day-open subscribe: first bar's volume should be the cum_volume delta up to its OWN "
        f"last tick (500.0), not including the tick that opens the next bar (1200.0) - got {completed['volume']}"
    )
    print("1. True day-open subscribe (cum_volume ~0 at first tick): first bar volume correct: PASSED")


def test_2_mid_day_subscribe_first_bar_volume_excludes_pre_subscription_volume():
    """THE REGRESSION TEST for the 22 Sep 2026 live incident. Simulates
    subscribing at 10:00 IST, when the day's real cumulative volume is
    already substantial (e.g. 500,000 shares from 09:15-10:00) - the
    first tick's own cum_volume reflects that. The reconstructed first
    bar's volume must be ONLY what accumulates from the subscription
    moment onward, never the whole day's volume so far."""
    st = ucf._SymbolState()
    PRE_SUBSCRIPTION_VOLUME = 500_000.0  # everything that happened 09:15-10:00, before we were listening
    assert ucf._update_bar(st, ltp=250.0, cum_volume=PRE_SUBSCRIPTION_VOLUME, t=_t(10, 0, 3)) is None
    assert ucf._update_bar(st, ltp=251.0, cum_volume=PRE_SUBSCRIPTION_VOLUME + 800.0, t=_t(10, 0, 40)) is None
    # next 5-min bar starts (10:05) - this closes the 10:00 bar
    completed = ucf._update_bar(st, ltp=252.0, cum_volume=PRE_SUBSCRIPTION_VOLUME + 1500.0, t=_t(10, 5, 2))
    assert completed is not None
    assert completed["volume"] == 800.0, (
        f"mid-day subscribe: first bar's volume must exclude the 500,000 pre-subscription volume "
        f"(expected 800.0, the delta up to the bar's own last tick), got {completed['volume']} - if this "
        f"is anywhere near 500,000+, that's the exact 25-60x-overshoot bug confirmed live via "
        f"/debug/underlying-feed/parity"
    )
    assert completed["volume"] < 10_000.0, "sanity bound: must not include the 500,000 pre-subscription volume"
    print("2. Mid-day subscribe (cum_volume already 500,000 at first tick): first bar volume excludes "
          "pre-subscription volume, not the whole day: PASSED")


def test_3_second_and_later_bars_unaffected_by_the_fix():
    """The fix only changes the FIRST bar's baseline - every bar after
    that already computed its delta from the previous bar's own closing
    cum_volume_now, untouched by this change.

    Note the transition tick itself (the one whose timestamp falls in
    the NEW window, triggering the old bar's close) belongs to the new
    bar, not the old one: its cum_volume becomes the new bar's own
    baseline, so its own volume contribution is counted when THAT bar
    later closes, not discarded. b2's baseline is therefore 101,500 (the
    11:00 bar's closing cum_volume_now), not 102,000 (the transition
    tick's own reading)."""
    st = ucf._SymbolState()
    ucf._update_bar(st, ltp=100.0, cum_volume=100_000.0, t=_t(11, 0, 1))       # opens 11:00 bar
    ucf._update_bar(st, ltp=100.5, cum_volume=101_500.0, t=_t(11, 4, 0))       # last tick of 11:00 bar
    b1 = ucf._update_bar(st, ltp=101.0, cum_volume=102_000.0, t=_t(11, 5, 1))  # opens 11:05, closes 11:00
    ucf._update_bar(st, ltp=101.5, cum_volume=104_200.0, t=_t(11, 9, 0))       # last tick of 11:05 bar
    b2 = ucf._update_bar(st, ltp=102.0, cum_volume=105_000.0, t=_t(11, 10, 1))  # opens 11:10, closes 11:05
    assert b1["volume"] == 1_500.0, f"expected 1500.0 (101,500 - 100,000), got {b1['volume']}"
    assert b2["volume"] == 2_700.0, f"expected 2700.0 (104,200 - 101,500), got {b2['volume']}"
    print("3. Second and later bars compute their own delta correctly, unaffected by the fix: PASSED")


def test_4_day_rollover_resets_baseline_same_as_a_fresh_subscribe():
    """_on_tick's day-rollover path resets current_bar_start to None,
    which routes back through the same first-tick branch tested above -
    confirms the module-level entry point (not just the pure function)
    also gets the fix."""
    ucf._state.clear()
    ucf._on_tick("TESTSYM", ltp=50.0, cum_volume=300_000.0, t=_t(13, 30, 1))    # mid-day subscribe, day 1, opens bar
    ucf._on_tick("TESTSYM", ltp=50.2, cum_volume=300_200.0, t=_t(13, 33, 0))    # last tick of this bar
    ucf._on_tick("TESTSYM", ltp=50.5, cum_volume=300_500.0, t=_t(13, 35, 1))    # closes first bar
    st = ucf._state["TESTSYM"]
    assert st.bars[-1]["volume"] == 200.0, f"expected 200.0 (300,200 - 300,000), got {st.bars[-1]['volume']}"

    # Day rollover - new day, cum_volume resets low per Dhan's own day-relative semantics
    next_day = _t(13, 30, 1) + timedelta(days=1)
    ucf._on_tick("TESTSYM", ltp=51.0, cum_volume=10_000.0, t=next_day)                        # opens new-day bar
    ucf._on_tick("TESTSYM", ltp=51.2, cum_volume=10_600.0, t=next_day + timedelta(minutes=3))  # last tick
    ucf._on_tick("TESTSYM", ltp=51.5, cum_volume=10_800.0, t=next_day + timedelta(minutes=5))  # closes it
    st = ucf._state["TESTSYM"]
    assert st.bars[-1]["volume"] == 600.0, f"expected 600.0 (10,600 - 10,000), got {st.bars[-1]['volume']}"
    print("4. Day rollover routes through the same fixed first-tick logic (via _on_tick, not just "
          "_update_bar directly): PASSED")


def test_5_persist_bar_writes_a_restorable_jsonl_line():
    """_persist_bar/_load_persisted_bars round-trip: a completed bar
    written to disk must come back byte-for-byte the same (datetime
    included) via the read path, not just "some data"."""
    bar = {"candle_start": _t(10, 30, 0), "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 12345.0}
    ucf._persist_bar("ROUNDTRIP", date(2026, 9, 22), bar)
    loaded = ucf._load_persisted_bars("ROUNDTRIP", date(2026, 9, 22), lookback_days=0)
    assert len(loaded) == 1, f"expected exactly 1 restored bar, got {len(loaded)}"
    assert loaded[0]["candle_start"] == bar["candle_start"]
    assert loaded[0]["open"] == 100.0 and loaded[0]["close"] == 100.5 and loaded[0]["volume"] == 12345.0
    print("5. _persist_bar/_load_persisted_bars round-trip a completed bar exactly: PASSED")


def test_6_load_persisted_bars_spans_multiple_days_and_trims_to_max_kept():
    """Covers the 3-day lookback and the MAX_BARS_KEPT trim, both real
    behavior _load_persisted_bars promises, not just the single-day
    round-trip above."""
    sym = "MULTIDAY"
    day0 = date(2026, 9, 20)
    for i in range(3):
        ucf._persist_bar(sym, day0, {"candle_start": _t(9, 15 + i * 5, 0).replace(day=20),
                                      "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0})
    day1 = date(2026, 9, 22)  # 2 days later, still within the default 3-day lookback
    for i in range(200):  # deliberately more than MAX_BARS_KEPT
        ucf._persist_bar(sym, day1, {"candle_start": _t(9, 15, 0).replace(day=22) + timedelta(minutes=5 * i),
                                      "open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 200.0})
    loaded = ucf._load_persisted_bars(sym, day1, lookback_days=3)
    assert len(loaded) == ucf.MAX_BARS_KEPT, f"expected trimmed to MAX_BARS_KEPT ({ucf.MAX_BARS_KEPT}), got {len(loaded)}"
    assert all(b["close"] == 2.0 for b in loaded), (
        "the oldest (day0) bars should have been trimmed away by MAX_BARS_KEPT, keeping only the "
        "most recent ones from day1 - if any close==1.0 bars survived, trimming kept the wrong end"
    )
    print("6. _load_persisted_bars spans multiple days and trims to the most recent MAX_BARS_KEPT: PASSED")


def test_7_restart_reconciliation_end_to_end():
    """THE actual regression test for the 22 Sep 2026 incident: ticks
    complete real bars (persisted to disk as they go), then _state is
    wiped (simulating a process restart), then subscribe() is called
    again - the symbol's bars must come back from disk before any new
    tick arrives, not start cold."""
    sym = "RESTARTSYM"
    today = datetime.now(IST).date()
    ucf._on_tick(sym, ltp=500.0, cum_volume=50_000.0, t=_t(9, 15, 1))
    ucf._on_tick(sym, ltp=500.5, cum_volume=51_000.0, t=_t(9, 19, 0))
    ucf._on_tick(sym, ltp=501.0, cum_volume=52_000.0, t=_t(9, 20, 1))  # closes 09:15 bar, volume=1000
    assert len(ucf._state[sym].bars) == 1
    pre_restart_bars = list(ucf._state[sym].bars)

    # Simulate a process restart: wipe ALL in-memory state, same as a fresh process start.
    ucf._state.clear()
    ucf._subscribed.clear()
    ucf._tick_subscriber_registered = True  # skip the real dhan_wrapper subscribe call in this test

    class _FakeDhanWrapper:
        @staticmethod
        def subscribe_equity_quote(symbol):
            pass

    import Options.dhan_client as dhan_client_module
    saved = dhan_client_module.dhan_wrapper
    dhan_client_module.dhan_wrapper = _FakeDhanWrapper()
    try:
        ucf.subscribe([sym])
    finally:
        dhan_client_module.dhan_wrapper = saved

    assert sym in ucf._state, "subscribe() must restore state for a symbol with persisted history"
    restored_bars = ucf._state[sym].bars
    assert len(restored_bars) == 1, f"expected the 1 pre-restart bar restored, got {len(restored_bars)}"
    assert restored_bars[0]["volume"] == pre_restart_bars[0]["volume"] == 1_000.0
    assert restored_bars[0]["candle_start"] == pre_restart_bars[0]["candle_start"]

    # A new tick after "restart" must continue correctly - not duplicate the restored bar,
    # and must use the mid-day-subscribe-safe baseline (test_2) since current_bar_start is None again.
    ucf._on_tick(sym, ltp=502.0, cum_volume=52_500.0, t=_t(9, 21, 0))
    assert len(ucf._state[sym].bars) == 1, "a same-bar tick must not fabricate a new completed bar"
    print("7. Restart reconciliation end-to-end: persisted bars survive a full _state wipe, and new "
          "ticks after 'restart' continue correctly, no duplication: PASSED")


def main():
    tmp_history = Path(tempfile.mkdtemp(prefix="ucf_test_history_"))
    saved_history_dir = ucf.HISTORY_DIR
    ucf.HISTORY_DIR = tmp_history  # redirect ALL disk I/O in this run to a throwaway dir, never the real history/
    try:
        print("=== underlying_candle_feed._update_bar volume-baseline test suite ===\n")
        test_1_true_day_open_subscribe_first_bar_volume_is_correct()
        test_2_mid_day_subscribe_first_bar_volume_excludes_pre_subscription_volume()
        test_3_second_and_later_bars_unaffected_by_the_fix()
        test_4_day_rollover_resets_baseline_same_as_a_fresh_subscribe()
        test_5_persist_bar_writes_a_restorable_jsonl_line()
        test_6_load_persisted_bars_spans_multiple_days_and_trims_to_max_kept()
        test_7_restart_reconciliation_end_to_end()
        print("\nALL UNDERLYING_CANDLE_FEED TESTS PASSED")
    finally:
        ucf.HISTORY_DIR = saved_history_dir
        shutil.rmtree(tmp_history, ignore_errors=True)


if __name__ == "__main__":
    main()
