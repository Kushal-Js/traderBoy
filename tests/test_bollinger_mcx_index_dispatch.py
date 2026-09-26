"""
Tests for Bollinger/signals.py's MCX/index underlying-reference and
market-hours dispatch (added 26 Sep 2026, user request: "Update Bollinger
strategy to trade in MCX and Index Options also... Update market hours
and timings and Square OFF policies as we have for SWING strategy
currently"). This is the highest-risk part of that change - a wrong
dispatch here would silently resolve the WRONG underlying/exchange for a
real-money entry (paper mode is off for Bollinger). Mocks dhan_wrapper's
instrument-resolution calls and Swing.candle_feed.ensure_subscribed so
this runs fully offline, no live Dhan auth needed - mirrors this
strategy's own test_bollinger_signals.py convention (mock the boundary,
exercise this package's own dispatch code for real).

HOW TO RUN:
    uv run python tests/test_bollinger_mcx_index_dispatch.py
"""
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import Bollinger.signals as signals  # noqa: E402
import Bollinger.trading_engine as trading_engine  # noqa: E402
from Bollinger import config  # noqa: E402


def test_1_underlying_reference_dispatches_mcx():
    """A symbol dhan_wrapper.is_mcx_commodity says is MCX must resolve via
    get_mcx_futures_contract, segment MCX_COMM/FUTCOM - never the plain
    NSE-equity path."""
    fake_contract = SimpleNamespace(security_id="571298", expiry_date=date(2026, 10, 30))
    with mock.patch.object(signals.dhan_wrapper, "is_mcx_commodity", return_value=True), \
         mock.patch.object(signals.dhan_wrapper, "get_mcx_futures_contract", return_value=fake_contract) as get_mcx, \
         mock.patch.object(signals.candle_feed, "ensure_subscribed") as ensure_sub:
        signals._mcx_contract_cache.clear()
        security_id, exchange_segment, instrument_type = signals._underlying_reference("COPPER")
    assert (security_id, exchange_segment, instrument_type) == ("571298", "MCX_COMM", "FUTCOM"), \
        f"expected MCX futures contract dispatch, got {(security_id, exchange_segment, instrument_type)}"
    get_mcx.assert_called_once_with("COPPER")
    ensure_sub.assert_called_once_with("COPPER", "571298", "MCX_COMM")
    print("1. MCX symbol dispatches to get_mcx_futures_contract / MCX_COMM / FUTCOM: PASSED")


def test_2_underlying_reference_dispatches_index():
    """A symbol in config.INDEX_SYMBOLS must resolve via index_security_id,
    segment IDX_I/INDEX - not the plain NSE-equity path, and not MCX."""
    assert "NIFTY" in config.INDEX_SYMBOLS, "test assumes NIFTY is in config.INDEX_SYMBOLS by default"
    with mock.patch.object(signals.dhan_wrapper, "is_mcx_commodity", return_value=False), \
         mock.patch.object(signals.dhan_wrapper, "index_security_id", return_value="13") as get_idx, \
         mock.patch.object(signals.candle_feed, "ensure_subscribed") as ensure_sub:
        security_id, exchange_segment, instrument_type = signals._underlying_reference("NIFTY")
    assert (security_id, exchange_segment, instrument_type) == ("13", "IDX_I", "INDEX"), \
        f"expected index dispatch, got {(security_id, exchange_segment, instrument_type)}"
    get_idx.assert_called_once_with("NIFTY")
    ensure_sub.assert_called_once_with("NIFTY", "13", "IDX_I")
    print("2. Index symbol (NIFTY) dispatches to index_security_id / IDX_I / INDEX: PASSED")


def test_3_underlying_reference_still_dispatches_nse_equity():
    """A plain NSE-equity symbol (neither MCX nor in config.INDEX_SYMBOLS)
    must be COMPLETELY UNCHANGED from before this MCX/index change - the
    original v1 behavior for the 9 already-live equity symbols must not
    regress."""
    with mock.patch.object(signals.dhan_wrapper, "is_mcx_commodity", return_value=False), \
         mock.patch.object(signals.dhan_wrapper, "_equity_security_id", return_value="3456") as get_eq, \
         mock.patch.object(signals.candle_feed, "ensure_subscribed") as ensure_sub:
        security_id, exchange_segment, instrument_type = signals._underlying_reference("SONACOMS")
    assert (security_id, exchange_segment, instrument_type) == ("3456", "NSE_EQ", "EQUITY"), \
        f"expected unchanged NSE-equity dispatch, got {(security_id, exchange_segment, instrument_type)}"
    get_eq.assert_called_once_with("SONACOMS")
    ensure_sub.assert_called_once_with("SONACOMS", "3456", "NSE_EQ")
    print("3. Plain NSE-equity symbol (SONACOMS) is unchanged: NSE_EQ / EQUITY: PASSED")


def test_4_symbol_market_open_uses_mcx_segment_for_mcx_only():
    """_symbol_market_open must check the MCX_COMM segment for an MCX
    symbol and NSE_EQ for everything else (including an index) - same
    dispatch Swing/signals.py's own _symbol_market_open uses."""
    with mock.patch.object(signals.dhan_wrapper, "is_mcx_commodity", return_value=True), \
         mock.patch.object(signals.dhan_wrapper, "is_market_open", return_value=True) as is_open, \
         mock.patch.object(signals, "_now_ist", return_value=__import__("datetime").datetime(2026, 9, 28, 12, 0)):
        assert signals._symbol_market_open("COPPER") is True
    is_open.assert_called_once_with(exchange_segment="MCX_COMM")

    with mock.patch.object(signals.dhan_wrapper, "is_mcx_commodity", return_value=False), \
         mock.patch.object(signals.dhan_wrapper, "is_market_open", return_value=True) as is_open2, \
         mock.patch.object(signals, "_now_ist", return_value=__import__("datetime").datetime(2026, 9, 28, 12, 0)):
        assert signals._symbol_market_open("NIFTY") is True
    is_open2.assert_called_once_with(exchange_segment="NSE_EQ")
    print("4. _symbol_market_open checks MCX_COMM for MCX, NSE_EQ for index/equity: PASSED")


def test_5_symbol_market_open_false_on_weekend():
    """Saturday/Sunday must always be closed regardless of is_market_open's
    own return value - same weekday-first guard as Swing's own."""
    saturday = __import__("datetime").datetime(2026, 9, 26 + (5 - date(2026, 9, 26).weekday()), 12, 0)
    assert saturday.weekday() == 5
    with mock.patch.object(signals, "_now_ist", return_value=saturday), \
         mock.patch.object(signals.dhan_wrapper, "is_market_open", return_value=True):
        assert signals._symbol_market_open("COPPER") is False
    print("5. Saturday is always closed regardless of is_market_open: PASSED")


def test_6_enter_position_dispatches_mcx_exchange_and_pnl_multiplier():
    """enter_position_for_stock must set exchange_segment=MCX_COMM and
    look up the REAL pnl_multiplier from Swing.mcx_registry for an MCX
    symbol - never the raw lot-count `quantity` (the exact SEM_LOT_UNITS=1
    bug found and fixed in this session's own backtest work, see
    trading-skills' bollinger-vortex-strategy-30day-backtest.md). Only
    exercises the instrument-resolution branch up to (but not including)
    order placement - stops the test at the funds-check gate by making it
    report insufficient funds, so no order-placement mocking is needed."""
    import asyncio

    fake_atm = SimpleNamespace(
        trading_symbol="COPPER 23 OCT 1400 CALL", security_id="99999", lot_size=1,
        expiry_date=date(2026, 10, 23),
    )

    async def fake_pnl_multiplier(symbol):
        assert symbol == "COPPER"
        return 2500

    async def fake_reserve_symbol(symbol):
        return True

    async def fake_release_symbol(symbol):
        return None

    async def fake_record_failed_entry(symbol):
        return None

    captured = {}

    async def fake_has_sufficient_bucket_funds(bucket, symbol, legs, buffer_rs):
        # Capture the leg tuple's exchange_segment/quantity so the test can
        # assert on it, then bail out here (insufficient funds) so the
        # test never needs to mock real order placement.
        captured["legs"] = legs
        return False

    with mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=True), \
         mock.patch.object(trading_engine.dhan_wrapper, "get_liquid_atm_option", return_value=fake_atm), \
         mock.patch.object(trading_engine.dhan_wrapper, "get_option_ltp", return_value=42.0), \
         mock.patch.object(trading_engine.mcx_registry, "pnl_multiplier", side_effect=fake_pnl_multiplier), \
         mock.patch.object(trading_engine.position_store, "reserve_symbol", side_effect=fake_reserve_symbol), \
         mock.patch.object(trading_engine.position_store, "release_symbol", side_effect=fake_release_symbol), \
         mock.patch.object(trading_engine.position_store, "record_failed_entry", side_effect=fake_record_failed_entry), \
         mock.patch.object(trading_engine.fund_allocation, "has_sufficient_bucket_funds",
                            side_effect=fake_has_sufficient_bucket_funds):
        result = asyncio.run(trading_engine.enter_position_for_stock("COPPER", "BULLISH", 1400.0, 1380.0, 1395.0))

    assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", \
        f"expected the test to stop cleanly at the funds-check gate, got {result}"
    (security_id, product_type, quantity, price, exchange_segment), = captured["legs"]
    assert exchange_segment == "MCX_COMM", f"expected MCX_COMM exchange_segment, got {exchange_segment}"
    assert product_type == config.MCX_PRODUCT, f"expected config.MCX_PRODUCT, got {product_type}"
    print("6. enter_position_for_stock resolves MCX_COMM exchange_segment/product_type for an MCX symbol: PASSED")


def test_7_enter_position_skips_mcx_without_configured_multiplier():
    """An MCX symbol with NO entry in data/mcx_config must SKIP the real
    entry rather than guess a pnl_multiplier - same discipline as Swing's
    own identical check, and the exact failure mode the SEM_LOT_UNITS=1
    bug (this session's backtest work) would otherwise reproduce live."""
    import asyncio

    fake_atm = SimpleNamespace(
        trading_symbol="UNCONFIGUREDMCX 23 OCT 100 CALL", security_id="88888", lot_size=1,
        expiry_date=date(2026, 10, 23),
    )

    async def fake_pnl_multiplier_none(symbol):
        return None

    async def fake_reserve_symbol(symbol):
        return True

    async def fake_release_symbol(symbol):
        return None

    async def fake_record_failed_entry(symbol):
        return None

    with mock.patch.object(trading_engine.dhan_wrapper, "is_mcx_commodity", return_value=True), \
         mock.patch.object(trading_engine.dhan_wrapper, "get_liquid_atm_option", return_value=fake_atm), \
         mock.patch.object(trading_engine.mcx_registry, "pnl_multiplier", side_effect=fake_pnl_multiplier_none), \
         mock.patch.object(trading_engine.position_store, "reserve_symbol", side_effect=fake_reserve_symbol), \
         mock.patch.object(trading_engine.position_store, "release_symbol", side_effect=fake_release_symbol), \
         mock.patch.object(trading_engine.position_store, "record_failed_entry", side_effect=fake_record_failed_entry):
        result = asyncio.run(trading_engine.enter_position_for_stock("UNCONFIGUREDMCX", "BULLISH", 100.0, 95.0, 98.0))

    assert result == {"symbol": "UNCONFIGUREDMCX", "status": "skipped", "reason": "mcx_pnl_multiplier_not_configured"}, \
        f"expected a clean skip with no guessed multiplier, got {result}"
    print("7. An unconfigured MCX symbol skips entry rather than guessing pnl_multiplier: PASSED")


def test_8_square_off_predicates():
    """_is_friday_square_off_time / _is_mcx_friday_square_off_time /
    _is_index_square_off_time must match Swing's own predicates exactly:
    Friday-only (weekday==4) for the first two, Mon-Fri for the index one,
    each gated on its own configured time-of-day."""
    import datetime as dt
    friday_before = dt.datetime(2026, 9, 25, 15, 0)   # Friday, before 15:25
    friday_after = dt.datetime(2026, 9, 25, 15, 30)   # Friday, after 15:25
    friday_late = dt.datetime(2026, 9, 25, 23, 30)    # Friday, after MCX's 23:25
    monday = dt.datetime(2026, 9, 28, 15, 30)         # Monday, after 15:25 (index-only should fire, Friday ones must not)

    with mock.patch.object(trading_engine, "_now_ist", return_value=friday_before):
        assert trading_engine._is_friday_square_off_time() is False
    with mock.patch.object(trading_engine, "_now_ist", return_value=friday_after):
        assert trading_engine._is_friday_square_off_time() is True
        assert trading_engine._is_mcx_friday_square_off_time() is False  # not yet 23:25
        assert trading_engine._is_index_square_off_time() is True
    with mock.patch.object(trading_engine, "_now_ist", return_value=friday_late):
        assert trading_engine._is_mcx_friday_square_off_time() is True
    with mock.patch.object(trading_engine, "_now_ist", return_value=monday):
        assert trading_engine._is_friday_square_off_time() is False       # not Friday
        assert trading_engine._is_mcx_friday_square_off_time() is False   # not Friday
        assert trading_engine._is_index_square_off_time() is True         # daily, Mon-Fri
    print("8. Friday/MCX-Friday/index-daily square-off predicates match Swing's own timing exactly: PASSED")


if __name__ == "__main__":
    test_1_underlying_reference_dispatches_mcx()
    test_2_underlying_reference_dispatches_index()
    test_3_underlying_reference_still_dispatches_nse_equity()
    test_4_symbol_market_open_uses_mcx_segment_for_mcx_only()
    test_5_symbol_market_open_false_on_weekend()
    test_6_enter_position_dispatches_mcx_exchange_and_pnl_multiplier()
    test_7_enter_position_skips_mcx_without_configured_multiplier()
    test_8_square_off_predicates()
    print("\nAll tests passed.")
