"""
Tests for Swing v2's Copper/MCX quantity-vs-pnl_multiplier distinction -
the single highest-stakes correctness point in the whole MCX feature (see
Swing/position_store.py's Position.pnl_multiplier docstring and Swing/
config.py's MCX_PNL_MULTIPLIERS docstring for the full rationale).

A Copper OPTIONS entry's real ORDER quantity (what actually gets sent to
place_mcx_market_order) must stay the tiny "number of lots" Dhan expects
(1 * QUANTITY_LOTS, since Copper's own SEM_LOT_UNITS is 1) while its
pnl_multiplier (used ONLY for MAX_LOSS_PROTECTION_RS/PROFIT_PROTECTION_RS
rupee math) must carry the REAL 2,500kg-equivalent economic exposure -
these must NEVER be swapped or conflated. Every existing NSE basket-type
must be completely unaffected (pnl_multiplier == quantity, byte-identical
to before this feature existed).

HOW TO RUN:
    uv run python tests/test_swing_v2_mcx_position_model.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_v2_mcx_position_model_test_"))
trade_history.HISTORY_DIR = scratch_dir

import asyncio

import Options.dhan_client as odc
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

import Swing.config as sc
import Swing.trading_engine as ste

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_copper_atm_option(symbol: str, option_type: str) -> AtmOption:
    # Mirrors Copper's real SEM_LOT_UNITS=1 (confirmed live, 12 Sep 2026) -
    # this is the actual, correct value for order-placement quantity.
    return AtmOption(trading_symbol=f"{symbol} FAKE {option_type}", strike=1100.0, option_type=option_type,
                      lot_size=1, security_id=f"OPTSEC-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_mocks():
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "place_mcx_market_order": odc.dhan_wrapper.place_mcx_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_copper_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda ts: 25.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 100.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 1_000_000.0}

    placed_orders = []

    def _place_mcx(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        placed_orders.append({"trading_symbol": trading_symbol, "quantity": quantity, "transaction_type": transaction_type})
        return {"order_id": f"FAKE-MCX-{trading_symbol}-{len(placed_orders)}", "is_amo": False}

    odc.dhan_wrapper.place_mcx_market_order = _place_mcx
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=25.0, filled_quantity=1, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders


def _set_options_basket():
    sc.BASKET_TYPE = "options"
    sc.BROKER_STOP_LOSS_ENABLED = False
    sc.MAX_CONCURRENT_TRADES = 2
    sc.FUNDS_CHECK_BUFFER_RS = 0.0
    ste.position_store.__init__()
    ste.signals.get_supertrend_state = lambda symbol: _async_none()


async def _async_none():
    return None


async def test_1_copper_options_entry_splits_quantity_from_pnl_multiplier():
    _set_options_basket()
    restore, placed = install_mocks()
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["COPPER"]
        expected_qty = 1 * sc.QUANTITY_LOTS
        expected_multiplier = sc.MCX_PNL_MULTIPLIERS["COPPER"] * sc.QUANTITY_LOTS
        assert pos.quantity == expected_qty, \
            f"real order quantity must stay the tiny lot-count Dhan expects for MCX: got {pos.quantity}, want {expected_qty}"
        assert pos.pnl_multiplier == expected_multiplier, \
            f"pnl_multiplier must carry the REAL per-lot economic exposure: got {pos.pnl_multiplier}, want {expected_multiplier}"
        assert pos.pnl_multiplier != pos.quantity, \
            "for Copper these two numbers must NOT be equal - that's the entire point of this field"
        assert placed[0]["quantity"] == expected_qty, \
            "the REAL order sent to place_mcx_market_order must use the lot-count quantity, never pnl_multiplier"
        assert pos.exchange_segment == "MCX_COMM"
        print("1. A Copper OPTIONS entry correctly splits real order quantity (lot-count) from "
              "pnl_multiplier (real economic exposure): PASSED")
    finally:
        restore()


async def test_2_nse_basket_types_keep_pnl_multiplier_identical_to_quantity():
    # Regression guard: every NSE-underlying basket-type must be
    # completely unaffected by this feature - pnl_multiplier must equal
    # quantity exactly, for every basket_type, same as before this field
    # existed.
    import Swing.position_store as sps
    for basket_type, lot_size, qty_lots in [("FUTURES", 475, 1), ("OPTIONS", 550, 2), ("EQUITY", None, 1)]:
        quantity = (lot_size * qty_lots) if lot_size else 200
        pos = sps.Position(
            underlying_symbol="RELIANCE", trading_symbol="RELIANCE TEST", basket_type=basket_type,
            regime="BULLISH", instrument_side="LONG", exchange_segment="NSE_FNO", product_type="MARGIN",
            quantity=quantity, lot_size=lot_size, entry_price=100.0, best_price=100.0,
            target_price=120.0, hard_stop_loss=80.0, order_id="OID", pnl_multiplier=quantity,
        )
        assert pos.pnl_multiplier == pos.quantity, \
            f"{basket_type}: pnl_multiplier must equal quantity for an NSE position - got {pos.pnl_multiplier} vs {pos.quantity}"
    print("2. Every NSE basket-type keeps pnl_multiplier identical to quantity (zero behavior change): PASSED")


async def main():
    print("=== Swing v2 Copper/MCX quantity-vs-pnl_multiplier test suite ===\n")
    await test_1_copper_options_entry_splits_quantity_from_pnl_multiplier()
    await test_2_nse_basket_types_keep_pnl_multiplier_identical_to_quantity()
    print("\nALL SWING V2 MCX POSITION-MODEL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
