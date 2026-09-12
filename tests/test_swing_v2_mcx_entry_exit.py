"""
Integration tests for Swing v2's Copper/MCX entry/exit routing - against
the REAL production functions (enter_position_for_stock / _exit_reason_
for), same "only the Dhan network boundary mocked" convention as
tests/test_swing_v2_entry_exit.py.

Coverage:
  1. is_mcx + BASKET_TYPE=="options" resolves via the MCX-capable get_atm_
     option, sets exchange_segment="MCX_COMM", places via place_mcx_
     market_order.
  2. is_mcx + BASKET_TYPE=="futures" is SKIPPED entirely - zero orders,
     symbol released - per the explicit scope restriction (Copper futures
     trading not enabled, options only for now).
  3. The exit-ladder's rupee checks (MAX_LOSS_HIT/PROFIT_PROTECTION_HIT)
     fire off pnl_multiplier, not the tiny order-placement quantity - a
     Copper position that would NEVER cross a rupee threshold at
     quantity=1 correctly DOES cross it at the real pnl_multiplier.
  4. WS subscribe/unsubscribe are never called for a Copper (MCX_COMM)
     position - it relies on the REST poll loop only, same as equity.

HOW TO RUN:
    uv run python tests/test_swing_v2_mcx_entry_exit.py
"""
import asyncio
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_v2_mcx_entry_exit_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

import Swing.config as sc
import Swing.trading_engine as ste
from Swing.position_store import unrealized_pnl_rs

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_copper_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE {option_type}", strike=1100.0, option_type=option_type,
                      lot_size=1, security_id=f"OPTSEC-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_mocks():
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "place_mcx_market_order": odc.dhan_wrapper.place_mcx_market_order,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_copper_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda ts: 25.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 100.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 1_000_000.0}

    placed_orders, ws_calls = [], {"subscribe": 0, "unsubscribe": 0}

    def _place_mcx(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        placed_orders.append({"trading_symbol": trading_symbol, "quantity": quantity, "transaction_type": transaction_type})
        return {"order_id": f"FAKE-MCX-{trading_symbol}-{len(placed_orders)}", "is_amo": False}

    odc.dhan_wrapper.place_mcx_market_order = _place_mcx
    odc.dhan_wrapper.subscribe_option_price = lambda ts: ws_calls.__setitem__("subscribe", ws_calls["subscribe"] + 1)
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: ws_calls.__setitem__("unsubscribe", ws_calls["unsubscribe"] + 1)
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=25.0, filled_quantity=1, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders, ws_calls


async def _async_none():
    return None


def _set(basket_type):
    sc.BASKET_TYPE = basket_type
    sc.BROKER_STOP_LOSS_ENABLED = False
    sc.MAX_CONCURRENT_TRADES = 2
    sc.FUNDS_CHECK_BUFFER_RS = 0.0
    ste.position_store.__init__()
    ste.signals.get_supertrend_state = lambda symbol: _async_none()


async def test_1_copper_options_entry_uses_mcx_routing():
    _set("options")
    restore, placed, ws_calls = install_mocks()
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["COPPER"]
        assert pos.exchange_segment == "MCX_COMM"
        assert pos.instrument_side == "LONG"
        assert len(placed) == 1, "exactly one real order should have been placed, via place_mcx_market_order"
        print("1. Copper OPTIONS entry resolves via the MCX-capable get_atm_option and routes to "
              "place_mcx_market_order with exchange_segment=MCX_COMM: PASSED")
    finally:
        restore()


async def test_2_copper_futures_entry_is_skipped_not_placed():
    _set("futures")
    restore, placed, ws_calls = install_mocks()
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "skipped", result
        assert result["reason"] == "mcx_futures_disabled", result
        assert "COPPER" not in ste.position_store.live_positions
        assert len(placed) == 0, "Copper futures trading must place ZERO real orders - options only for now"
        assert "COPPER" not in ste.position_store.reserved_symbols, \
            "a skipped entry must release/never reserve capacity"
        print("2. Copper FUTURES entry is skipped entirely (no order, no capacity consumed) - "
              "options-only scope restriction respected: PASSED")
    finally:
        restore()


async def test_3_exit_ladder_uses_pnl_multiplier_not_quantity():
    _set("options")
    restore, placed, ws_calls = install_mocks()
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["COPPER"]

        real_cap = sc.MAX_LOSS_PROTECTION_RS
        sc.MAX_LOSS_PROTECTION_RS = 1000.0
        try:
            # A 1-rupee adverse move at quantity=1 (the real order quantity)
            # would be a Rs 1 loss - nowhere near the Rs 1000 cap. At the
            # REAL pnl_multiplier (2500 * QUANTITY_LOTS), the same 1-rupee
            # move is Rs 2500+ - well past it. If _exit_reason_for used
            # position.quantity instead of pnl_multiplier here, this
            # MAX_LOSS_HIT would never fire.
            ltp = pos.entry_price - 1.0
            loss_at_quantity = -unrealized_pnl_rs(pos.instrument_side, pos.entry_price, ltp, pos.quantity)
            loss_at_multiplier = -unrealized_pnl_rs(pos.instrument_side, pos.entry_price, ltp, pos.pnl_multiplier)
            assert loss_at_quantity < sc.MAX_LOSS_PROTECTION_RS, \
                "sanity check: at the tiny order quantity, this move must NOT reach the cap"
            assert loss_at_multiplier >= sc.MAX_LOSS_PROTECTION_RS, \
                "sanity check: at the real pnl_multiplier, this move MUST reach the cap"
            reason = ste._exit_reason_for(pos, ltp)
            assert reason == "MAX_LOSS_HIT", \
                f"_exit_reason_for must use pnl_multiplier (would fire MAX_LOSS_HIT), got {reason}"
        finally:
            sc.MAX_LOSS_PROTECTION_RS = real_cap
        print("3. _exit_reason_for's rupee checks correctly use pnl_multiplier, not the tiny "
              "order-placement quantity: PASSED")
    finally:
        restore()


async def test_4_ws_never_subscribed_for_a_copper_position():
    _set("options")
    restore, placed, ws_calls = install_mocks()
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        assert ws_calls["subscribe"] == 0, \
            "an MCX position must never subscribe to WS ticks (unverified segment support - REST poll only)"
        print("4. WebSocket ticks are never subscribed for a Copper (MCX_COMM) position - "
              "REST poll loop only, same as equity: PASSED")
    finally:
        restore()


async def main():
    print("=== Swing v2 Copper/MCX entry-exit routing test suite ===\n")
    await test_1_copper_options_entry_uses_mcx_routing()
    await test_2_copper_futures_entry_is_skipped_not_placed()
    await test_3_exit_ladder_uses_pnl_multiplier_not_quantity()
    await test_4_ws_never_subscribed_for_a_copper_position()
    print("\nALL SWING V2 MCX ENTRY-EXIT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
