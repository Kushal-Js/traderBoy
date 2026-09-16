"""
Tests for Swing's MCX-only volume-floor entry gate (promoted from
shadow-mode analysis to a live gate, 16 Sep 2026 - user request: "enable
this to SWING strategy but for MCX only and make it flag enabled/disabled
but make it enabled as of now").

Coverage:
  1. An MCX symbol (COPPER) with a thin entry-candle volume (< the
     configured floor) is BLOCKED when the gate is enabled - no
     reservation taken, no order placed.
  2. The SAME thin-volume MCX symbol is allowed through when the gate is
     DISABLED (config.MCX_VOLUME_FLOOR_GATE_ENABLED = False) - proves
     this is a genuine, real-time-flippable flag, not baked into the
     entry logic unconditionally.
  3. An NSE-EQUITY watchlist symbol (e.g. ADANIPORTS, not in MCX_SYMBOLS)
     is NEVER gated by this, even with an equally thin volume ratio -
     the user's explicit "for MCX only" scoping.
  4. A healthy (>= floor) volume ratio on an MCX symbol is NOT blocked -
     the gate only rejects genuinely thin candles, not every MCX entry.
  5. Missing volume data (volume_ratio=None, e.g. get_supertrend_state
     itself returned None) fails OPEN - never blocks on missing
     information, only on a CONFIRMED thin reading.

HOW TO RUN:
    uv run python tests/test_swing_mcx_volume_floor_gate.py
"""
import asyncio
import os
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history
import tempfile
scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_mcx_volume_floor_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

import Swing.config as sc
import Swing.signals as signals
import Swing.trading_engine as ste
from Swing.signals import SupertrendState

_REAL_GET_SUPERTREND_STATE = ste.signals.get_supertrend_state

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def _fake_st(volume_ratio):
    return SupertrendState(
        candle_start=None, close=100.0, supertrend=95.0, is_above=True,
        prev_close=94.0, prev_supertrend=95.0, prev_is_above=False,
        volume=1000.0, volume_ratio=volume_ratio,
    )


def _mcx_option(symbol, option_type):
    return AtmOption(trading_symbol=f"{symbol} FAKE {option_type}", strike=1000.0, option_type=option_type,
                      lot_size=1, security_id=f"OPTSEC-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_mocks(entry_fill_status=OrderStatus.TRADED):
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "get_pending_order_id": odc.dhan_wrapper.get_pending_order_id,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_mcx_market_order": getattr(odc.dhan_wrapper, "place_mcx_market_order", None),
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = lambda symbol, option_type: _mcx_option(symbol, option_type)
    odc.dhan_wrapper.get_option_ltp = lambda ts: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 100.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 1_000_000.0}
    odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: None
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None

    placed_orders = []

    def _place(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type, "quantity": quantity})
        return {"order_id": order_id, "is_amo": False}

    odc.dhan_wrapper.place_mcx_market_order = _place
    odc.dhan_wrapper.place_market_order = _place
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=entry_fill_status, remark="", fill_price=100.0, filled_quantity=1, is_amo=False)

    def restore():
        for name, fn in originals.items():
            if fn is not None:
                setattr(odc.dhan_wrapper, name, fn)
        ste.signals.get_supertrend_state = _REAL_GET_SUPERTREND_STATE

    return restore, placed_orders


def _set(gate_enabled, floor_ratio=1.2):
    sc.BASKET_TYPE = "options"
    sc.MCX_SYMBOLS = {"COPPER"}
    sc.MCX_OPTIONS_ONLY_SYMBOLS = {"COPPER"}
    sc.MCX_PNL_MULTIPLIERS = {"COPPER": 2500}
    sc.MCX_VOLUME_FLOOR_GATE_ENABLED = gate_enabled
    sc.MCX_VOLUME_FLOOR_RATIO_MIN = floor_ratio
    sc.MAX_CONCURRENT_TRADES = 2
    ste.position_store.__init__()


async def test_1_thin_mcx_volume_blocked_when_gate_enabled():
    _set(gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.5))
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "mcx_volume_floor_gate", result
        assert len(placed) == 0, "no real order should have been placed"
        assert "COPPER" not in ste.position_store.reserved_symbols
        print("1. Thin MCX volume (0.5x < 1.2x floor) is BLOCKED when the gate is enabled - zero orders placed: PASSED")
    finally:
        restore()


async def test_2_same_thin_volume_allowed_when_gate_disabled():
    _set(gate_enabled=False)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.5))
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        assert len(placed) == 1
        print("2. The SAME thin volume (0.5x) is allowed through when MCX_VOLUME_FLOOR_GATE_ENABLED=False "
              "- confirms this is a real, flippable flag: PASSED")
    finally:
        restore()


async def test_3_nse_equity_symbol_never_gated_regardless_of_volume():
    _set(gate_enabled=True)
    restore, placed = install_mocks()
    # ADANIPORTS is NOT in MCX_SYMBOLS - is_mcx=False, gate must never apply
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.1))
    try:
        result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert result["status"] == "entered", result
        assert len(placed) == 1
        print("3. An NSE-equity watchlist symbol (ADANIPORTS) is NEVER gated by the MCX-only volume floor, "
              "even with an equally thin (0.1x) volume ratio: PASSED")
    finally:
        restore()


async def test_4_healthy_mcx_volume_not_blocked():
    _set(gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=2.5))
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        assert len(placed) == 1
        print("4. A healthy (2.5x >= 1.2x floor) MCX volume ratio is NOT blocked - "
              "the gate only rejects genuinely thin candles: PASSED")
    finally:
        restore()


async def test_5_missing_volume_data_fails_open():
    _set(gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(None)
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] != "skipped" or result.get("reason") != "mcx_volume_floor_gate", \
            f"missing signal data (volume_ratio unknown) must never be treated as a confirmed thin candle: {result}"
        assert result["status"] == "entered", result
        print("5. Missing volume data (get_supertrend_state returned None) fails OPEN on the volume gate "
              "specifically - proceeds to a real entry rather than blocking on missing information: PASSED")
    finally:
        restore()


async def _async(value):
    return value


async def main():
    print("=== Swing MCX-only volume-floor gate test suite ===\n")
    await test_1_thin_mcx_volume_blocked_when_gate_enabled()
    await test_2_same_thin_volume_allowed_when_gate_disabled()
    await test_3_nse_equity_symbol_never_gated_regardless_of_volume()
    await test_4_healthy_mcx_volume_not_blocked()
    await test_5_missing_volume_data_fails_open()
    print("\nALL Swing MCX volume-floor gate tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
