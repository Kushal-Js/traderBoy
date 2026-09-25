"""
Tests for Swing v2's Copper/MCX broker-position reconciliation:
  1. get_broker_net_quantity(segment="MCX_COMM") reads get_open_mcx_
     positions(), NOT the NSE_FNO list (which would always report 0 for a
     real MCX position, silently orphaning it - see get_broker_net_
     quantity's own docstring).
  2. reconcile_broker_positions() picks up a mocked open Copper position
     (attributed to "Swing") at startup, with the correct pnl_multiplier
     (from Swing/mcx_registry.py's live registry), not the raw broker
     quantity.

HOW TO RUN:
    uv run python tests/test_swing_v2_mcx_reconcile.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_v2_mcx_reconcile_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc

import Swing.config as sc
import Swing.trading_engine as ste
from Swing.mcx_registry import mcx_registry

COPPER_PNL_MULTIPLIER = 2500  # real 2,500kg/lot - matches data/mcx_config's real COPPER entry


def test_1_get_broker_net_quantity_mcx_segment_reads_mcx_positions():
    original_mcx = odc.dhan_wrapper.get_open_mcx_positions
    original_fno = odc.dhan_wrapper.get_open_fno_positions
    try:
        odc.dhan_wrapper.get_open_mcx_positions = lambda: [
            {"trading_symbol": "COPPER 23 SEP 1100 CALL", "underlying_symbol": "COPPER",
             "option_type": "CE", "lot_size": 1, "quantity": 1, "avg_price": 25.0, "product_type": "MARGIN"},
        ]
        # A wrong implementation would fall through to this and always see 0.
        odc.dhan_wrapper.get_open_fno_positions = lambda: []

        qty = odc.dhan_wrapper.get_broker_net_quantity("COPPER 23 SEP 1100 CALL", segment="MCX_COMM")
        assert qty == 1, f"expected the real MCX position's quantity (1), got {qty}"

        qty_wrong_symbol = odc.dhan_wrapper.get_broker_net_quantity("SOME OTHER SYMBOL", segment="MCX_COMM")
        assert qty_wrong_symbol == 0, "an unrelated symbol must still read as flat"
        print("1. get_broker_net_quantity(segment='MCX_COMM') correctly reads get_open_mcx_positions, "
              "not the NSE_FNO list: PASSED")
    finally:
        odc.dhan_wrapper.get_open_mcx_positions = original_mcx
        odc.dhan_wrapper.get_open_fno_positions = original_fno


async def test_2_reconcile_broker_positions_picks_up_copper_with_real_multiplier():
    originals = {
        "get_open_fno_positions": odc.dhan_wrapper.get_open_fno_positions,
        "get_open_equity_positions": odc.dhan_wrapper.get_open_equity_positions,
        "get_open_mcx_positions": odc.dhan_wrapper.get_open_mcx_positions,
        "get_pending_order_id": odc.dhan_wrapper.get_pending_order_id,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
    }
    original_attribute = ste.attribute_open_broker_position
    try:
        odc.dhan_wrapper.get_open_fno_positions = lambda: []
        odc.dhan_wrapper.get_open_equity_positions = lambda: []
        # get_pending_order_id (broker-SL discovery) and subscribe_option_
        # price (WS-subscribe every reconciled MCX_COMM position) both
        # touch dhan_wrapper.client (real Dhan auth) if unmocked - pre-
        # existing gap in this test (confirmed present on HEAD before this
        # file's own mcx_registry-related edits too, not a regression).
        odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type, *_: None
        odc.dhan_wrapper.subscribe_option_price = lambda ts: None
        odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
        odc.dhan_wrapper.get_open_mcx_positions = lambda: [
            {"trading_symbol": "COPPER 23 SEP 1100 CALL", "underlying_symbol": "COPPER",
             "option_type": "CE", "lot_size": 1, "quantity": 1, "avg_price": 25.0, "product_type": "MARGIN"},
        ]

        # attribute_open_broker_position is a plain SYNC function, called
        # via run_in_executor inside reconcile_broker_positions - mock it
        # the same way, not as a coroutine function (which run_in_executor
        # would just return unawaited, never matching == "Swing").
        ste.attribute_open_broker_position = lambda trading_symbol: "Swing"
        await mcx_registry.set_symbol("COPPER", options_only=True, pnl_multiplier=COPPER_PNL_MULTIPLIER)

        positions = await ste.reconcile_broker_positions()
        assert len(positions) == 1, positions
        pos = positions[0]
        assert pos.underlying_symbol == "COPPER"
        assert pos.exchange_segment == "MCX_COMM"
        assert pos.basket_type == "OPTIONS"
        assert pos.instrument_side == "LONG"
        assert pos.quantity == 1, "the real order-placement quantity (lot-count) must be preserved as-is"
        expected_multiplier = COPPER_PNL_MULTIPLIER * sc.QUANTITY_LOTS
        assert pos.pnl_multiplier == expected_multiplier, \
            f"reconciled pnl_multiplier must come from mcx_registry, got {pos.pnl_multiplier} want {expected_multiplier}"
        print("2. reconcile_broker_positions() picks up a real open Copper position with the correct "
              "mcx_registry-derived pnl_multiplier (not the raw broker quantity): PASSED")
    finally:
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
        ste.attribute_open_broker_position = original_attribute


async def main():
    print("=== Swing v2 Copper/MCX reconciliation test suite ===\n")
    test_1_get_broker_net_quantity_mcx_segment_reads_mcx_positions()
    await test_2_reconcile_broker_positions_picks_up_copper_with_real_multiplier()
    print("\nALL SWING V2 MCX RECONCILE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
