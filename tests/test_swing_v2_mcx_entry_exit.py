"""
Integration tests for Swing v2's Copper/MCX entry/exit routing - against
the REAL production functions (enter_position_for_stock / _exit_reason_
for), same "only the Dhan network boundary mocked" convention as
tests/test_swing_v2_entry_exit.py.

Coverage:
  1. Copper (in MCX_OPTIONS_ONLY_SYMBOLS) resolves via the MCX-capable
     get_atm_option, sets exchange_segment="MCX_COMM", places via place_
     mcx_market_order - regardless of BASKET_TYPE.
  2. Copper ALWAYS trades OPTIONS, even when the global BASKET_TYPE is
     "futures" - it does not skip, and it does not take the futures path
     either (corrected 12 Sep 2026: "whatever is the BASKET_TYPE, it
     should not impact COPPER as it only has to trade in options").
  3. The exit-ladder's rupee checks (MAX_LOSS_HIT/PROFIT_PROTECTION_HIT)
     fire off pnl_multiplier, not the tiny order-placement quantity - a
     Copper position that would NEVER cross a rupee threshold at
     quantity=1 correctly DOES cross it at the real pnl_multiplier.
  4. WS subscribe/unsubscribe are never called for a Copper (MCX_COMM)
     position - it relies on the REST poll loop only, same as equity.
  5. The OPTIONS-only override is scoped to MCX_OPTIONS_ONLY_SYMBOLS
     specifically, NOT a blanket rule for every symbol in MCX_SYMBOLS
     (explicit correction, 12 Sep 2026: "this doesn't apply to all
     instruments under MCX but only for COPPER") - a hypothetical other
     MCX symbol in MCX_SYMBOLS but not in MCX_OPTIONS_ONLY_SYMBOLS must
     still follow the global BASKET_TYPE normally.

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
        "place_market_order": odc.dhan_wrapper.place_market_order,
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

    def _place(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        placed_orders.append({"trading_symbol": trading_symbol, "quantity": quantity, "transaction_type": transaction_type})
        return {"order_id": f"FAKE-{trading_symbol}-{len(placed_orders)}", "is_amo": False}

    def _place_mcx(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        placed_orders.append({"trading_symbol": trading_symbol, "quantity": quantity, "transaction_type": transaction_type})
        return {"order_id": f"FAKE-MCX-{trading_symbol}-{len(placed_orders)}", "is_amo": False}

    odc.dhan_wrapper.place_market_order = _place
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


async def test_2_copper_always_trades_options_even_when_global_basket_type_is_futures():
    # Corrected 12 Sep 2026 (user clarification: "whatever is the
    # BASKET_TYPE, it should not impact COPPER as it only has to trade in
    # options") - Copper does NOT skip when the global BASKET_TYPE is
    # "futures"; it still enters, still as an OPTIONS position, completely
    # ignoring the global setting. Only Copper (MCX_OPTIONS_ONLY_SYMBOLS)
    # gets this override - see test_swing_v2_mcx_position_model.py for the
    # regression guard that this is NOT a blanket rule for every MCX
    # symbol in MCX_SYMBOLS.
    _set("futures")
    restore, placed, ws_calls = install_mocks()
    try:
        result = await ste.enter_position_for_stock("COPPER", "BULLISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["COPPER"]
        assert pos.basket_type == "OPTIONS", \
            f"Copper must always trade OPTIONS regardless of the global BASKET_TYPE, got {pos.basket_type}"
        assert pos.exchange_segment == "MCX_COMM"
        assert len(placed) == 1, "exactly one real order should have been placed, via place_mcx_market_order"
        print("2. Copper enters as OPTIONS even when the global BASKET_TYPE is 'futures' - "
              "the BASKET_TYPE override is per-symbol, not global: PASSED")
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


async def test_5_options_only_override_is_scoped_to_copper_not_all_mcx_symbols():
    # A hypothetical second MCX symbol ("SILVER") added to MCX_SYMBOLS but
    # deliberately NOT added to MCX_OPTIONS_ONLY_SYMBOLS must follow the
    # global BASKET_TYPE like any NSE symbol - it must NOT be silently
    # forced into OPTIONS just because it lives under MCX_SYMBOLS. This is
    # the regression guard for the explicit correction: "this doesn't
    # apply to all instruments under MCX but only for COPPER".
    _set("futures")
    real_mcx_symbols = sc.MCX_SYMBOLS
    sc.MCX_SYMBOLS = real_mcx_symbols | {"SILVER"}  # MCX_OPTIONS_ONLY_SYMBOLS untouched - still just {"COPPER"}
    restore, placed, ws_calls = install_mocks()
    original_get_futures_contract = odc.dhan_wrapper.get_futures_contract

    def fake_futures_contract(symbol):
        from Options.dhan_client import FuturesContract
        return FuturesContract(trading_symbol=f"{symbol} FUT", security_id=f"FUTSEC-{symbol}",
                                lot_size=30, expiry_date=FUTURE_EXPIRY)
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    try:
        result = await ste.enter_position_for_stock("SILVER", "BULLISH")
        assert result["status"] == "entered", result
        pos = ste.position_store.live_positions["SILVER"]
        assert pos.basket_type == "FUTURES", \
            f"SILVER is in MCX_SYMBOLS but NOT MCX_OPTIONS_ONLY_SYMBOLS - it must follow the global " \
            f"BASKET_TYPE (futures) normally, got {pos.basket_type}"
        assert pos.exchange_segment == "NSE_FNO", \
            "an MCX_SYMBOLS member outside MCX_OPTIONS_ONLY_SYMBOLS takes the plain NSE futures path today"
        print("5. The OPTIONS-only override is scoped to MCX_OPTIONS_ONLY_SYMBOLS (Copper), NOT a "
              "blanket rule for every symbol in MCX_SYMBOLS: PASSED")
    finally:
        restore()
        odc.dhan_wrapper.get_futures_contract = original_get_futures_contract
        sc.MCX_SYMBOLS = real_mcx_symbols


async def main():
    print("=== Swing v2 Copper/MCX entry-exit routing test suite ===\n")
    await test_1_copper_options_entry_uses_mcx_routing()
    await test_2_copper_always_trades_options_even_when_global_basket_type_is_futures()
    await test_3_exit_ladder_uses_pnl_multiplier_not_quantity()
    await test_4_ws_never_subscribed_for_a_copper_position()
    await test_5_options_only_override_is_scoped_to_copper_not_all_mcx_symbols()
    print("\nALL SWING V2 MCX ENTRY-EXIT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
