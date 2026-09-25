"""
Tests for Swing's volume-floor entry gate - originally MCX-only (promoted
from shadow-mode analysis to a live gate, 16 Sep 2026 - user request:
"enable this to SWING strategy but for MCX only and make it flag
enabled/disabled but make it enabled as of now"), extended to every
non-MCX watchlist symbol on 18 Sep 2026 after investigating a real
ANGELONE 29 SEP 295 PUT loss of Rs 4,125 on 17 Sep 2026 - that entry had
a shadow reversal-filter VolRatio of 0.01 (near-zero entry-candle volume)
that was logged but never enforced outside MCX. The MCX and NSE gates are
independently configured (separate enabled flags and thresholds) even
though the underlying check is identical.

Coverage:
  1. An MCX symbol (COPPER) with a thin entry-candle volume (< the
     configured floor) is BLOCKED when the MCX gate is enabled - no
     reservation taken, no order placed.
  2. The SAME thin-volume MCX symbol is allowed through when the MCX gate
     is DISABLED - proves this is a genuine, real-time-flippable flag,
     not baked into the entry logic unconditionally.
  3. An NSE-equity watchlist symbol (e.g. ADANIPORTS, not in MCX_SYMBOLS)
     with a thin entry-candle volume is BLOCKED when the NSE gate is
     enabled - the extension this file's own header describes.
  4. The SAME thin-volume NSE symbol is allowed through when the NSE gate
     is disabled - independently flippable, same as the MCX gate.
  5. The two gates are genuinely INDEPENDENT: MCX enabled + NSE disabled
     blocks COPPER but lets an equally-thin ADANIPORTS entry through (and
     the mirror configuration the other way around).
  6. A healthy (>= floor) volume ratio on an MCX symbol is NOT blocked -
     the gate only rejects genuinely thin candles, not every MCX entry.
  7. Missing volume data (volume_ratio=None, e.g. get_supertrend_state
     itself returned None) fails OPEN for MCX - never blocks on missing
     information, only on a CONFIRMED thin reading.
  8. The same healthy-volume-not-blocked check for the NSE gate.
  9. The same missing-data-fails-open check for the NSE gate.

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
from Swing.mcx_registry import mcx_registry
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
        "get_last_historical_close": odc.dhan_wrapper.get_last_historical_close,
        "get_cached_option_ltp": odc.dhan_wrapper.get_cached_option_ltp,
        "note_rest_ltp": odc.dhan_wrapper.note_rest_ltp,
        "place_mcx_stop_loss_limit_order": odc.dhan_wrapper.place_mcx_stop_loss_limit_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "is_mcx_commodity": odc.dhan_wrapper.is_mcx_commodity,
    }
    # Avoids a real instrument-master/Dhan-login call - moved off the old
    # static sc.MCX_SYMBOLS set 25 Sep 2026 (see Swing/mcx_registry.py).
    odc.dhan_wrapper.is_mcx_commodity = lambda symbol: symbol.upper() == "COPPER"
    # config.BROKER_STOP_LOSS_ENABLED is never overridden by this file's
    # own _set() (unlike every OTHER Swing test file), so every test here
    # picks up the AMBIENT real .env value - which is true - meaning every
    # "entered" result below (tests 2-5) would otherwise fall through to a
    # REAL, unmocked broker-side stop-loss placement attempt
    # (place_mcx_stop_loss_limit_order for COPPER, place_stop_loss_limit_
    # order for ADANIPORTS in test_3) touching dhan_wrapper.client for
    # real - this is what was actually causing the repeated real Dhan
    # login attempts (one per successful entry test), not the LTP-cache
    # gap alone.
    odc.dhan_wrapper.place_mcx_stop_loss_limit_order = lambda *a, **k: {"order_id": "FAKE-SL-MCX"}
    odc.dhan_wrapper.place_stop_loss_limit_order = lambda *a, **k: {"order_id": "FAKE-SL"}
    # get_cached_option_ltp/note_rest_ltp both call the REAL _instrument_
    # meta internally, which touches dhan_wrapper.client - a lazy property
    # that triggers a genuine Dhan login if unmocked (see trading-skills'
    # incidents/2026-09-08-test-suite-real-auth-leak.md and its
    # recurrence 3 days later - this file was missed when that class of
    # gap was fixed elsewhere, and made real login attempts, including
    # tripping Dhan's own account-level rate limiter, before this fix).
    odc.dhan_wrapper.get_cached_option_ltp = lambda trading_symbol: None
    odc.dhan_wrapper.note_rest_ltp = lambda trading_symbol, ltp: None
    odc.dhan_wrapper.get_atm_option = lambda symbol, option_type: _mcx_option(symbol, option_type)
    odc.dhan_wrapper.get_option_ltp = lambda ts: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 100.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 1_000_000.0}
    odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type, *_: None
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


async def _set(mcx_gate_enabled, nse_gate_enabled=False, floor_ratio=1.2):
    sc.BASKET_TYPE = "options"
    await mcx_registry.set_symbol("COPPER", options_only=True, pnl_multiplier=2500)
    sc.MCX_VOLUME_FLOOR_GATE_ENABLED = mcx_gate_enabled
    sc.MCX_VOLUME_FLOOR_RATIO_MIN = floor_ratio
    sc.NSE_VOLUME_FLOOR_GATE_ENABLED = nse_gate_enabled
    sc.NSE_VOLUME_FLOOR_RATIO_MIN = floor_ratio
    sc.MAX_CONCURRENT_TRADES = 2
    ste.position_store.__init__()


async def test_1_thin_mcx_volume_blocked_when_gate_enabled():
    await _set(mcx_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.5))
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "mcx_volume_floor_gate", result
        assert len(placed) == 0, "no real order should have been placed"
        assert "COPPER" not in ste.position_store.reserved_symbols
        print("1. Thin MCX volume (0.5x < 1.2x floor) is BLOCKED when the MCX gate is enabled - zero orders placed: PASSED")
    finally:
        restore()


async def test_2_same_thin_volume_allowed_when_gate_disabled():
    await _set(mcx_gate_enabled=False)
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


async def test_3_thin_nse_volume_blocked_when_nse_gate_enabled():
    """The extension: ADANIPORTS is NOT in MCX_SYMBOLS, so is_mcx=False -
    this now goes through the NSE gate instead of skipping volume checks
    entirely. Mirrors the real ANGELONE incident's VolRatio=0.01 entry."""
    await _set(mcx_gate_enabled=False, nse_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.01))
    try:
        result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert result["status"] == "skipped" and result["reason"] == "nse_volume_floor_gate", result
        assert len(placed) == 0, "no real order should have been placed"
        assert "ADANIPORTS" not in ste.position_store.reserved_symbols
        print("3. Thin NSE volume (0.01x < 1.2x floor, the real ANGELONE incident's own ratio) is BLOCKED "
              "when the NSE gate is enabled - zero orders placed: PASSED")
    finally:
        restore()


async def test_4_same_thin_nse_volume_allowed_when_nse_gate_disabled():
    await _set(mcx_gate_enabled=False, nse_gate_enabled=False)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.01))
    try:
        result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert result["status"] == "entered", result
        assert len(placed) == 1
        print("4. The SAME thin NSE volume (0.01x) is allowed through when NSE_VOLUME_FLOOR_GATE_ENABLED=False "
              "- independently flippable, same as the MCX gate: PASSED")
    finally:
        restore()


async def test_5_mcx_and_nse_gates_are_genuinely_independent():
    """MCX enabled + NSE disabled: COPPER (thin) is blocked, ADANIPORTS
    (equally thin) is not - and the mirror configuration the other way -
    proving these are two separate flags/thresholds, not one shared one."""
    await _set(mcx_gate_enabled=True, nse_gate_enabled=False)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.3))
    try:
        copper_result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert copper_result["status"] == "skipped" and copper_result["reason"] == "mcx_volume_floor_gate", copper_result
        adaniports_result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert adaniports_result["status"] == "entered", adaniports_result
    finally:
        restore()

    await _set(mcx_gate_enabled=False, nse_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=0.3))
    try:
        copper_result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert copper_result["status"] == "entered", copper_result
        adaniports_result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert adaniports_result["status"] == "skipped" and adaniports_result["reason"] == "nse_volume_floor_gate", \
            adaniports_result
        print("5. The MCX and NSE volume-floor gates are genuinely independent - either can be enabled/disabled "
              "without affecting the other's own symbols: PASSED")
    finally:
        restore()


async def test_6_healthy_mcx_volume_not_blocked():
    await _set(mcx_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=2.5))
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        assert len(placed) == 1
        print("6. A healthy (2.5x >= 1.2x floor) MCX volume ratio is NOT blocked - "
              "the gate only rejects genuinely thin candles: PASSED")
    finally:
        restore()


async def test_7_missing_volume_data_fails_open_mcx():
    await _set(mcx_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(None)
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] != "skipped" or result.get("reason") != "mcx_volume_floor_gate", \
            f"missing signal data (volume_ratio unknown) must never be treated as a confirmed thin candle: {result}"
        assert result["status"] == "entered", result
        print("7. Missing volume data (get_supertrend_state returned None) fails OPEN on the MCX volume gate "
              "specifically - proceeds to a real entry rather than blocking on missing information: PASSED")
    finally:
        restore()


async def test_8_healthy_nse_volume_not_blocked():
    await _set(mcx_gate_enabled=False, nse_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(_fake_st(volume_ratio=2.5))
    try:
        result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert result["status"] == "entered", result
        assert len(placed) == 1
        print("8. A healthy (2.5x >= 1.2x floor) NSE volume ratio is NOT blocked - "
              "the NSE gate only rejects genuinely thin candles: PASSED")
    finally:
        restore()


async def test_9_missing_volume_data_fails_open_nse():
    await _set(mcx_gate_enabled=False, nse_gate_enabled=True)
    restore, placed = install_mocks()
    ste.signals.get_supertrend_state = lambda symbol, interval_minutes=None: _async(None)
    try:
        result = await ste.enter_position_for_stock("ADANIPORTS", "BULLISH")
        assert result["status"] != "skipped" or result.get("reason") != "nse_volume_floor_gate", \
            f"missing signal data (volume_ratio unknown) must never be treated as a confirmed thin candle: {result}"
        assert result["status"] == "entered", result
        print("9. Missing volume data (get_supertrend_state returned None) fails OPEN on the NSE volume gate "
              "specifically - proceeds to a real entry rather than blocking on missing information: PASSED")
    finally:
        restore()


async def _async(value):
    return value


async def main():
    print("=== Swing volume-floor gate (MCX + NSE) test suite ===\n")
    await test_1_thin_mcx_volume_blocked_when_gate_enabled()
    await test_2_same_thin_volume_allowed_when_gate_disabled()
    await test_3_thin_nse_volume_blocked_when_nse_gate_enabled()
    await test_4_same_thin_nse_volume_allowed_when_nse_gate_disabled()
    await test_5_mcx_and_nse_gates_are_genuinely_independent()
    await test_6_healthy_mcx_volume_not_blocked()
    await test_7_missing_volume_data_fails_open_mcx()
    await test_8_healthy_nse_volume_not_blocked()
    await test_9_missing_volume_data_fails_open_nse()
    print("\nALL Swing volume-floor gate (MCX + NSE) tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
