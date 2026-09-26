"""
Tests for breakout_signal.py's record_alert() index/MCX-commodity filter
(added 26 Sep 2026, audit finding): this is an NSE-equity-only scanner
(_equity_security_id is its only resolution path) but kept receiving
NIFTY/BANKNIFTY/NATURALGAS - legitimately Swing's own watchlist symbols,
handled through Swing's dedicated index/MCX path, not this one - via the
shared curated-universe/universe_bucket seed. Each one then failed every
single scan cycle forever (ValueError: No NSE equity instrument found),
burning a thread-pool submission and a traceback every cycle for symbols
that could never succeed. Fixed by filtering at record_alert's single
insertion point (the only place any symbol enters a watchlist, whether
from a live Chartink alert or the curated-universe seed).

Exercises the REAL record_alert() directly - no reimplementation - only
mocking `dhan_wrapper.is_mcx_commodity` (the network/instrument-master
boundary); INDEX_SECURITY_ID is a real static dict, not mocked.

HOW TO RUN:
    uv run python tests/test_breakout_signal_index_mcx_filter.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

import breakout_signal as bs  # noqa: E402
import trade_history  # noqa: E402
from Options.dhan_client import dhan_wrapper  # noqa: E402


def _fake_cfg(ws_on: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(BREAKOUT_USE_WS_CANDLES=ws_on)


async def _get_items(strategy: str, option_type: str) -> dict:
    async with bs._LOCK:
        w = bs._watchlist(strategy, option_type)
        await bs._ensure_today_locked(w)
        return dict(w.items)


def _mcx_mock(mcx_symbols: set[str]):
    return mock.patch.object(dhan_wrapper, "is_mcx_commodity", side_effect=lambda s: s in mcx_symbols)


def test_1_index_symbols_filtered_real_equity_kept():
    with _mcx_mock(set()):
        asyncio.run(bs.record_alert("T1", "CE", ["NIFTY", "BANKNIFTY", "RELIANCE"], cfg=_fake_cfg()))
    items = asyncio.run(_get_items("T1", "CE"))
    assert set(items.keys()) == {"RELIANCE"}, (
        f"NIFTY/BANKNIFTY must be filtered out (index symbols, not NSE equity), got {set(items.keys())}"
    )
    print("1. Index symbols (NIFTY/BANKNIFTY) filtered out, real NSE-equity symbol kept: PASSED")


def test_2_mcx_commodity_filtered():
    with _mcx_mock({"COPPER", "NATURALGAS"}):
        asyncio.run(bs.record_alert("T2", "CE", ["COPPER", "NATURALGAS", "TCS"], cfg=_fake_cfg()))
    items = asyncio.run(_get_items("T2", "CE"))
    assert set(items.keys()) == {"TCS"}, f"MCX commodities must be filtered out, got {set(items.keys())}"
    print("2. MCX-commodity symbols (COPPER/NATURALGAS) filtered out, real NSE-equity symbol kept: PASSED")


def test_3_mixed_batch_only_equity_survives():
    with _mcx_mock({"COPPER"}):
        asyncio.run(bs.record_alert("T3", "PE", ["NIFTY", "COPPER", "ABSL", "TCS"], cfg=_fake_cfg()))
    items = asyncio.run(_get_items("T3", "PE"))
    assert set(items.keys()) == {"ABSL", "TCS"}, (
        f"a mixed batch must keep only the real NSE-equity symbols, got {set(items.keys())}"
    )
    print("3. Mixed batch (index + MCX + equity): only equity symbols survive into the watchlist: PASSED")


def test_4_all_filtered_batch_is_a_harmless_noop():
    with _mcx_mock({"NATURALGAS"}):
        asyncio.run(bs.record_alert("T4", "CE", ["NIFTY", "BANKNIFTY", "NATURALGAS"], cfg=_fake_cfg()))
    items = asyncio.run(_get_items("T4", "CE"))
    assert items == {}, f"a batch that's entirely index/MCX symbols must leave the watchlist empty, got {items}"
    print("4. A batch that's entirely index/MCX symbols is a harmless no-op (no crash, empty watchlist): PASSED")


def test_5_ws_subscribe_never_called_for_filtered_symbols():
    """The filter must happen BEFORE the (pre-existing) WS-subscribe step,
    so a filtered-out symbol is never even subscribed."""
    import underlying_candle_feed
    calls = []
    saved = underlying_candle_feed.subscribe
    underlying_candle_feed.subscribe = lambda symbols: calls.append(list(symbols))
    try:
        with _mcx_mock({"COPPER"}):
            asyncio.run(bs.record_alert("T5", "CE", ["NIFTY", "COPPER", "WIPRO"], cfg=_fake_cfg(ws_on=True)))
        assert len(calls) == 1 and calls[0] == ["WIPRO"], (
            f"only the surviving equity symbol should ever be WS-subscribed, got {calls}"
        )
    finally:
        underlying_candle_feed.subscribe = saved
    print("5. WS-subscribe is only called for the surviving (non-index/MCX) symbol: PASSED")


def test_6_plain_equity_batch_unaffected_baseline():
    with _mcx_mock(set()):
        asyncio.run(bs.record_alert("T6", "CE", ["absl", "reliance", "tcs"], cfg=_fake_cfg()))
    items = asyncio.run(_get_items("T6", "CE"))
    assert set(items.keys()) == {"ABSL", "RELIANCE", "TCS"}, (
        f"a plain all-equity batch must be completely unaffected by this fix, got {set(items.keys())}"
    )
    print("6. A plain all-NSE-equity batch is completely unaffected (baseline regression guard): PASSED")


def main():
    tmp_history = Path(tempfile.mkdtemp(prefix="breakout_signal_filter_test_history_"))
    saved_history_dir = trade_history.HISTORY_DIR
    trade_history.HISTORY_DIR = tmp_history
    try:
        print("=== breakout_signal.py record_alert() index/MCX filter test suite ===\n")
        test_1_index_symbols_filtered_real_equity_kept()
        test_2_mcx_commodity_filtered()
        test_3_mixed_batch_only_equity_survives()
        test_4_all_filtered_batch_is_a_harmless_noop()
        test_5_ws_subscribe_never_called_for_filtered_symbols()
        test_6_plain_equity_batch_unaffected_baseline()
        print("\nALL INDEX/MCX FILTER TESTS PASSED")
    finally:
        trade_history.HISTORY_DIR = saved_history_dir
        shutil.rmtree(tmp_history, ignore_errors=True)


if __name__ == "__main__":
    main()
