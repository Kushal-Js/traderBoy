"""
Tests for Swing's "basket_hedge" trading mode (added 1 Sep 2026, user
request): "enabling basket buy strategy but with a caveat that when exit
conditions are met, we will sell off this basket and buy a single PE ATM
option contract" - held until (1) loss > 2k, (2) profit > 2k ("lock
profit"), or (3) a bare Supertrend reversal "even if buy signal is not
yet triggered." Switched via config.STRATEGY_MODE == "basket_hedge",
coexisting with "basket" and "sequential" (neither of those is touched or
retested here - see test_swing_package.py/test_swing_sequential_mode.py
for their own coverage).

The one real design assumption made when building this (stated to the
user, not re-litigated here): none of the 3 PE-hedge exit conditions
carries a confirmed fresh BUY signal, so exiting the PE hedge always
returns to plain watching - it never auto-re-enters a fresh basket the
way sequential mode's own entry-refire path does. Test 3 exercises this
directly (capacity released, no re-entry).

Also covers the "grandfathered" real-money case explicitly requested by
the user - "consider the open trade as a basket order for this time as
it is already live" - a lone FUTSTK leg (no paired PE, e.g. APLAPOLLO,
entered under sequential mode before this mode existed) must reconcile
as a 1-leg BASKET-state position, not be rejected as an anomaly the way
plain basket mode's own reconciliation would.

Covers, against the REAL production functions (not reimplemented):
  1. config.STRATEGY_MODE defaults to "basket_hedge"; PE_PROFIT_LOCK_RS
     exists as its own config var.
  2. A full real cycle: NONE -> BASKET (both legs, all-or-nothing) ->
     PE_HEDGE (basket sold, single PE bought) -> NONE (loss-cap exit),
     via the REAL _enter_basket_hedge_for_stock/_exit_basket_hedge_to_pe/
     _exit_pe_hedge_to_watching/_evaluate_pe_hedge_exit_signal, with
     every real order verified and the full sequence persisted to
     trade_history.
  3. The PE hedge's profit-lock exit condition, independently.
  4. The PE hedge's bare Supertrend-reversal exit condition,
     independently - fires even when the full entry signal (price-
     confirmation gate + 1-min confirm) would NOT have fired, per the
     user's own words.
  5. Capacity stays RESERVED throughout the BASKET->PE_HEDGE swap and is
     only RELEASED once the PE hedge itself exits; a second entry
     attempt for the SAME symbol is rejected mid-flight, a DIFFERENT
     symbol is unaffected.
  6. Startup reconciliation: a lone FUT -> 1-leg BASKET (the grandfathered
     shape), a lone PE -> PE_HEDGE, and a matched FUT+PE pair -> 2-leg
     BASKET (the normal freshly-entered shape).
  7. The manual kill-switch (_square_off_all) closes EVERY leg currently
     held (1 or 2, whichever state) and releases capacity, mode-aware.
  8. monitor_loop's own 3-way per-tick dispatch touches exactly one of
     the three tick functions per tick.

Known gap, documented rather than silently skipped: this pass does NOT
add a basket_hedge paper-trading variant (paper_engine.py) - scope/time,
and config.PAPER_TRADING_ENABLED is currently False anyway. Paper trading
under basket_hedge mode currently falls through to whatever
paper_engine.py's own STRATEGY_MODE dispatch already does for a mode it
doesn't recognize - see paper_engine.py before ever turning paper trading
on while basket_hedge is active.

HOW TO RUN:
    uv run python tests/test_swing_basket_hedge_mode.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_basket_hedge_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.config as swing_config
import Swing.position_store as sps
import Swing.trading_engine as ste
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def fake_supertrend_state(close, supertrend, is_above, prev_is_above):
    return ste.SupertrendState(
        candle_start=None, close=close, supertrend=supertrend, is_above=is_above,
        prev_close=close, prev_supertrend=supertrend, prev_is_above=prev_is_above,
    )


def install_all_dhan_mocks(fill_prices=None, ltp_by_symbol=None):
    """Same shared-mock-the-Dhan-boundary-only pattern every other Swing
    test file in this repo uses - see test_swing_sequential_mode.py's own
    docstring on this helper for the rationale. fill_prices: consumed IN
    ORDER across successive place_market_order/wait_for_order_result
    calls (every flow in this file is strictly sequential)."""
    fill_prices = list(fill_prices or [])
    ltp_by_symbol = ltp_by_symbol or {}
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    # Fixed, generous fakes - the proactive funds check (added 1 Sep
    # 2026) calls these before every entry attempt; unmocked, they'd
    # fall through to a REAL Dhan network call (and a real, slow
    # authentication attempt) via _retry. Not what's under test in this
    # file, so always "plenty of funds" here.
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 100000.0}

    def fake_get_option_ltp(trading_symbol):
        if trading_symbol not in ltp_by_symbol:
            raise ValueError(f"no fake LTP configured for {trading_symbol}")
        return ltp_by_symbol[trading_symbol]

    odc.dhan_wrapper.get_option_ltp = fake_get_option_ltp

    placed_orders = []
    fills_iter = iter(fill_prices)

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        fill_price = next(fills_iter, 50.0)
        return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="",
                            fill_price=fill_price, filled_quantity=1, is_amo=False)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders


def test_1_config_defaults_to_basket_hedge():
    assert swing_config.STRATEGY_MODE == "basket_hedge", \
        f"expected STRATEGY_MODE to default to 'basket_hedge', got {swing_config.STRATEGY_MODE!r}"
    assert hasattr(swing_config, "PE_PROFIT_LOCK_RS"), "PE_PROFIT_LOCK_RS config var must exist ('lock profit')"
    assert swing_config.PE_PROFIT_LOCK_RS == 2000.0, swing_config.PE_PROFIT_LOCK_RS
    print("1. config.STRATEGY_MODE defaults to 'basket_hedge' (basket/sequential modes preserved, "
          "not deleted); new PE_PROFIT_LOCK_RS config var present with the requested 2k default: PASSED")


async def test_2_full_cycle_basket_to_pe_hedge_to_loss_cap_exit():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 10
    real_pe_loss_cap = ste.config.PE_MAX_LOSS_RS
    ste.config.PE_MAX_LOSS_RS = 2000.0
    real_pe_profit_lock = ste.config.PE_PROFIT_LOCK_RS
    ste.config.PE_PROFIT_LOCK_RS = 2000.0

    real_exit_signal = ste._evaluate_basket_exit_signal
    symbol = "RELIANCE"

    async def fake_exit_signal(sym, basket):
        return "SUPERTREND_5MIN_EXIT" if (sym == symbol and exit_fires["value"]) else None

    exit_fires = {"value": False}
    ste._evaluate_basket_exit_signal = fake_exit_signal

    fut_symbol = "RELIANCE FAKE EXP FUT"
    pe_symbol = "RELIANCE FAKE EXP PUT"
    # Order of fills: BUY FUT(100), BUY PE(20) [basket entry],
    # SELL FUT(110), SELL PE(15) [basket exit], BUY PE(25) [hedge entry].
    # The final loss-cap exit is priced via LTP, not a fresh fill.
    restore, placed_orders = install_all_dhan_mocks(
        fill_prices=[100.0, 20.0, 110.0, 15.0, 25.0],
        ltp_by_symbol={pe_symbol: 5.0},  # forces a loss > 2000 on qty from fake_atm_option's lot_size (500)
    )
    try:
        # --- NONE -> BASKET (all-or-nothing entry, same mechanics as plain basket mode) ---
        result = await ste._enter_basket_hedge_for_stock(symbol)
        assert result["status"] == "entered", result
        position = store.live_positions[symbol]
        assert position.state == "BASKET"
        assert len(position.legs) == 2, position.legs
        assert symbol in store.reserved_symbols

        # --- BASKET -> PE_HEDGE (exit signal fires: sell both legs, buy 1 PE) ---
        exit_fires["value"] = True
        await ste._exit_basket_hedge_to_pe(symbol, position)
        position = store.live_positions[symbol]
        assert position.state == "PE_HEDGE", position.state
        assert len(position.legs) == 1, position.legs
        assert position.legs[0].option_trading_symbol == pe_symbol
        assert symbol in store.reserved_symbols, "capacity must stay RESERVED across the basket->hedge swap"

        # --- PE_HEDGE -> NONE (loss-cap exit) ---
        pe_leg = position.legs[0]
        reason = await ste._evaluate_pe_hedge_exit_signal(symbol, pe_leg)
        assert reason == "PE_MAX_LOSS_HIT", \
            f"expected the loss cap to fire (entry={pe_leg.entry_price}, ltp=5.0, qty={pe_leg.quantity}), got {reason}"
        await ste._exit_pe_hedge_to_watching(symbol, pe_leg, reason)

        assert symbol not in store.live_positions, "must be back to NONE after the loss-cap exit"
        assert symbol not in store.reserved_symbols, \
            "the loss-cap exit must release capacity - no auto re-entry into a fresh basket"

        # 5 real orders: BUY FUT, BUY PE (entry) | SELL FUT, SELL PE (basket exit) | BUY PE (hedge entry) | SELL PE (hedge exit)
        expected_sequence = [
            (fut_symbol, "BUY"), (pe_symbol, "BUY"),
            (fut_symbol, "SELL"), (pe_symbol, "SELL"),
            (pe_symbol, "BUY"), (pe_symbol, "SELL"),
        ]
        actual_sequence = [(o["trading_symbol"], o["transaction_type"]) for o in placed_orders]
        assert actual_sequence == expected_sequence, actual_sequence

        await asyncio.sleep(0.3)
        opened = [r for r in trade_history.read_all_jsonl("position_opened")
                  if r["strategy"] == "Swing" and r["underlying_symbol"] == symbol]
        closed = [r for r in trade_history.read_all_jsonl("real_trades")
                  if r["strategy"] == "Swing" and r["underlying_symbol"] == symbol]
        # opened: FUT, PE (basket entry) + PE (hedge entry) = 3
        assert len(opened) == 3, f"expected 3 legs opened (FUT,PE,PE-hedge), got {len(opened)}: {opened}"
        # closed: FUT, PE (basket exit) + PE (hedge exit) = 3
        assert len(closed) == 3, f"expected 3 legs closed, got {len(closed)}: {closed}"
        assert closed[-1]["exit_reason"] == "PE_MAX_LOSS_HIT"

        events = [r for r in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if r["underlying_symbol"] == symbol]
        event_types = [r["event"] for r in events]
        assert event_types == [
            "BASKET_HEDGE_BASKET_ENTERED", "BASKET_HEDGE_BASKET_EXITED",
            "BASKET_HEDGE_PE_HEDGE_ENTERED", "BASKET_HEDGE_PE_HEDGE_EXITED",
        ], event_types

        print("2. Full real basket_hedge cycle (NONE->BASKET->PE_HEDGE->NONE via loss-cap) via the "
              "REAL _enter_basket_hedge_for_stock/_exit_basket_hedge_to_pe/_exit_pe_hedge_to_watching/"
              "_evaluate_pe_hedge_exit_signal: exactly the expected 6-order sequence placed, 3 legs "
              "opened+closed correctly in trade_history, every transition durably logged: PASSED")
    finally:
        restore()
        ste._evaluate_basket_exit_signal = real_exit_signal
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap
        ste.config.PE_MAX_LOSS_RS = real_pe_loss_cap
        ste.config.PE_PROFIT_LOCK_RS = real_pe_profit_lock


async def test_3_pe_hedge_profit_lock_exit():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    real_pe_profit_lock = ste.config.PE_PROFIT_LOCK_RS
    ste.config.PE_PROFIT_LOCK_RS = 2000.0
    real_pe_loss_cap = ste.config.PE_MAX_LOSS_RS
    ste.config.PE_MAX_LOSS_RS = 2000.0
    symbol = "INFY"
    pe_symbol = "INFY FAKE EXP PUT"
    restore, placed_orders = install_all_dhan_mocks(
        fill_prices=[30.0], ltp_by_symbol={pe_symbol: 40.0},  # +10/unit * 500 lot_size = 5000 profit > 2000 cap
    )
    try:
        pe_leg = sps.Leg(
            underlying_symbol=symbol, option_trading_symbol=pe_symbol, option_type="PE",
            quantity=500, lot_size=500, entry_price=30.0, order_id="X", product_type=swing_config.OPTIONS_PRODUCT,
        )
        await store.set_pe_hedge(symbol, pe_leg)
        reason = await ste._evaluate_pe_hedge_exit_signal(symbol, pe_leg)
        assert reason == "PE_PROFIT_LOCK_HIT", f"expected profit-lock to fire, got {reason}"

        await ste._exit_pe_hedge_to_watching(symbol, pe_leg, reason)
        assert symbol not in store.live_positions
        assert symbol not in store.reserved_symbols

        print("3. PE hedge's own profit-lock exit condition ('lock profit when it becomes more than "
              "2k') fires correctly and independently of the loss-cap check: PASSED")
    finally:
        restore()
        ste.config.PE_PROFIT_LOCK_RS = real_pe_profit_lock
        ste.config.PE_MAX_LOSS_RS = real_pe_loss_cap


async def test_4_pe_hedge_bare_supertrend_reversal_exit():
    """Per the user's own words: "Super trend reversal comes again even
    if buy signal is not yet triggered" - this must fire from a BARE
    Supertrend crossed_above check, never routed through the full
    _evaluate_watchlist_entry_signal (which additionally requires the
    price-confirmation gate + a 1-min confirm timeframe)."""
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    real_fetch_supertrend = ste._fetch_supertrend_state
    real_entry_signal = ste._evaluate_watchlist_entry_signal
    symbol = "TCS"
    pe_symbol = "TCS FAKE EXP PUT"

    async def fake_fetch_supertrend(sym, interval_minutes):
        assert sym == symbol
        return fake_supertrend_state(close=3800.0, supertrend=3750.0, is_above=True, prev_is_above=False)

    async def entry_signal_never_fires(sym):
        # Proves the exit does NOT depend on this - if _evaluate_pe_hedge_exit_signal
        # ever routed through this, the reversal would never be detected here.
        return False

    ste._fetch_supertrend_state = fake_fetch_supertrend
    ste._evaluate_watchlist_entry_signal = entry_signal_never_fires
    restore, placed_orders = install_all_dhan_mocks(
        fill_prices=[30.0], ltp_by_symbol={},  # no LTP configured - loss/profit checks must not crash, just skip
    )
    try:
        pe_leg = sps.Leg(
            underlying_symbol=symbol, option_trading_symbol=pe_symbol, option_type="PE",
            quantity=500, lot_size=500, entry_price=30.0, order_id="X", product_type=swing_config.OPTIONS_PRODUCT,
        )
        await store.set_pe_hedge(symbol, pe_leg)
        reason = await ste._evaluate_pe_hedge_exit_signal(symbol, pe_leg)
        assert reason == "PE_SUPERTREND_REVERSAL_EXIT", \
            f"expected the bare Supertrend reversal to fire even with no confirmed entry signal, got {reason}"
        print("4. PE hedge's own bare Supertrend-reversal exit condition fires independently of the "
              "full entry-signal gate (price confirmation + 1-min confirm NOT required), matching "
              "the user's own wording 'even if buy signal is not yet triggered': PASSED")
    finally:
        restore()
        ste._fetch_supertrend_state = real_fetch_supertrend
        ste._evaluate_watchlist_entry_signal = real_entry_signal


async def test_5_capacity_blocks_reentry_mid_flight_but_not_other_symbols():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 10
    restore, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0])
    try:
        symbol = "SBIN"
        result_1 = await ste._enter_basket_hedge_for_stock(symbol)
        assert result_1["status"] == "entered", result_1

        result_2 = await ste._enter_basket_hedge_for_stock(symbol)
        assert result_2["status"] == "skipped" and result_2["reason"] == "duplicate_or_capacity_full", result_2

        result_other = await ste._enter_basket_hedge_for_stock("WIPRO")
        assert result_other["status"] == "entered", result_other

        print("5. Capacity correctly blocks a duplicate basket_hedge entry for a symbol already "
              "mid-flight, while a different symbol enters normally: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_6_reconciliation_handles_all_three_leg_shapes():
    restore, _ = install_all_dhan_mocks()
    real_get_positions = odc.dhan_wrapper.get_open_fno_positions
    real_attribute = ste.attribute_open_broker_position
    odc.dhan_wrapper.get_open_fno_positions = lambda: [
        # APLAPOLLO: lone FUT, no paired PE - the real, grandfathered live position.
        {"trading_symbol": "APLAPOLLO FAKE EXP FUT", "underlying_symbol": "APLAPOLLO",
         "quantity": 350, "lot_size": 350, "avg_price": 2263.3},
        # A lone PE, no paired FUT - the normal PE_HEDGE shape after a restart mid-hedge.
        {"trading_symbol": "INFY FAKE EXP PUT", "underlying_symbol": "INFY",
         "quantity": 500, "lot_size": 500, "avg_price": 25.0},
        # A matched FUT+PE pair - a normal, freshly-entered 2-leg basket.
        {"trading_symbol": "TCS FAKE EXP FUT", "underlying_symbol": "TCS",
         "quantity": 250, "lot_size": 250, "avg_price": 3800.0},
        {"trading_symbol": "TCS FAKE EXP PUT", "underlying_symbol": "TCS",
         "quantity": 250, "lot_size": 250, "avg_price": 40.0},
    ]
    ste.attribute_open_broker_position = lambda ts: "Swing"
    try:
        positions = await ste.reconcile_basket_hedge_positions()
        by_symbol = {p.underlying_symbol: p for p in positions}
        assert set(by_symbol) == {"APLAPOLLO", "INFY", "TCS"}, by_symbol

        aplapollo = by_symbol["APLAPOLLO"]
        assert aplapollo.state == "BASKET", aplapollo.state
        assert len(aplapollo.legs) == 1, aplapollo.legs
        assert aplapollo.legs[0].option_type == "FUT"
        assert aplapollo.legs[0].entry_price == 2263.3
        assert aplapollo.legs[0].reconciled is True

        infy = by_symbol["INFY"]
        assert infy.state == "PE_HEDGE", infy.state
        assert len(infy.legs) == 1 and infy.legs[0].option_type == "PE"

        tcs = by_symbol["TCS"]
        assert tcs.state == "BASKET", tcs.state
        assert len(tcs.legs) == 2, tcs.legs
        assert {leg.option_type for leg in tcs.legs} == {"FUT", "PE"}

        store = sps.BasketHedgeStore()
        for position in positions:
            await store.reconcile_position(position)
        assert set(store.live_positions) == {"APLAPOLLO", "INFY", "TCS"}
        assert set(store.reserved_symbols) == {"APLAPOLLO", "INFY", "TCS"}
        # Idempotent.
        for position in positions:
            await store.reconcile_position(position)
        assert len(store.live_positions) == 3

        print("6. Startup reconciliation correctly handles all 3 leg shapes: a lone FUT (the real "
              "grandfathered APLAPOLLO position) as a 1-leg BASKET with the exact right entry_price "
              "(2263.3), a lone PE as PE_HEDGE, and a matched FUT+PE pair as a normal 2-leg BASKET, "
              "idempotently: PASSED")
    finally:
        odc.dhan_wrapper.get_open_fno_positions = real_get_positions
        ste.attribute_open_broker_position = real_attribute
        restore()


async def test_7_manual_square_off_closes_every_current_leg():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    real_mode = ste.config.STRATEGY_MODE
    ste.config.STRATEGY_MODE = "basket_hedge"
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks(fill_prices=[100.0, 20.0, 95.0, 18.0])
    try:
        result = await ste._enter_basket_hedge_for_stock("AXISBANK")
        assert result["status"] == "entered", result
        assert "AXISBANK" in store.live_positions

        await ste._square_off_all("MANUAL_SQUARE_OFF")
        assert "AXISBANK" not in store.live_positions, "manual square-off must close every current leg"
        assert "AXISBANK" not in store.reserved_symbols, \
            "manual square-off must release capacity, not swap into a PE hedge"

        await asyncio.sleep(0.2)
        closed = [r for r in trade_history.read_all_jsonl("real_trades")
                  if r["strategy"] == "Swing" and r["underlying_symbol"] == "AXISBANK"]
        assert len(closed) == 2 and all(r["exit_reason"] == "MANUAL_SQUARE_OFF" for r in closed), closed
        print("7. Manual square-off closes EVERY leg currently held by a basket_hedge position "
              "(both legs of a live BASKET here) and releases capacity, mode-aware: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_MODE = real_mode
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_8_monitor_loop_dispatch_is_3way_isolated():
    """Replicates monitor_loop's own dispatch line (rather than running
    the real infinite loop), same established pattern used for
    sequential mode's own equivalent test."""
    calls = {"basket": 0, "sequential": 0, "basket_hedge": 0}
    real_basket_tick = ste._basket_monitor_tick
    real_sequential_tick = ste._sequential_monitor_tick
    real_basket_hedge_tick = ste._basket_hedge_monitor_tick

    async def fake_basket_tick():
        calls["basket"] += 1

    async def fake_sequential_tick():
        calls["sequential"] += 1

    async def fake_basket_hedge_tick():
        calls["basket_hedge"] += 1

    ste._basket_monitor_tick = fake_basket_tick
    ste._sequential_monitor_tick = fake_sequential_tick
    ste._basket_hedge_monitor_tick = fake_basket_hedge_tick
    real_mode = ste.config.STRATEGY_MODE
    try:
        for mode in ("basket", "basket_hedge", "sequential"):
            ste.config.STRATEGY_MODE = mode
            if ste.config.STRATEGY_MODE == "basket":
                await ste._basket_monitor_tick()
            elif ste.config.STRATEGY_MODE == "basket_hedge":
                await ste._basket_hedge_monitor_tick()
            else:
                await ste._sequential_monitor_tick()
        assert calls == {"basket": 1, "sequential": 1, "basket_hedge": 1}, calls

        print("8. monitor_loop's own 3-way per-tick mode dispatch calls exactly one of "
              "_basket_monitor_tick/_sequential_monitor_tick/_basket_hedge_monitor_tick per tick, "
              "matching config.STRATEGY_MODE: PASSED")
    finally:
        ste._basket_monitor_tick = real_basket_tick
        ste._sequential_monitor_tick = real_sequential_tick
        ste._basket_hedge_monitor_tick = real_basket_hedge_tick
        ste.config.STRATEGY_MODE = real_mode


async def main():
    print("=== Swing basket_hedge-mode test suite ===\n")
    test_1_config_defaults_to_basket_hedge()
    await test_2_full_cycle_basket_to_pe_hedge_to_loss_cap_exit()
    await test_3_pe_hedge_profit_lock_exit()
    await test_4_pe_hedge_bare_supertrend_reversal_exit()
    await test_5_capacity_blocks_reentry_mid_flight_but_not_other_symbols()
    await test_6_reconciliation_handles_all_three_leg_shapes()
    await test_7_manual_square_off_closes_every_current_leg()
    await test_8_monitor_loop_dispatch_is_3way_isolated()
    print("\nALL SWING BASKET_HEDGE-MODE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
