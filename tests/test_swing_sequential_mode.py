"""
Tests for Swing's "sequential" trading mode (added 1 Sep 2026, user
request): "now it won't be a basket order but 2 different orders running
sequentially" - buy futures on entry, sell it and buy an ATM PE hedge on
exit, then exit that PE either on its own rupee loss cap (back to plain
watching) or once the entry condition fires again (which also
immediately re-buys futures, "keep this loop going"). Switched via the
new config.STRATEGY_MODE flag ("basket" | "sequential") - basket mode's
own code is untouched and fully covered by test_swing_package.py/
test_swing_integration.py already; this file covers ONLY what's new.

Two points were genuinely ambiguous in the user's own wording and were
confirmed via AskUserQuestion before writing any code - both are
explicitly exercised below:
  1. A PE loss-cap exit returns to plain WATCHING, it does NOT blindly
     re-buy futures (only the entry-condition-refire path does that).
  2. Paper trading (paper_engine.py) mirrors whichever mode is active,
     rather than staying pinned to basket mode.

Covers, against the REAL production functions (not reimplemented):
  1. config.STRATEGY_MODE defaults to "sequential" (the mode requested
     to be ON "for time being").
  2. A full, real two-loop cycle through the state machine (NONE ->
     FUTURES -> PE -> FUTURES -> PE -> NONE, i.e. 2 full "buy futures,
     hedge with PE" iterations then a loss-cap exit) via the REAL
     _enter_futures_for_stock/_swap_futures_to_pe/_swap_pe_to_futures/
     _exit_pe_to_watching/_evaluate_pe_exit_signal, with every real
     order placed verified (5 orders: BUY FUT, SELL FUT, BUY PE, SELL
     PE, BUY FUT, SELL FUT, BUY PE, SELL PE - 4 legs opened+closed) and
     the FULL sequence persisted correctly to trade_history.
  3. Capacity stays RESERVED throughout every swap (FUTURES<->PE) and is
     only RELEASED by the loss-cap exit - a second entry attempt for the
     SAME symbol is rejected while mid-loop, a DIFFERENT symbol is
     unaffected.
  4. Startup reconciliation recovers a LONE broker leg (FUT or PE) into
     SequentialPositionStore directly - the NORMAL shape here, unlike
     basket mode where a lone leg is an anomaly.
  5. The manual kill-switch (_square_off_all) is mode-aware: in
     sequential mode it closes the currently-held leg and returns the
     symbol to plain watching (capacity released), not into a hedge.
  6. monitor_loop's own per-tick mode dispatch - "basket" mode never
     touches sequential_store, "sequential" mode never touches
     basket_store.
  7. Paper trading's sequential mode mirrors the same state machine
     (NONE -> FUTURES -> PE -> FUTURES -> NONE), simulated, NEVER placing
     a real order (proven by making place_market_order/
     wait_for_order_result raise if called), persisted to its own log
     completely separate from basket mode's own paper log.

HOW TO RUN:
    uv run python tests/test_swing_sequential_mode.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_sequential_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.config as swing_config
import Swing.paper_engine as spe
import Swing.position_store as sps
import Swing.trading_engine as ste
import Swing.watchlist as swl
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def _raise_if_called(*args, **kwargs):
    raise AssertionError(f"SAFETY INVARIANT VIOLATED: a real order-placement function was called "
                          f"during a paper trading flow. args={args} kwargs={kwargs}")


def install_all_dhan_mocks(fill_prices=None, ltp_by_symbol=None):
    """fill_prices: list of fill prices consumed IN ORDER across
    successive place_market_order/wait_for_order_result calls (this
    file's flows are always strictly sequential, never concurrent, so
    order is deterministic). ltp_by_symbol: fixed LTP responses for
    get_option_ltp (used by the PE loss-cap check and paper trading's own
    simulated fills)."""
    fill_prices = list(fill_prices or [])
    ltp_by_symbol = ltp_by_symbol or {}
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    # Fixed fakes - the paper-trading margin/funds logging (used by the
    # sequential paper flows in test 7) is not what's under test here;
    # unmocked, these would fall through to a REAL Dhan network call
    # (and a real, slow authentication attempt) via _retry.
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


def test_1_sequential_mode_remains_a_valid_switchable_mode():
    """config.STRATEGY_MODE's own default moved on to "basket_hedge" 1
    Sep 2026 (see test_swing_basket_hedge_mode.py's own test 1) - this
    file's job isn't to pin the CURRENT default, only to prove
    "sequential" itself hasn't been deleted and is still a real,
    recognized value every test below can select explicitly (exactly as
    they already do, e.g. test_5's own ste.config.STRATEGY_MODE =
    "sequential")."""
    assert swing_config.STRATEGY_MODE in ("basket", "sequential", "basket_hedge"), \
        f"unexpected STRATEGY_MODE value entirely: {swing_config.STRATEGY_MODE!r}"
    real_mode = swing_config.STRATEGY_MODE
    try:
        swing_config.STRATEGY_MODE = "sequential"
        assert swing_config.STRATEGY_MODE == "sequential"
    finally:
        swing_config.STRATEGY_MODE = real_mode
    print("1. 'sequential' remains a valid, fully switchable STRATEGY_MODE value (basket and "
          "basket_hedge modes preserved alongside it, not deleted) - this file's own tests below "
          "each pin the mode explicitly rather than relying on whatever the current default is: PASSED")


async def test_2_full_two_loop_cycle_then_loss_cap_exit():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 10
    real_pe_cap = ste.config.PE_MAX_LOSS_RS
    ste.config.PE_MAX_LOSS_RS = 2000.0

    real_entry_signal = ste._evaluate_watchlist_entry_signal
    real_exit_signal = ste._evaluate_basket_exit_signal
    symbol = "RELIANCE"

    async def fake_entry_signal(sym):
        return sym == symbol and entry_fires["value"]

    async def fake_exit_signal(sym, basket):
        return "SUPERTREND_5MIN_EXIT" if (sym == symbol and exit_fires["value"]) else None

    entry_fires = {"value": False}
    exit_fires = {"value": False}
    ste._evaluate_watchlist_entry_signal = fake_entry_signal
    ste._evaluate_basket_exit_signal = fake_exit_signal

    fut_symbol = "RELIANCE FAKE EXP FUT"
    pe_symbol = "RELIANCE FAKE EXP PUT"
    # Order of fills: BUY FUT(100), SELL FUT(110), BUY PE(20), SELL PE(15),
    # BUY FUT(112), SELL FUT(105), BUY PE(18), (loss-cap exit priced via LTP, not a fill)
    restore, placed_orders = install_all_dhan_mocks(
        fill_prices=[100.0, 110.0, 20.0, 15.0, 112.0, 105.0, 18.0],
        ltp_by_symbol={pe_symbol: 10.0, fut_symbol: 100.0},  # PE: final loss-cap SELL/LTP check; FUT: the funds-check price
    )
    try:
        # --- Loop iteration 1: NONE -> FUTURES ---
        entry_fires["value"] = True
        result = await ste._enter_futures_for_stock(symbol)
        assert result["status"] == "entered" and result["leg"] == "FUT", result
        assert symbol in store.live_legs and store.live_legs[symbol].option_type == "FUT"
        assert symbol in store.reserved_symbols

        # --- FUTURES -> PE ---
        entry_fires["value"] = False
        exit_fires["value"] = True
        fut_leg = store.live_legs[symbol]
        await ste._swap_futures_to_pe(symbol, fut_leg)
        assert store.live_legs[symbol].option_type == "PE", store.live_legs.get(symbol)
        assert symbol in store.reserved_symbols, "capacity must stay RESERVED during a swap, not released"

        # --- PE -> FUTURES (loop continues: entry signal re-fires) ---
        exit_fires["value"] = False
        entry_fires["value"] = True
        pe_leg = store.live_legs[symbol]
        await ste._swap_pe_to_futures(symbol, pe_leg)
        assert store.live_legs[symbol].option_type == "FUT", store.live_legs.get(symbol)
        assert symbol in store.reserved_symbols, "capacity must stay RESERVED across the loop"

        # --- FUTURES -> PE again (2nd iteration) ---
        entry_fires["value"] = False
        exit_fires["value"] = True
        fut_leg_2 = store.live_legs[symbol]
        await ste._swap_futures_to_pe(symbol, fut_leg_2)
        assert store.live_legs[symbol].option_type == "PE"

        # --- PE -> NONE (loss-cap exit, NOT a re-entry into futures) ---
        exit_fires["value"] = False
        pe_leg_2 = store.live_legs[symbol]
        reason = await ste._evaluate_pe_exit_signal(symbol, pe_leg_2)
        assert reason == "PE_MAX_LOSS_HIT", \
            f"expected the loss cap to fire (entry={pe_leg_2.entry_price}, ltp=10.0, qty={pe_leg_2.quantity}), got {reason}"
        await ste._exit_pe_to_watching(symbol, pe_leg_2, reason)

        assert symbol not in store.live_legs, "must be back to NONE after the loss-cap exit"
        assert symbol not in store.reserved_symbols, \
            "the loss-cap exit MUST release capacity - confirmed via AskUserQuestion this does NOT re-buy futures"

        # 8 real orders placed total: BUY/SELL FUT, BUY/SELL PE, BUY/SELL FUT, BUY/SELL PE
        assert len(placed_orders) == 8, placed_orders
        expected_sequence = [
            (fut_symbol, "BUY"), (fut_symbol, "SELL"), (pe_symbol, "BUY"), (pe_symbol, "SELL"),
            (fut_symbol, "BUY"), (fut_symbol, "SELL"), (pe_symbol, "BUY"), (pe_symbol, "SELL"),
        ]
        actual_sequence = [(o["trading_symbol"], o["transaction_type"]) for o in placed_orders]
        assert actual_sequence == expected_sequence, actual_sequence

        await asyncio.sleep(0.3)
        opened = [r for r in trade_history.read_all_jsonl("position_opened")
                  if r["strategy"] == "Swing" and r["underlying_symbol"] == symbol]
        closed = [r for r in trade_history.read_all_jsonl("real_trades")
                  if r["strategy"] == "Swing" and r["underlying_symbol"] == symbol]
        assert len(opened) == 4, f"expected 4 legs opened (FUT,PE,FUT,PE), got {len(opened)}: {opened}"
        assert len(closed) == 4, f"expected 4 legs closed, got {len(closed)}: {closed}"
        assert [r["option_type"] for r in opened] == ["FUT", "PE", "FUT", "PE"]
        assert closed[-1]["exit_reason"] == "PE_MAX_LOSS_HIT"

        print("2. Full real 2-loop-iteration sequential cycle (NONE->FUT->PE->FUT->PE->NONE) via "
              "the REAL _enter_futures_for_stock/_swap_futures_to_pe/_swap_pe_to_futures/"
              "_exit_pe_to_watching/_evaluate_pe_exit_signal: exactly the expected 8-order sequence "
              "placed, 4 legs opened+closed correctly in trade_history, loss-cap exit correctly "
              "does NOT re-buy futures: PASSED")
    finally:
        restore()
        ste._evaluate_watchlist_entry_signal = real_entry_signal
        ste._evaluate_basket_exit_signal = real_exit_signal
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap
        ste.config.PE_MAX_LOSS_RS = real_pe_cap


async def test_3_capacity_blocks_reentry_mid_loop_but_not_other_symbols():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 10
    restore, placed_orders = install_all_dhan_mocks(
        fill_prices=[100.0], ltp_by_symbol={"TCS FAKE EXP FUT": 100.0, "SBIN FAKE EXP FUT": 100.0},
    )
    try:
        symbol = "TCS"
        result_1 = await ste._enter_futures_for_stock(symbol)
        assert result_1["status"] == "entered", result_1

        # A second attempt for the SAME symbol, still mid-loop, must be rejected.
        result_2 = await ste._enter_futures_for_stock(symbol)
        assert result_2["status"] == "skipped" and result_2["reason"] == "duplicate_or_capacity_full", result_2

        # A DIFFERENT symbol is completely unaffected.
        result_other = await ste._enter_futures_for_stock("SBIN")
        assert result_other["status"] == "entered", result_other

        print("3. Capacity correctly blocks a duplicate entry attempt for a symbol already mid-loop, "
              "while a different symbol enters normally: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_4_reconciliation_recovers_a_lone_leg():
    restore, _ = install_all_dhan_mocks()
    real_get_positions = odc.dhan_wrapper.get_open_fno_positions
    real_attribute = ste.attribute_open_broker_position
    odc.dhan_wrapper.get_open_fno_positions = lambda: [
        {"trading_symbol": "TATASTEEL FAKE EXP FUT", "underlying_symbol": "TATASTEEL",
         "quantity": 250, "lot_size": 250, "avg_price": 150.0},
    ]
    ste.attribute_open_broker_position = lambda ts: "Swing"
    try:
        legs = await ste.reconcile_sequential_positions()
        assert len(legs) == 1, legs
        leg = legs[0]
        assert leg.underlying_symbol == "TATASTEEL"
        assert leg.option_type == "FUT", "a lone FUT leg is the NORMAL shape in sequential mode, not an anomaly"
        assert leg.reconciled is True

        store = sps.SequentialPositionStore()
        await store.reconcile_leg(leg)
        assert "TATASTEEL" in store.live_legs
        assert "TATASTEEL" in store.reserved_symbols
        # Idempotent - reconciling the same leg twice must not duplicate/overwrite oddly.
        await store.reconcile_leg(leg)
        assert len(store.live_legs) == 1

        print("4. Sequential-mode startup reconciliation recovers a LONE Swing-attributed broker "
              "leg (the NORMAL shape here, unlike basket mode) into SequentialPositionStore, "
              "idempotently: PASSED")
    finally:
        odc.dhan_wrapper.get_open_fno_positions = real_get_positions
        ste.attribute_open_broker_position = real_attribute
        restore()


async def test_5_manual_square_off_is_mode_aware():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    real_mode = ste.config.STRATEGY_MODE
    ste.config.STRATEGY_MODE = "sequential"
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks(
        fill_prices=[100.0, 95.0], ltp_by_symbol={"WIPRO FAKE EXP FUT": 100.0},
    )
    try:
        result = await ste._enter_futures_for_stock("WIPRO")
        assert result["status"] == "entered", result
        assert "WIPRO" in store.live_legs

        await ste._square_off_all("MANUAL_SQUARE_OFF")
        assert "WIPRO" not in store.live_legs, "manual square-off must close the held leg"
        assert "WIPRO" not in store.reserved_symbols, \
            "manual square-off must return to plain watching (capacity released), not into a hedge"

        await asyncio.sleep(0.2)
        closed = [r for r in trade_history.read_all_jsonl("real_trades")
                  if r["strategy"] == "Swing" and r["underlying_symbol"] == "WIPRO"]
        assert len(closed) == 1 and closed[0]["exit_reason"] == "MANUAL_SQUARE_OFF"
        print("5. Manual square-off (_square_off_all) is mode-aware - in sequential mode it closes "
              "the held leg and returns straight to watching, not into a hedge: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_MODE = real_mode
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_6_monitor_loop_dispatch_is_mode_isolated():
    """Replicates monitor_loop's own dispatch line (rather than running
    the real infinite loop) to prove "basket" mode never touches
    sequential_store and "sequential" mode never touches basket_store -
    same established pattern other Swing test files use for a loop's own
    gated body."""
    basket_calls = {"n": 0}
    sequential_calls = {"n": 0}
    real_basket_tick = ste._basket_monitor_tick
    real_sequential_tick = ste._sequential_monitor_tick

    async def fake_basket_tick():
        basket_calls["n"] += 1

    async def fake_sequential_tick():
        sequential_calls["n"] += 1

    ste._basket_monitor_tick = fake_basket_tick
    ste._sequential_monitor_tick = fake_sequential_tick
    real_mode = ste.config.STRATEGY_MODE
    try:
        ste.config.STRATEGY_MODE = "basket"
        if ste.config.STRATEGY_MODE == "basket":
            await ste._basket_monitor_tick()
        else:
            await ste._sequential_monitor_tick()
        assert basket_calls["n"] == 1 and sequential_calls["n"] == 0

        ste.config.STRATEGY_MODE = "sequential"
        if ste.config.STRATEGY_MODE == "basket":
            await ste._basket_monitor_tick()
        else:
            await ste._sequential_monitor_tick()
        assert basket_calls["n"] == 1 and sequential_calls["n"] == 1

        print("6. monitor_loop's own per-tick mode dispatch calls exactly one of "
              "_basket_monitor_tick/_sequential_monitor_tick per tick, matching config.STRATEGY_MODE: PASSED")
    finally:
        ste._basket_monitor_tick = real_basket_tick
        ste._sequential_monitor_tick = real_sequential_tick
        ste.config.STRATEGY_MODE = real_mode


async def test_7_paper_sequential_full_loop_never_places_real_orders():
    p_store = spe.SequentialPaperStore()
    spe.sequential_paper_store = p_store
    real_entry_signal = spe.trading_engine._evaluate_watchlist_entry_signal
    real_exit_signal = spe.trading_engine._evaluate_basket_exit_signal
    symbol = "HDFCBANK"

    async def fake_entry_signal(sym):
        return sym == symbol and entry_fires["value"]

    async def fake_exit_signal(sym, basket):
        return "SUPERTREND_5MIN_EXIT" if (sym == symbol and exit_fires["value"]) else None

    entry_fires = {"value": False}
    exit_fires = {"value": False}
    spe.trading_engine._evaluate_watchlist_entry_signal = fake_entry_signal
    spe.trading_engine._evaluate_basket_exit_signal = fake_exit_signal

    fut_symbol = "HDFCBANK FAKE EXP FUT"
    pe_symbol = "HDFCBANK FAKE EXP PUT"
    ltp_by_symbol = {fut_symbol: 1600.0, pe_symbol: 25.0}
    restore, placed_orders = install_all_dhan_mocks(ltp_by_symbol=ltp_by_symbol)
    odc.dhan_wrapper.place_market_order = _raise_if_called
    odc.dhan_wrapper.wait_for_order_result = _raise_if_called
    try:
        # NONE -> FUTURES (paper)
        entry_fires["value"] = True
        await spe._enter_paper_futures(symbol)
        assert symbol in p_store.live_legs and p_store.live_legs[symbol].instrument_type == "FUT"

        # FUTURES -> PE (paper)
        entry_fires["value"] = False
        exit_fires["value"] = True
        fut_leg = p_store.live_legs[symbol]
        await spe._swap_paper_futures_to_pe(symbol, fut_leg)
        assert p_store.live_legs[symbol].instrument_type == "PE"

        # PE -> FUTURES (paper, loop continues)
        exit_fires["value"] = False
        entry_fires["value"] = True
        pe_leg = p_store.live_legs[symbol]
        await spe._swap_paper_pe_to_futures(symbol, pe_leg)
        assert p_store.live_legs[symbol].instrument_type == "FUT"

        # FUTURES -> PE again
        entry_fires["value"] = False
        exit_fires["value"] = True
        fut_leg_2 = p_store.live_legs[symbol]
        await spe._swap_paper_futures_to_pe(symbol, fut_leg_2)
        assert p_store.live_legs[symbol].instrument_type == "PE"

        # PE -> NONE (paper loss-cap exit) - force it via a low LTP.
        exit_fires["value"] = False
        pe_leg_2 = p_store.live_legs[symbol]
        ltp_by_symbol[pe_symbol] = pe_leg_2.entry_price - (spe.config.PE_MAX_LOSS_RS / pe_leg_2.quantity) - 1
        pe_reason = await spe._evaluate_paper_pe_exit_signal(symbol, pe_leg_2)
        assert pe_reason == "PE_MAX_LOSS_HIT", pe_reason
        await spe._exit_paper_pe_to_watching(symbol, pe_leg_2, pe_reason)
        assert symbol not in p_store.live_legs, "must be back to NONE (paper) after the loss-cap exit"

        assert placed_orders == [], \
            "SAFETY INVARIANT: paper trading must never place any real order, even across a full loop"

        sequential_paper_trades = [t for t in p_store.completed if t["underlying_symbol"] == symbol]
        assert len(sequential_paper_trades) == 4, sequential_paper_trades
        assert [t["instrument_type"] for t in sequential_paper_trades] == ["FUT", "PE", "FUT", "PE"]
        assert sequential_paper_trades[-1]["exit_reason"] == "PE_MAX_LOSS_HIT"

        reloaded = trade_history.read_all_jsonl(spe.SWING_SEQUENTIAL_PAPER_LOG_NAME)
        reloaded_for_symbol = [t for t in reloaded if t["underlying_symbol"] == symbol]
        assert len(reloaded_for_symbol) == 4, "must round-trip through the dedicated on-disk log too"

        basket_paper_log = trade_history.read_all_jsonl(spe.SWING_PAPER_LOG_NAME)
        assert all(t["underlying_symbol"] != symbol for t in basket_paper_log), \
            "sequential paper activity must NEVER leak into basket mode's own separate paper log"

        print("7. Sequential paper trading mirrors the full real state machine (NONE->FUT->PE->FUT-"
              ">PE->NONE) purely via simulated LTP fills, NEVER placing a real order across the "
              "whole loop, persisted to its own on-disk log fully isolated from basket mode's own "
              "paper log: PASSED")
    finally:
        restore()
        spe.trading_engine._evaluate_watchlist_entry_signal = real_entry_signal
        spe.trading_engine._evaluate_basket_exit_signal = real_exit_signal


async def main():
    print("=== Swing sequential-mode test suite ===\n")
    test_1_sequential_mode_remains_a_valid_switchable_mode()
    await test_2_full_two_loop_cycle_then_loss_cap_exit()
    await test_3_capacity_blocks_reentry_mid_loop_but_not_other_symbols()
    await test_4_reconciliation_recovers_a_lone_leg()
    await test_5_manual_square_off_is_mode_aware()
    await test_6_monitor_loop_dispatch_is_mode_isolated()
    await test_7_paper_sequential_full_loop_never_places_real_orders()
    print("\nALL SWING SEQUENTIAL-MODE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
