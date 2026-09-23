"""
Tests for Swing/candle_feed.py - the WS-based local 5-min/15-min candle
reconstruction added 23 Sep 2026 to stop Swing's regime/Supertrend REST
fetches from hitting Dhan's account-wide DH-904 rate limit (see that
module's own docstring for the full design this covers).

Reuses the exact same _update_bar/_on_tick correctness already proven by
tests/test_underlying_candle_feed.py (this module's bar-building
algorithm is a direct copy of that one, just parameterized differently),
so this file focuses on what's NEW here instead of re-deriving those:

  1. _resample: correct 5m->15m aggregation, and the still-forming
     trailing bucket is never included.
  2. Disk persistence is namespaced by security_id (not just symbol/day) -
     a restore never mixes two different MCX contracts' bars.
  3. ensure_subscribed: first-time subscribe, a no-op re-call with the
     same reference, and a detected MCX contract roll (security_id
     changed under the same symbol) resets in-memory state, unsubscribes
     the old contract, subscribes the new one, and does NOT load the old
     contract's persisted bars into the new one's series.
  4. is_fresh()/get_candles_dict() basic correctness.
  5. A concurrency stress test: real ticks arriving on one thread while
     ensure_subscribed is called repeatedly from others (mirroring the
     real call pattern - ticks on the WS feed's background thread,
     ensure_subscribed from every regime/Supertrend fetch's executor
     thread) - must never raise, deadlock, or corrupt the bar list.

HOW TO RUN:
    uv run python tests/test_swing_candle_feed.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
os.environ.setdefault("DHAN_ACCESS_TOKEN", "test")

import Swing.candle_feed as cf  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 23, h, m, s, tzinfo=IST)


class _FakeDhanWrapper:
    """Records every subscribe/unsubscribe call instead of touching any
    real WebSocket - ensure_subscribed's own local `from Options.
    dhan_client import dhan_wrapper` picks this up transparently once
    it's installed as the module attribute (see install()/restore() below)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.tick_subscribers: list = []

    def add_quote_tick_subscriber(self, callback) -> None:
        self.tick_subscribers.append(callback)
        self.calls.append(("add_quote_tick_subscriber",))

    def subscribe_equity_quote(self, symbol: str) -> None:
        self.calls.append(("subscribe_equity_quote", symbol))

    def unsubscribe_equity_quote(self, symbol: str) -> None:
        self.calls.append(("unsubscribe_equity_quote", symbol))

    def subscribe_mcx_quote(self, symbol: str, security_id: str) -> None:
        self.calls.append(("subscribe_mcx_quote", symbol, security_id))

    def unsubscribe_mcx_quote(self, symbol: str, security_id: str) -> None:
        self.calls.append(("unsubscribe_mcx_quote", symbol, security_id))


def _install_fake_dhan_wrapper() -> _FakeDhanWrapper:
    import Options.dhan_client as dc
    fake = _FakeDhanWrapper()
    dc.dhan_wrapper = fake
    return fake


def _reset_module_state() -> None:
    """ensure_subscribed/_on_tick mutate module-level globals - every test
    below must start from a clean slate, same discipline test_underlying_
    candle_feed.py's own main() uses for HISTORY_DIR."""
    with cf._lock:
        cf._state.clear()
        cf._subscribed_ref.clear()
    cf._tick_subscriber_registered = False


def _make_5m_bars(n: int, start_hour: int = 9, start_minute: int = 15) -> list[dict]:
    bars = []
    t = _t(start_hour, start_minute)
    for i in range(n):
        bars.append({
            "candle_start": t, "open": 100 + i, "high": 101 + i,
            "low": 99 + i, "close": 100.5 + i, "volume": 10.0,
        })
        minute = t.minute + 5
        t = t.replace(hour=t.hour + minute // 60, minute=minute % 60)
    return bars


def test_1_resample_aggregates_complete_15min_buckets_only():
    bars = _make_5m_bars(8)  # 09:15..09:50 - two complete 15m buckets + one incomplete trailing (2 of 3)
    out = cf._resample(bars, 15)
    assert len(out) == 2, f"expected 2 complete 15m buckets from 8 5m bars, got {len(out)}"
    assert out[0]["candle_start"] == _t(9, 15)
    assert out[0]["open"] == 100 and out[0]["close"] == 102.5 and out[0]["volume"] == 30.0
    assert out[0]["high"] == 103 and out[0]["low"] == 99
    assert out[1]["candle_start"] == _t(9, 30)
    # A trailing bucket with the FULL expected count (exactly 3 members, nothing incomplete about
    # it) must still be emitted even though it's the last group in the list.
    bars_exact = _make_5m_bars(9)  # 09:15..09:55, 9 bars = exactly 3 complete 15m buckets
    out_exact = cf._resample(bars_exact, 15)
    assert len(out_exact) == 3, f"a fully-complete trailing bucket must not be dropped, got {len(out_exact)}"
    print("1. _resample aggregates only complete 15m buckets, never a still-forming trailing one: PASSED")


def test_2_resample_5min_is_passthrough():
    bars = [{"candle_start": _t(9, 15), "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 5.0}]
    assert cf._resample(bars, 5) is bars
    print("2. _resample(interval=5) is a pure passthrough of the base series: PASSED")


def test_3_persist_and_restore_is_namespaced_by_security_id():
    sym, day = "TESTSYM", cf.datetime.now(IST).date()
    bar = {"candle_start": _t(9, 15), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}
    cf._persist_bar(sym, "SEC_A", day, bar)
    cf._persist_bar(sym, "SEC_B", day, {**bar, "open": 999.0})

    restored_a = cf._load_persisted_bars(sym, "SEC_A", day, lookback_days=3)
    restored_b = cf._load_persisted_bars(sym, "SEC_B", day, lookback_days=3)
    restored_c = cf._load_persisted_bars(sym, "SEC_C_NEVER_WRITTEN", day, lookback_days=3)

    assert len(restored_a) == 1 and restored_a[0]["open"] == 1.0
    assert len(restored_b) == 1 and restored_b[0]["open"] == 999.0
    assert restored_c == [], "a security_id that was never persisted must restore empty, never another one's bars"
    print("3. Persisted bars are namespaced by security_id - no cross-contract mixing on restore: PASSED")


def test_4_ensure_subscribed_first_time_and_idempotent_recall():
    fake = _install_fake_dhan_wrapper()
    _reset_module_state()
    sym = "ASHOKLEY"
    cf.ensure_subscribed(sym, "212", "NSE_EQ")
    assert cf._subscribed_ref[sym] == ("212", "NSE_EQ")
    assert sym in cf._state
    assert ("subscribe_equity_quote", sym) in fake.calls
    assert len(fake.tick_subscribers) == 1, "tick subscriber must be registered exactly once"

    calls_before = list(fake.calls)
    cf.ensure_subscribed(sym, "212", "NSE_EQ")  # identical reference again
    assert fake.calls == calls_before, "an unchanged reference must be a complete no-op, no re-subscribe"
    assert len(fake.tick_subscribers) == 1, "must never register the tick subscriber twice"
    print("4. ensure_subscribed: first-time subscribe + idempotent re-call is a true no-op: PASSED")


def test_5_ensure_subscribed_mcx_roll_resets_state_and_resubscribes():
    fake = _install_fake_dhan_wrapper()
    _reset_module_state()
    sym = "NATURALGAS"

    cf.ensure_subscribed(sym, "570750", "MCX_COMM")
    cf._on_tick(sym, ltp=100.0, cum_volume=1000.0, t=_t(9, 15, 0))
    cf._on_tick(sym, ltp=101.0, cum_volume=1100.0, t=_t(9, 20, 0))  # completes the 09:15 bar
    with cf._lock:
        assert len(cf._state[sym].bars) == 1, "one completed bar expected before the roll"

    cf.ensure_subscribed(sym, "580999", "MCX_COMM")  # contract rolled to a new security_id
    assert ("unsubscribe_mcx_quote", sym, "570750") in fake.calls, "the retired contract must be unsubscribed"
    assert ("subscribe_mcx_quote", sym, "580999") in fake.calls, "the new contract must be subscribed"
    with cf._lock:
        assert cf._state[sym].security_id == "580999"
        assert cf._state[sym].bars == [], (
            "a roll must reset accumulated bars - splicing the old contract's price series onto the "
            "new one's would silently corrupt every regime/Supertrend reading right after the roll"
        )

    # A tick under the new contract must accumulate fresh, with no leftover state from the old one.
    cf._on_tick(sym, ltp=50.0, cum_volume=10.0, t=_t(9, 25, 0))
    cf._on_tick(sym, ltp=51.0, cum_volume=20.0, t=_t(9, 30, 0))
    with cf._lock:
        assert len(cf._state[sym].bars) == 1
        assert cf._state[sym].bars[0]["open"] == 50.0
    print("5. ensure_subscribed detects an MCX contract roll, resets state, unsubscribes/resubscribes correctly: PASSED")


def test_6_roll_restore_never_loads_the_retired_contracts_bars():
    fake = _install_fake_dhan_wrapper()
    _reset_module_state()
    sym, day = "COPPER", cf.datetime.now(IST).date()

    cf.ensure_subscribed(sym, "OLD_CONTRACT", "MCX_COMM")
    cf._on_tick(sym, ltp=700.0, cum_volume=500.0, t=_t(9, 15, 0))
    cf._on_tick(sym, ltp=701.0, cum_volume=600.0, t=_t(9, 20, 0))
    with cf._lock:
        assert len(cf._state[sym].bars) == 1

    cf.ensure_subscribed(sym, "NEW_CONTRACT", "MCX_COMM")
    with cf._lock:
        assert cf._state[sym].bars == [], "roll must start the new contract cold, not restore the old contract's disk history"
    # And the OLD contract's file is still on disk, untouched, just no longer read for this symbol.
    assert cf._persist_path(sym, "OLD_CONTRACT", day).exists()
    print("6. A roll never restores the retired contract's own persisted bars from disk: PASSED")


def test_7_is_fresh_and_get_candles_dict():
    _reset_module_state()
    sym = "FRESHTEST"
    assert cf.is_fresh(sym, max_age_seconds=90) is False, "an unsubscribed symbol must never read as fresh"
    assert cf.get_candles_dict(sym, 5) == {}, "no bars yet - must be an empty dict, not an error"

    with cf._lock:
        cf._state[sym] = cf._SymbolState("SEC_X")
        cf._state[sym].last_tick_at = datetime.now(IST)
        cf._state[sym].bars = [
            {"candle_start": _t(9, 15), "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 5.0},
        ]
    assert cf.is_fresh(sym, max_age_seconds=90) is True
    assert cf.is_fresh(sym, max_age_seconds=0) is False, "zero tolerance must read a just-set tick as already stale"
    data = cf.get_candles_dict(sym, 5)
    assert data["close"] == [1.5] and len(data["timestamp"]) == 1
    print("7. is_fresh()/get_candles_dict() basic correctness: PASSED")


def test_8_concurrent_ticks_and_ensure_subscribed_never_crash_or_corrupt():
    """Real call pattern: the WS feed has exactly ONE background thread
    delivering ticks for every subscribed symbol (see candle_feed.py's
    own module docstring / dhan_client.py's _run_market_feed_forever -
    this is a real architectural invariant, not an assumption this test
    should relax), while every regime/Supertrend fetch's OWN executor
    thread concurrently calls ensure_subscribed (a cheap idempotent check
    on every single fetch cycle, per Swing/signals.py's
    _underlying_reference - and in production, Options/Futures/Luxury's
    OWN executor threads may also be running concurrently, though they
    never touch this module's state directly). Must never raise,
    deadlock, or leave bars in a torn/inconsistent state."""
    _install_fake_dhan_wrapper()
    _reset_module_state()
    sym = "STRESSTEST"
    cf.ensure_subscribed(sym, "STRESS_ID", "NSE_EQ")

    errors: list[Exception] = []
    stop = threading.Event()

    def tick_worker():
        t = _t(9, 15, 0)
        cum_vol = 0.0
        try:
            while not stop.is_set():
                cum_vol += 1.0
                cf._on_tick(sym, ltp=100.0 + cum_vol, cum_volume=cum_vol, t=t)
                t = t + cf.timedelta(seconds=1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def resubscribe_worker():
        try:
            for _ in range(2000):
                cf.ensure_subscribed(sym, "STRESS_ID", "NSE_EQ")  # same ref every time - should stay a no-op
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=tick_worker)]  # exactly one - matches the real single-feed-thread invariant
    threads += [threading.Thread(target=resubscribe_worker) for _ in range(5)]
    for th in threads:
        th.start()
    time.sleep(1.5)
    stop.set()
    for th in threads:
        th.join(timeout=10)
        assert not th.is_alive(), "a worker thread failed to finish - possible deadlock"

    assert not errors, f"concurrent access raised: {errors}"
    with cf._lock:
        st = cf._state[sym]
        # Internal consistency: every completed bar's volume must be non-negative (max(0.0, ...)
        # in _update_bar) and bars must stay sorted/within the trim cap - a real corruption (a
        # torn read/write racing the lock) would show up as a violation of one of these.
        assert len(st.bars) <= cf.MAX_BARS_KEPT
        for b in st.bars:
            assert b["volume"] >= 0.0
        starts = [b["candle_start"] for b in st.bars]
        assert starts == sorted(starts), "bars must remain in chronological order under concurrent access"
    print("8. Concurrent ticks + repeated ensure_subscribed calls: no crash, no deadlock, no corruption: PASSED")


def main():
    tmp_history = Path(tempfile.mkdtemp(prefix="swing_cf_test_history_"))
    saved_history_dir = cf.HISTORY_DIR
    cf.HISTORY_DIR = tmp_history
    import Options.dhan_client as dc
    saved_dhan_wrapper = dc.dhan_wrapper
    try:
        print("=== Swing/candle_feed.py test suite ===\n")
        test_1_resample_aggregates_complete_15min_buckets_only()
        test_2_resample_5min_is_passthrough()
        test_3_persist_and_restore_is_namespaced_by_security_id()
        test_4_ensure_subscribed_first_time_and_idempotent_recall()
        test_5_ensure_subscribed_mcx_roll_resets_state_and_resubscribes()
        test_6_roll_restore_never_loads_the_retired_contracts_bars()
        test_7_is_fresh_and_get_candles_dict()
        test_8_concurrent_ticks_and_ensure_subscribed_never_crash_or_corrupt()
        print("\nALL SWING CANDLE_FEED TESTS PASSED")
    finally:
        cf.HISTORY_DIR = saved_history_dir
        dc.dhan_wrapper = saved_dhan_wrapper
        _reset_module_state()
        shutil.rmtree(tmp_history, ignore_errors=True)


if __name__ == "__main__":
    main()
