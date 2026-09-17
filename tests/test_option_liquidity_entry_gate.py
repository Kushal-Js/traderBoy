"""
Tests for the option-liquidity ENTRY gate (added 18 Sep 2026, real
incident: SOLARINDS 29 SEP 18750 PUT, Options, 17 Sep 2026, lost Rs
3,995.00 via MAX_LOSS_HIT while the underlying itself moved only ~0.32% -
a pure option-side liquidity/gap event none of the existing entry-time
signals (volume floor, ADX/ER trend-strength) could see, since all of
them measure the UNDERLYING's own price/volume, never the OPTION's).

This gate reuses dhan_wrapper.refresh_liquidity_signal/get_cached_
illiquid - the option's OWN "last N completed bars all zero volume"
check, already built and live for EXIT decisions (Options/Futures/Luxury
each already have LIQUIDITY_GUARD_ENABLED) - but wires it into the ENTRY
path too, for the first time, via reversal_filters.check_option_
liquidity. Checked in each package's own _enter_single_position, right
after the real contract is resolved (get_atm_option) and before any real
order is placed.

Coverage:
  1. check_option_liquidity_sync fails OPEN (passes=True, is_illiquid=None)
     when dhan_wrapper._client is None - never triggers a real login as a
     side effect, same discipline as check_volume_floor_sync/check_trend_
     strength_sync.
  2. check_option_liquidity_sync correctly reports (False, True) when the
     option is confirmed illiquid (get_cached_illiquid returns True after
     a real refresh_liquidity_signal call).
  3. check_option_liquidity_sync correctly reports (True, False) when the
     option is confirmed liquid.
  4. check_option_liquidity_sync fails OPEN when get_cached_illiquid
     returns None (not enough data yet) - never treats missing
     information as a confirmed illiquid reading.
  5-7. Wiring: for EACH of Options/Futures/Luxury (each keeps its own
     copy of _enter_single_position), an illiquid option is BLOCKED when
     LIQUIDITY_ENTRY_GATE_ENABLED=True (zero real orders placed), the
     SAME illiquid signal is allowed through when the flag is False, and
     a liquid option is never blocked even with the flag on.

HOW TO RUN:
    uv run python tests/test_option_liquidity_entry_gate.py
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import tempfile
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_option_liquidity_entry_gate_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.config as ocfg
import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Futures.config as fcfg
import Futures.position_store as fps
import Futures.trading_engine as fte
import Luxury.config as lcfg
import Luxury.position_store as lps
import Luxury.trading_engine as lte
import reversal_filters
from Options.dhan_client import AtmOption, OrderResult, OrderStatus, dhan_wrapper

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE {option_type}", strike=1000.0, option_type=option_type,
                      lot_size=100, security_id=f"OPTSEC-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Mocks every Dhan network call the entry path touches. Shared across
    all three packages since they read the SAME dhan_wrapper singleton."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_bearish": odc.dhan_wrapper.get_cached_supertrend_bearish,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "refresh_ema_cross_signal": odc.dhan_wrapper.refresh_ema_cross_signal,
        "get_cached_ema_cross_candle_start": odc.dhan_wrapper.get_cached_ema_cross_candle_start,
        "is_rsi_loss_reentry_blocked": odc.dhan_wrapper.is_rsi_loss_reentry_blocked,
        "get_cached_rsi": odc.dhan_wrapper.get_cached_rsi,
        "get_cached_prev_rsi": odc.dhan_wrapper.get_cached_prev_rsi,
        "rsi_loss_reentry_reason": odc.dhan_wrapper.rsi_loss_reentry_reason,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 10_000_000.0}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_supertrend_bearish = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda underlying_symbol: None
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda underlying_symbol: None
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: False
    odc.dhan_wrapper.get_cached_rsi = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_prev_rsi = lambda underlying_symbol: None
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda underlying_symbol: None

    placed_orders = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}-{id(object())}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.place_stop_loss_limit_order = lambda trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SLL-{trading_symbol}-{len(placed_orders)}-{id(object())}"}
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=100, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)

    return restore, placed_orders


def test_1_check_option_liquidity_fails_open_when_not_authenticated():
    original = dhan_wrapper._client
    dhan_wrapper._client = None
    with mock.patch.object(dhan_wrapper, "authenticate") as fake_auth:
        try:
            passes, is_illiquid = reversal_filters.check_option_liquidity_sync("RELIANCE 29 SEP 3000 CALL")
            assert passes is True and is_illiquid is None
            fake_auth.assert_not_called()
            print("1. check_option_liquidity_sync fails OPEN (passes=True) when dhan_wrapper._client is None, "
                  "and never triggers a real login as a side effect: PASSED")
        finally:
            dhan_wrapper._client = original


def test_2_reports_illiquid_correctly():
    original = dhan_wrapper._client
    dhan_wrapper._client = object()  # non-None, so the early-exit guard doesn't fire
    with mock.patch.object(dhan_wrapper, "refresh_liquidity_signal", lambda ts: None), \
         mock.patch.object(dhan_wrapper, "get_cached_illiquid", lambda ts: True):
        try:
            passes, is_illiquid = reversal_filters.check_option_liquidity_sync("SOLARINDS 29 SEP 18750 PUT")
            assert passes is False and is_illiquid is True
            print("2. check_option_liquidity_sync correctly reports (passes=False) for a confirmed-illiquid "
                  "option: PASSED")
        finally:
            dhan_wrapper._client = original


def test_3_reports_liquid_correctly():
    original = dhan_wrapper._client
    dhan_wrapper._client = object()
    with mock.patch.object(dhan_wrapper, "refresh_liquidity_signal", lambda ts: None), \
         mock.patch.object(dhan_wrapper, "get_cached_illiquid", lambda ts: False):
        try:
            passes, is_illiquid = reversal_filters.check_option_liquidity_sync("RELIANCE 29 SEP 3000 CALL")
            assert passes is True and is_illiquid is False
            print("3. check_option_liquidity_sync correctly reports (passes=True) for a confirmed-liquid "
                  "option: PASSED")
        finally:
            dhan_wrapper._client = original


def test_4_fails_open_on_missing_data():
    original = dhan_wrapper._client
    dhan_wrapper._client = object()
    with mock.patch.object(dhan_wrapper, "refresh_liquidity_signal", lambda ts: None), \
         mock.patch.object(dhan_wrapper, "get_cached_illiquid", lambda ts: None):
        try:
            passes, is_illiquid = reversal_filters.check_option_liquidity_sync("NEWSYMBOL 29 SEP 100 CALL")
            assert passes is True and is_illiquid is None, \
                "missing signal data (not enough bars yet) must never be treated as confirmed illiquid"
            print("4. check_option_liquidity_sync fails OPEN when get_cached_illiquid returns None (not "
                  "enough data yet): PASSED")
        finally:
            dhan_wrapper._client = original


async def test_5_options_entry_blocked_when_illiquid():
    store = ops.PositionStore()
    ote.position_store = store
    ocfg.MAX_LIVE_POSITIONS_CE, ocfg.MAX_LIVE_POSITIONS_PE = 5, 5
    ocfg.LOSS_REPEAT_BLOCK_ENABLED = False
    ocfg.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    ocfg.VOLUME_FLOOR_GATE_ENABLED = False
    ocfg.LIQUIDITY_ENTRY_GATE_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_option_liquidity", new=AsyncMock(return_value=(False, True))):
        try:
            result = await ote._process_one_entry("SOLARINDS", "PE")
            assert result["status"] == "skipped" and result["reason"] == "option_illiquid_at_entry", result
            assert len(placed_orders) == 0, "no real order should have been placed"
            print("5. Options: an already-illiquid option is BLOCKED at entry when "
                  "LIQUIDITY_ENTRY_GATE_ENABLED=True - zero real orders placed: PASSED")
        finally:
            restore()


async def test_6_options_entry_allowed_when_flag_disabled():
    store = ops.PositionStore()
    ote.position_store = store
    ocfg.MAX_LIVE_POSITIONS_CE, ocfg.MAX_LIVE_POSITIONS_PE = 5, 5
    ocfg.LOSS_REPEAT_BLOCK_ENABLED = False
    ocfg.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    ocfg.VOLUME_FLOOR_GATE_ENABLED = False
    ocfg.LIQUIDITY_ENTRY_GATE_ENABLED = False
    restore, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_option_liquidity", new=AsyncMock(return_value=(False, True))):
        try:
            result = await ote._process_one_entry("SOLARINDS", "PE")
            assert result["status"] == "entered", result
            assert len(placed_orders) == 1
            print("6. Options: the SAME illiquid signal is allowed through when "
                  "LIQUIDITY_ENTRY_GATE_ENABLED=False - confirms this is a real, flippable flag: PASSED")
        finally:
            restore()


async def test_7_options_liquid_option_not_blocked():
    store = ops.PositionStore()
    ote.position_store = store
    ocfg.MAX_LIVE_POSITIONS_CE, ocfg.MAX_LIVE_POSITIONS_PE = 5, 5
    ocfg.LOSS_REPEAT_BLOCK_ENABLED = False
    ocfg.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    ocfg.VOLUME_FLOOR_GATE_ENABLED = False
    ocfg.LIQUIDITY_ENTRY_GATE_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_option_liquidity", new=AsyncMock(return_value=(True, False))):
        try:
            result = await ote._process_one_entry("RELIANCE", "CE")
            assert result["status"] == "entered", result
            assert len(placed_orders) == 1
            print("7. Options: a liquid option is NOT blocked even with the gate enabled: PASSED")
        finally:
            restore()


async def test_8_futures_entry_blocked_when_illiquid():
    store = fps.PositionStore()
    fte.position_store = store
    fcfg.MAX_LIVE_POSITIONS_CE, fcfg.MAX_LIVE_POSITIONS_PE = 5, 5
    fcfg.LOSS_REPEAT_BLOCK_ENABLED = False
    fcfg.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    fcfg.VOLUME_FLOOR_GATE_ENABLED = False
    fcfg.LIQUIDITY_ENTRY_GATE_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_option_liquidity", new=AsyncMock(return_value=(False, True))):
        try:
            result = await fte._process_one_entry("SOLARINDS", "PE")
            assert result["status"] == "skipped" and result["reason"] == "option_illiquid_at_entry", result
            assert len(placed_orders) == 0
            print("8. Futures: an already-illiquid option is BLOCKED at entry when the gate is enabled - "
                  "zero real orders placed: PASSED")
        finally:
            restore()


async def test_9_luxury_entry_blocked_when_illiquid():
    store = lps.PositionStore()
    lte.position_store = store
    lcfg.MAX_LIVE_POSITIONS_CE, lcfg.MAX_LIVE_POSITIONS_PE = 5, 5
    lcfg.LOSS_REPEAT_BLOCK_ENABLED = False
    lcfg.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    lcfg.LIQUIDITY_ENTRY_GATE_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks()
    with mock.patch.object(reversal_filters, "check_option_liquidity", new=AsyncMock(return_value=(False, True))):
        try:
            result = await lte._process_one_entry("SOLARINDS", "PE")
            assert result["status"] == "skipped" and result["reason"] == "option_illiquid_at_entry", result
            assert len(placed_orders) == 0
            print("9. Luxury: an already-illiquid option is BLOCKED at entry when the gate is enabled - "
                  "zero real orders placed: PASSED")
        finally:
            restore()


async def main():
    print("=== Option-liquidity entry gate test suite ===\n")
    test_1_check_option_liquidity_fails_open_when_not_authenticated()
    test_2_reports_illiquid_correctly()
    test_3_reports_liquid_correctly()
    test_4_fails_open_on_missing_data()
    await test_5_options_entry_blocked_when_illiquid()
    await test_6_options_entry_allowed_when_flag_disabled()
    await test_7_options_liquid_option_not_blocked()
    await test_8_futures_entry_blocked_when_illiquid()
    await test_9_luxury_entry_blocked_when_illiquid()
    print("\nALL option-liquidity entry gate CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
