"""
Tests for choppy_stocks.py and its wiring into the Options webhook
handler - user request 31 Aug 2026 ("keep a list of stocks with lot size
> 6000, avoid Options trades in them, refresh weekly").

Covers, against the REAL production functions (not reimplemented):
  1. compute_choppy_stocks correctly filters by lot size, one entry per
     underlying even with many option rows for it.
  2. write_choppy_list / read_choppy_list round-trip through real disk I/O
     (a scratch temp dir, never the real choppy/ folder).
  3. The in-memory cache (is_choppy/_load_into_cache/refresh_choppy_list)
     stays consistent with what's on disk, and FAILS OPEN (excludes
     nothing) when nothing has been written yet.
  4. _next_monday_noon_ist's scheduling edge cases (exactly at the target
     instant, just before it, a mid-week day).
  5. The real webhook handler (_handle_chartink_webhook) actually excludes
     a choppy symbol before ranking, doesn't let it consume a top-N slot,
     and still enters the non-choppy symbols normally. Only the Dhan
     network boundary is mocked (same approach as
     test_deep_integration.py) - no live Dhan session needed.

HOW TO RUN:
    uv run python tests/test_choppy_stocks.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import choppy_stocks
import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_choppy_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Options.option_main as om
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


class FakeDhanWrapper:
    """A minimal stand-in exposing only what compute_choppy_stocks needs -
    instruments() and the real _underlying_from_trading_symbol parsing
    logic (reused verbatim from the real wrapper, not reimplemented, so
    this test can't drift from what production actually does)."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def instruments(self):
        return self._df

    _underlying_from_trading_symbol = staticmethod(odc.DhanWrapper._underlying_from_trading_symbol)


def _fake_instrument_df() -> pd.DataFrame:
    """3 underlyings, several rows each (different strikes) - mirrors the
    real scrip master's shape. RELIANCE (small lot) and TCS (small lot)
    should NOT be flagged; BIGLOT (lot 7500, > threshold) should be, from
    every one of its rows, deduplicated to a single entry."""
    rows = []
    for strike in (1300, 1350, 1400):
        rows.append({"SEM_TRADING_SYMBOL": f"RELIANCE-Sep2026-{strike}-CE", "SEM_LOT_UNITS": 500,
                     "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "OPTSTK"})
    for strike in (4000, 4050):
        rows.append({"SEM_TRADING_SYMBOL": f"TCS-Sep2026-{strike}-PE", "SEM_LOT_UNITS": 150,
                     "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "OPTSTK"})
    for strike in (900, 950, 1000):
        rows.append({"SEM_TRADING_SYMBOL": f"BIGLOT-Sep2026-{strike}-CE", "SEM_LOT_UNITS": 7500,
                     "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "OPTSTK"})
    # A non-OPTSTK row (e.g. a future) with a huge "lot size" - must be
    # ignored by the SEM_INSTRUMENT_NAME filter, not just the threshold.
    rows.append({"SEM_TRADING_SYMBOL": "RELIANCE-Sep2026-FUT", "SEM_LOT_UNITS": 99999,
                 "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "FUTSTK"})
    # A BSE row sharing BIGLOT's own trading symbol shape - must be ignored
    # by the SEM_EXM_EXCH_ID == "NSE" filter.
    rows.append({"SEM_TRADING_SYMBOL": "BIGLOT-Sep2026-900-CE", "SEM_LOT_UNITS": 7500,
                 "SEM_EXM_EXCH_ID": "BSE", "SEM_INSTRUMENT_NAME": "OPTSTK"})
    return pd.DataFrame(rows)


def test_1_compute_choppy_stocks_filters_correctly():
    wrapper = FakeDhanWrapper(_fake_instrument_df())
    result = choppy_stocks.compute_choppy_stocks(wrapper)
    assert result == {"BIGLOT": 7500}, f"expected only BIGLOT flagged at 7500, got {result}"
    print("1. compute_choppy_stocks: only the lot-size>6000 underlying (BIGLOT) is flagged, "
          "deduped across its 3 rows, non-OPTSTK/non-NSE rows correctly ignored: PASSED")


def test_2_write_and_read_round_trip():
    real_dir, real_file = choppy_stocks.CHOPPY_DIR, choppy_stocks.CHOPPY_FILE
    choppy_stocks.CHOPPY_DIR = scratch_dir / "choppy"
    choppy_stocks.CHOPPY_FILE = choppy_stocks.CHOPPY_DIR / "choppy_stocks.json"
    try:
        assert choppy_stocks.read_choppy_list() is None, "expected no file yet"
        written = choppy_stocks.write_choppy_list({"BIGLOT": 7500, "HUGELOT": 10000})
        assert written["threshold"] == choppy_stocks.LOT_SIZE_THRESHOLD
        assert written["count"] == 2
        read_back = choppy_stocks.read_choppy_list()
        assert read_back == written, "read-back must exactly match what was written"
        symbols = {s["symbol"] for s in read_back["stocks"]}
        assert symbols == {"BIGLOT", "HUGELOT"}
        # Sorted by lot size descending, per write_choppy_list's own contract.
        assert read_back["stocks"][0]["symbol"] == "HUGELOT"
        print("2. write_choppy_list/read_choppy_list round-trip through real disk I/O "
              "(scratch dir), sorted descending by lot size: PASSED")
    finally:
        choppy_stocks.CHOPPY_DIR, choppy_stocks.CHOPPY_FILE = real_dir, real_file


def test_3_cache_fails_open_then_tracks_refresh():
    choppy_stocks._cached_choppy_symbols = set()  # simulate a fresh process, nothing loaded yet
    assert not choppy_stocks.is_choppy("BIGLOT"), "must fail open (exclude nothing) before any load/refresh"

    real_dir, real_file = choppy_stocks.CHOPPY_DIR, choppy_stocks.CHOPPY_FILE
    choppy_stocks.CHOPPY_DIR = scratch_dir / "choppy2"
    choppy_stocks.CHOPPY_FILE = choppy_stocks.CHOPPY_DIR / "choppy_stocks.json"
    try:
        wrapper = FakeDhanWrapper(_fake_instrument_df())
        choppy_stocks.refresh_choppy_list(wrapper)
        assert choppy_stocks.is_choppy("BIGLOT"), "cache must reflect the just-completed refresh immediately"
        assert not choppy_stocks.is_choppy("RELIANCE")
        assert not choppy_stocks.is_choppy("TCS")

        # A fresh process restarting later must recover the same state from disk.
        choppy_stocks._cached_choppy_symbols = set()
        choppy_stocks.load_choppy_cache_at_startup()
        assert choppy_stocks.is_choppy("BIGLOT"), "load_choppy_cache_at_startup must recover the persisted list"
        print("3. In-memory cache fails open with nothing loaded, reflects refresh_choppy_list "
              "immediately, and survives a simulated restart via load_choppy_cache_at_startup: PASSED")
    finally:
        choppy_stocks.CHOPPY_DIR, choppy_stocks.CHOPPY_FILE = real_dir, real_file
        choppy_stocks._cached_choppy_symbols = set()


def test_4_next_monday_noon_edge_cases():
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    exactly_at_target = datetime(2026, 8, 31, 12, 0, 0, tzinfo=IST)  # Monday, exactly noon
    assert choppy_stocks._next_monday_noon_ist(exactly_at_target) == datetime(2026, 9, 7, 12, 0, tzinfo=IST), \
        "at the exact target instant, must roll to NEXT Monday (candidate <= now), not fire twice"

    one_second_before = datetime(2026, 8, 31, 11, 59, 59, tzinfo=IST)
    assert choppy_stocks._next_monday_noon_ist(one_second_before) == datetime(2026, 8, 31, 12, 0, tzinfo=IST)

    mid_week = datetime(2026, 9, 2, 15, 0, tzinfo=IST)  # Wednesday
    assert choppy_stocks._next_monday_noon_ist(mid_week) == datetime(2026, 9, 7, 12, 0, tzinfo=IST)

    sunday_late = datetime(2026, 9, 6, 23, 59, tzinfo=IST)
    assert choppy_stocks._next_monday_noon_ist(sunday_late) == datetime(2026, 9, 7, 12, 0, tzinfo=IST)

    print("4. _next_monday_noon_ist: exact-instant rollover, just-before, mid-week, and "
          "Sunday-night edge cases all resolve correctly: PASSED")


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.place_market_order = lambda trading_symbol, quantity, transaction_type, tag=None, product_type=None: {
        "order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


async def test_5_webhook_handler_excludes_choppy_stock_before_ranking():
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 10

    real_rank = om.rank_and_pick_top_stocks

    def fake_ranked(stocks, top_n, prefer_highest):
        # Ranks by input order, capped to top_n - same shape as
        # test_deep_integration.py's fake_ranked, isolating ranking's own
        # network dependency without changing its selection contract.
        return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]

    om.rank_and_pick_top_stocks = fake_ranked

    real_choppy_symbols = choppy_stocks._cached_choppy_symbols
    choppy_stocks._cached_choppy_symbols = {"BIGLOT"}

    restore = install_all_dhan_mocks()
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="RELIANCE,BIGLOT,TCS", trigger_prices="1,1,1", triggered_at="9:20 am",
            scan_name="choppy-test", scan_url="choppy-test",
            alert_name="Choppy-stock exclusion test",
        )
        result = await om._handle_chartink_webhook(payload, "CE", True)

        assert result["status"] == "processed", result
        assert result["choppy_stocks_excluded"] == ["BIGLOT"], result
        ranked_symbols = [s for s, _pct in result["ranked_by_day_change_pct"]]
        assert "BIGLOT" not in ranked_symbols, "choppy stock must never reach ranking at all"
        assert set(ranked_symbols) == {"RELIANCE", "TCS"}, ranked_symbols

        entered = [e["symbol"] for e in result["entries"] if e["status"] == "entered"]
        assert set(entered) == {"RELIANCE", "TCS"}, \
            f"both non-choppy stocks should still enter normally, got {entered}"
        assert "BIGLOT" not in store.live_positions

        # Alert log must still show the ORIGINAL, unfiltered stock list -
        # this is an audit trail of what Chartink sent, not what the bot acted on.
        await asyncio.sleep(0.3)
        alerts = trade_history.read_all_jsonl("webhook_alerts")
        last = alerts[-1]
        assert set(last["stocks"]) == {"RELIANCE", "BIGLOT", "TCS"}, \
            f"webhook_alerts log must record the full original alert, got {last['stocks']}"

        print("5. Real webhook handler excludes the choppy stock (BIGLOT) before ranking - "
              "it never occupies a top-N slot, both other stocks enter normally, and the "
              "audit log still records the original unfiltered alert: PASSED")
    finally:
        restore()
        om.rank_and_pick_top_stocks = real_rank
        choppy_stocks._cached_choppy_symbols = real_choppy_symbols


async def test_6_all_stocks_choppy_is_ignored_cleanly():
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 10

    real_choppy_symbols = choppy_stocks._cached_choppy_symbols
    choppy_stocks._cached_choppy_symbols = {"BIGLOT", "HUGELOT"}
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="BIGLOT,HUGELOT", trigger_prices="1,1", triggered_at="9:20 am",
            scan_name="choppy-test-2", scan_url="choppy-test-2",
            alert_name="All-choppy alert test",
        )
        result = await om._handle_chartink_webhook(payload, "CE", True)
        assert result["status"] == "ignored"
        assert result["reason"] == "all_stocks_choppy"
        assert set(result["choppy_stocks_excluded"]) == {"BIGLOT", "HUGELOT"}
        assert store.live_positions == {}
        print("6. An alert where every stock is choppy is cleanly ignored (no ranking/entry "
              "attempted at all), not left to fall through to a misleading 'could not rank' reason: PASSED")
    finally:
        choppy_stocks._cached_choppy_symbols = real_choppy_symbols


async def main():
    print("=== choppy_stocks.py test suite ===\n")
    test_1_compute_choppy_stocks_filters_correctly()
    test_2_write_and_read_round_trip()
    test_3_cache_fails_open_then_tracks_refresh()
    test_4_next_monday_noon_edge_cases()
    await test_5_webhook_handler_excludes_choppy_stock_before_ranking()
    await test_6_all_stocks_choppy_is_ignored_cleanly()
    print("\nALL CHOPPY-STOCKS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
