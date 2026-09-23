"""
Tests for breakout_signal.py's record_alert() WS-subscribe coverage fix
(added 23 Sep 2026, user request - closes a real gap found via a live
audit the same day: a genuine Chartink-alerted symbol outside the
curated universe never got WS-subscribed, so it stayed on the REST-
capped single-snapshot path forever, even after the same-day WS-walk fix
landed - see record_alert's own updated docstring and trading-skills'
write-up for the full audit numbers: 37/143 pending symbols were
REST-only purely because nothing had ever subscribed them).

Covers:
  1. cfg given + BREAKOUT_USE_WS_CANDLES=True: every alerted symbol is
     WS-subscribed, using the SAME cleaned/uppercased form the watchlist
     itself stores (not the raw, possibly-mixed-case webhook payload) -
     a case mismatch here would silently make is_fresh()/get_candles_dict
     never find the symbol later, the exact kind of quiet bug that would
     look like "it's subscribed" while never actually being used.
  2. cfg given but BREAKOUT_USE_WS_CANDLES=False: no subscribe call -
     matches every OTHER WS-candle flag's default-off behavior.
  3. cfg=None (the 2 internal dispatcher/universe-seed call sites'
     existing pattern): no subscribe call from record_alert itself -
     unchanged, since those callers already subscribe separately.
  4. The watchlist itself is populated identically regardless of cfg -
     this fix must never change WHAT gets recorded, only whether a
     WS-subscribe side effect also happens.
  5. An empty/blank-only stocks list never calls subscribe (nothing to
     subscribe), same early-return discipline as before.

HOW TO RUN:
    uv run python tests/test_breakout_signal_record_alert_ws_subscribe.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

import breakout_signal as bs  # noqa: E402
import trade_history  # noqa: E402


def _fake_cfg(ws_on: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(BREAKOUT_USE_WS_CANDLES=ws_on)


def _install_fake_subscribe():
    """underlying_candle_feed is imported lazily INSIDE _ws_subscribe_best_effort
    (`import underlying_candle_feed`), so patching the real module's
    `subscribe` attribute before that import runs is enough - Python
    caches the module object, so the lazy import just returns the same
    (now-patched) module."""
    import underlying_candle_feed
    calls = []
    saved = underlying_candle_feed.subscribe

    def fake_subscribe(symbols):
        calls.append(list(symbols))

    underlying_candle_feed.subscribe = fake_subscribe
    return calls, saved


def _restore_subscribe(saved):
    import underlying_candle_feed
    underlying_candle_feed.subscribe = saved


def test_1_ws_on_subscribes_cleaned_uppercased_symbols():
    calls, saved = _install_fake_subscribe()
    try:
        asyncio.run(bs.record_alert("TestStrategy1", "CE", ["absl", "  reliance ", "TCS"], cfg=_fake_cfg(True)))
        assert len(calls) == 1, f"expected exactly one subscribe call, got {len(calls)}"
        assert calls[0] == ["ABSL", "RELIANCE", "TCS"], (
            f"must subscribe the SAME cleaned/uppercased form the watchlist stores, got {calls[0]}"
        )
        print("1. BREAKOUT_USE_WS_CANDLES=True: subscribes every alerted symbol, correctly cleaned/uppercased: PASSED")
    finally:
        _restore_subscribe(saved)


def test_2_ws_off_never_subscribes():
    calls, saved = _install_fake_subscribe()
    try:
        asyncio.run(bs.record_alert("TestStrategy2", "CE", ["ABSL"], cfg=_fake_cfg(False)))
        assert calls == [], "BREAKOUT_USE_WS_CANDLES=False must never trigger a subscribe call"
        print("2. BREAKOUT_USE_WS_CANDLES=False: no subscribe call: PASSED")
    finally:
        _restore_subscribe(saved)


def test_3_cfg_none_never_subscribes_from_record_alert_itself():
    calls, saved = _install_fake_subscribe()
    try:
        asyncio.run(bs.record_alert("TestStrategy3", "CE", ["ABSL"]))  # no cfg - matches internal call sites
        assert calls == [], (
            "cfg=None (the internal dispatcher/universe-seed call sites' own pattern) must not "
            "subscribe from record_alert itself - those callers already do it separately"
        )
        print("3. cfg=None: no subscribe call from record_alert itself (unchanged internal-caller behavior): PASSED")
    finally:
        _restore_subscribe(saved)


def test_4_watchlist_contents_identical_regardless_of_cfg():
    calls, saved = _install_fake_subscribe()
    try:
        asyncio.run(bs.record_alert("TestStrategy4a", "CE", ["absl", "reliance"], cfg=_fake_cfg(True)))
        asyncio.run(bs.record_alert("TestStrategy4b", "CE", ["absl", "reliance"], cfg=None))
        w_a = asyncio.run(_get_items("TestStrategy4a", "CE"))
        w_b = asyncio.run(_get_items("TestStrategy4b", "CE"))
        assert set(w_a.keys()) == set(w_b.keys()) == {"ABSL", "RELIANCE"}, (
            "the fix must never change WHAT gets recorded in the watchlist, only the WS-subscribe side effect"
        )
        print("4. Watchlist contents are identical regardless of cfg - only the WS-subscribe side effect differs: PASSED")
    finally:
        _restore_subscribe(saved)


async def _get_items(strategy: str, option_type: str) -> dict:
    async with bs._LOCK:
        w = bs._watchlist(strategy, option_type)
        bs._ensure_today_locked(w)
        return dict(w.items)


def test_5_empty_stocks_never_subscribes():
    calls, saved = _install_fake_subscribe()
    try:
        asyncio.run(bs.record_alert("TestStrategy5", "CE", [], cfg=_fake_cfg(True)))
        asyncio.run(bs.record_alert("TestStrategy5", "CE", ["   ", ""], cfg=_fake_cfg(True)))
        assert calls == [], "an empty or blank-only stocks list must never trigger a subscribe call"
        print("5. Empty/blank-only stocks list: no subscribe call: PASSED")
    finally:
        _restore_subscribe(saved)


def main():
    tmp_history = Path(tempfile.mkdtemp(prefix="breakout_signal_test_history_"))
    saved_history_dir = trade_history.HISTORY_DIR
    trade_history.HISTORY_DIR = tmp_history
    try:
        print("=== breakout_signal.py record_alert() WS-subscribe coverage fix test suite ===\n")
        test_1_ws_on_subscribes_cleaned_uppercased_symbols()
        test_2_ws_off_never_subscribes()
        test_3_cfg_none_never_subscribes_from_record_alert_itself()
        test_4_watchlist_contents_identical_regardless_of_cfg()
        test_5_empty_stocks_never_subscribes()
        print("\nALL RECORD_ALERT WS-SUBSCRIBE TESTS PASSED")
    finally:
        trade_history.HISTORY_DIR = saved_history_dir
        shutil.rmtree(tmp_history, ignore_errors=True)


if __name__ == "__main__":
    main()
