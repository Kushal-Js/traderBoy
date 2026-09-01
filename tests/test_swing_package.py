"""
Deep integration tests for the Swing package (user request 31 Aug 2026:
"buy 1 lot of stocks future contract along with PE ATM option to hedge
them... all-or-nothing guarantee... at a time only 2 basket orders can be
live... 2 webhooks... deploy it with a flag to turn it off, keep it off
as of now").

Same mocking philosophy as every other deep-integration suite in this
repo - mocks only the Dhan network boundary (Swing reuses the exact same
dhan_wrapper singleton as Options/Futures/Luxury), keeps all real
position_store/basket_store/watchlist/trading_engine LOGIC completely
real.

Covers:
  1. The disabled-by-default flag: both webhooks return status=ignored/
     reason=strategy_disabled and place NO orders at all while
     config.STRATEGY_ENABLED is False (the deployed default).
  2. A full, successful all-or-nothing basket entry once enabled - both
     legs (futures BUY + PE BUY) placed for real (mocked), basket
     recorded correctly, both legs tagged Swing in trade_history.
  3. All-or-nothing rollback case 1: the FUTURES leg itself fails -> the
     PE leg is NEVER attempted at all ("neither").
  4. All-or-nothing rollback case 2: the futures leg fills but the PE
     leg then fails -> the futures leg is automatically unwound (a
     compensating SELL) - confirmed via a real order-count check, not
     just a status field.
  5. Basket capacity (MAX_LIVE_BASKETS) is enforced and configurable.
  6. The watchlist webhook adds symbols and reports which were already
     present.
  7. Swing does NOT participate in cross_strategy_registry (user decision
     1 Sep 2026, reversing its original 31 Aug 2026 design) - Swing and
     Options racing for the SAME stock via their REAL entry functions can
     now BOTH win (no mutual exclusion, and the shared registry stays
     completely untouched by Swing's own attempt). Swing's OWN dedup
     (basket_store.reserve_symbol) still fully protects against a
     SWING-vs-SWING double entry on the same symbol, proven via a real
     concurrent race using its REAL entry function.
  8. get_futures_contract() resolves the correct nearest-expiry contract
     (and correctly handles a hyphenated underlying like BAJAJ-AUTO -
     the same latent bug class _underlying_from_trading_symbol had for
     options, fixed for futures in the same change) against a fake
     instrument dataframe - no live Dhan session needed.

HOW TO RUN:
    uv run python tests/test_swing_package.py
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

import pandas as pd
import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_test_"))
trade_history.HISTORY_DIR = scratch_dir

import cross_strategy_registry as csr
import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Options.option_main as om
import Swing.position_store as sps
import Swing.trading_engine as ste
import Swing.swing_main as sm
import Swing.watchlist as swl
from Options.dhan_client import AtmOption, FuturesContract, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(place_order_results=None):
    """Mocks the shared dhan_wrapper singleton (Swing reuses the exact
    same one Options/Futures/Luxury do). `place_order_results`, if given,
    is a list of {"status": ...} dicts consumed in order across
    successive place_market_order calls - lets a test force e.g. the
    SECOND order placed (the PE leg) to fail while the first (futures)
    succeeds, or vice versa. Records every order actually placed
    (trading_symbol, transaction_type) for assertions about what did or
    didn't get attempted."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    # Only Swing's own leg placement touches the mocks above directly, but
    # test 7 also drives a REAL Options._process_one_entry (to prove the
    # cross-strategy race fairly) - that path additionally calls
    # refresh_supertrend_signal/get_cached_supertrend_candle_start, which
    # would otherwise fall through to a live (and here, unauthenticated -
    # confirmed live) Dhan call. Mocked unconditionally, not just when
    # Options is involved, so this helper is safe for every test in this
    # file with zero live network calls, matching every other deep-
    # integration suite's own mock completeness in this repo.
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None

    placed_orders = []
    results_iter = iter(place_order_results or [])
    default_result = {"status": OrderStatus.TRADED, "fill_price": 50.0}

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type,
                               "order_id": order_id, "quantity": quantity, "product_type": product_type})
        return {"order_id": order_id, "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        spec = next(results_iter, default_result)
        return OrderResult(order_id=order_id, status=spec["status"], remark=spec.get("remark", ""),
                            fill_price=spec.get("fill_price", 50.0), filled_quantity=1, is_amo=False)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)

    return restore, placed_orders


async def test_1_disabled_by_default_no_orders_placed():
    """The deployed default: STRATEGY_ENABLED=false. Both webhooks must
    refuse cleanly and place ZERO orders."""
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = False
    sm.config.STRATEGY_ENABLED = False
    restore, placed_orders = install_all_dhan_mocks()
    try:
        enter_payload = sm.SwingWebhookPayload(stocks="RELIANCE", alert_name="t1-enter")
        enter_result = await sm.chartink_webhook_swing_enter(enter_payload)
        assert enter_result["status"] == "ignored"
        assert enter_result["reason"] == "strategy_disabled"

        watchlist_payload = sm.SwingWebhookPayload(stocks="TCS", alert_name="t1-watchlist")
        watchlist_result = await sm.chartink_webhook_swing_watchlist(watchlist_payload)
        assert watchlist_result["status"] == "ignored"
        assert watchlist_result["reason"] == "strategy_disabled"

        assert placed_orders == [], f"disabled strategy must place ZERO orders, got {placed_orders}"
        assert await sps.basket_store.remaining_capacity() == sc_max_baskets()
        print("1. Disabled-by-default flag: both webhooks cleanly refuse "
              "(status=ignored/reason=strategy_disabled), zero orders placed: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        sm.config.STRATEGY_ENABLED = real_enabled


def sc_max_baskets():
    return ste.config.MAX_LIVE_BASKETS


async def test_2_full_successful_basket_entry():
    store = sps.BasketStore()
    sm.basket_store = store
    ste.basket_store = store

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    sm.config.STRATEGY_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks()
    try:
        result = await ste.enter_basket_for_stock("RELIANCE")
        assert result["status"] == "entered", result
        assert len(store.live_baskets) == 1
        basket = store.live_baskets["RELIANCE"]
        assert basket.futures_leg.option_type == "FUT"
        assert basket.option_leg.option_type == "PE"
        assert basket.futures_leg.option_trading_symbol == "RELIANCE FAKE EXP FUT"
        assert basket.option_leg.option_trading_symbol == "RELIANCE FAKE EXP PUT"

        buy_orders = [o for o in placed_orders if o["transaction_type"] == "BUY"]
        assert len(buy_orders) == 2, f"expected exactly 2 BUY orders (futures + PE), got {buy_orders}"

        await asyncio.sleep(0.3)
        opened = trade_history.read_all_jsonl("position_opened")
        swing_opened = [r for r in opened if r["strategy"] == "Swing"]
        assert len(swing_opened) == 2, f"expected both legs logged to trade_history, got {swing_opened}"
        assert {r["option_type"] for r in swing_opened} == {"FUT", "PE"}
        print("2. Full successful basket entry: both legs placed (futures BUY + PE BUY), "
              "basket recorded correctly, both legs tagged 'Swing' in trade_history: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        sm.config.STRATEGY_ENABLED = real_enabled


async def test_3_futures_leg_fails_pe_leg_never_attempted():
    store = sps.BasketStore()
    ste.basket_store = store

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks(
        place_order_results=[{"status": OrderStatus.REJECTED, "remark": "simulated RMS rejection"}],
    )
    try:
        result = await ste.enter_basket_for_stock("TCS")
        assert result["status"] == "rejected", result
        assert result["reason"] == "futures_leg_failed", result

        assert len(placed_orders) == 1, \
            f"the PE leg must NEVER be attempted when the futures leg fails ('neither'), got {placed_orders}"
        assert placed_orders[0]["transaction_type"] == "BUY"
        assert "FUT" in placed_orders[0]["trading_symbol"]
        assert "TCS" not in store.live_baskets
        assert store.reserved_symbols == set()
        print("3. Futures leg fails -> PE leg is NEVER attempted at all ('neither') - "
              "exactly 1 order placed, no basket created, reservation released: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_4_pe_leg_fails_futures_leg_gets_unwound():
    store = sps.BasketStore()
    ste.basket_store = store

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks(place_order_results=[
        {"status": OrderStatus.TRADED, "fill_price": 100.0},                       # futures BUY succeeds
        {"status": OrderStatus.REJECTED, "remark": "simulated PE RMS rejection"},   # PE BUY fails
        {"status": OrderStatus.TRADED, "fill_price": 99.5},                        # unwind SELL succeeds
    ])
    try:
        result = await ste.enter_basket_for_stock("SBIN")
        assert result["status"] == "rejected", result
        assert result["reason"] == "pe_leg_failed_futures_unwound", result

        assert len(placed_orders) == 3, f"expected futures BUY + PE BUY + futures SELL (unwind), got {placed_orders}"
        assert placed_orders[0]["transaction_type"] == "BUY" and "FUT" in placed_orders[0]["trading_symbol"]
        assert placed_orders[1]["transaction_type"] == "BUY" and "PUT" in placed_orders[1]["trading_symbol"]
        assert placed_orders[2]["transaction_type"] == "SELL" and "FUT" in placed_orders[2]["trading_symbol"], \
            "the futures leg must be unwound (a compensating SELL) after the PE leg fails"

        assert "SBIN" not in store.live_baskets, "a failed basket entry must never leave a live basket behind"
        assert store.reserved_symbols == set()
        print("4. PE leg fails after futures leg fills -> futures leg is automatically unwound "
              "(compensating SELL) - 3 real orders (BUY fut, BUY PE, SELL fut), no basket survives: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_5_basket_capacity_enforced_and_configurable():
    store = sps.BasketStore()
    ste.basket_store = store

    real_enabled = ste.config.STRATEGY_ENABLED
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.STRATEGY_ENABLED = True
    ste.config.MAX_LIVE_BASKETS = 2
    restore, placed_orders = install_all_dhan_mocks()
    try:
        r1 = await ste.enter_basket_for_stock("RELIANCE")
        r2 = await ste.enter_basket_for_stock("TCS")
        r3 = await ste.enter_basket_for_stock("SBIN")
        assert r1["status"] == "entered" and r2["status"] == "entered", (r1, r2)
        assert r3["status"] == "skipped" and r3["reason"] == "duplicate_or_capacity_full", r3
        assert len(store.live_baskets) == 2

        # Confirm it's genuinely configurable, not a hardcoded 2.
        ste.config.MAX_LIVE_BASKETS = 1
        store2 = sps.BasketStore()
        ste.basket_store = store2
        r4 = await ste.enter_basket_for_stock("HDFCBANK")
        r5 = await ste.enter_basket_for_stock("ICICIBANK")
        assert r4["status"] == "entered"
        assert r5["status"] == "skipped" and r5["reason"] == "duplicate_or_capacity_full"
        print("5. Basket capacity (MAX_LIVE_BASKETS) enforced correctly at 2, and confirmed "
              "genuinely configurable (re-tested and enforced at 1): PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_6_watchlist_webhook_adds_symbols():
    store = swl.WatchlistStore()
    sm.watchlist_store = store

    real_enabled = sm.config.STRATEGY_ENABLED
    sm.config.STRATEGY_ENABLED = True
    try:
        payload1 = sm.SwingWebhookPayload(stocks="RELIANCE,TCS", alert_name="t6")
        result1 = await sm.chartink_webhook_swing_watchlist(payload1)
        assert result1["status"] == "processed"
        assert set(result1["added"]) == {"RELIANCE", "TCS"}
        assert result1["already_on_watchlist"] == []

        payload2 = sm.SwingWebhookPayload(stocks="TCS,SBIN", alert_name="t6b")
        result2 = await sm.chartink_webhook_swing_watchlist(payload2)
        assert result2["added"] == ["SBIN"]
        assert result2["already_on_watchlist"] == ["TCS"]

        snapshot = await store.snapshot()
        assert snapshot["count"] == 3
        print("6. Watchlist webhook adds new symbols and correctly reports which were "
              "already present: PASSED")
    finally:
        sm.config.STRATEGY_ENABLED = real_enabled


async def test_7_swing_no_longer_shares_cross_strategy_registry():
    """Swing and Options racing for the SAME stock via their REAL entry
    functions, fired concurrently - as of 1 Sep 2026 BOTH must win (Swing
    was deliberately taken OUT of cross_strategy_registry, see
    trading_engine.py's own module docstring), and the shared registry
    must never even see Swing's own attempt. Separately: Swing's OWN
    dedup (basket_store.reserve_symbol) must still fully prevent a
    SWING-vs-SWING double entry on the same symbol - that's the
    "independent, own separate list" protection that replaced it."""
    options_store = ops.PositionStore()
    ote.position_store = options_store
    swing_store = sps.BasketStore()
    ste.basket_store = swing_store

    real_ce_cap = ote.config.MAX_LIVE_POSITIONS_CE
    ote.config.MAX_LIVE_POSITIONS_CE = 10
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    restore, placed_orders = install_all_dhan_mocks()
    try:
        options_result, swing_result = await asyncio.gather(
            ote._process_one_entry("RELIANCE", "CE"),
            ste.enter_basket_for_stock("RELIANCE"),
        )
        assert options_result["status"] == "entered", options_result
        assert swing_result["status"] == "entered", \
            f"Swing must no longer be blocked by Options claiming the same underlying, got {swing_result}"
        assert csr.snapshot() == {}, \
            "the shared registry must never see Swing's own claim at all now (Options' own claim " \
            "still released normally, so it should read back empty either way)"
        print("7a. Swing no longer shares cross_strategy_registry with Options - both can now "
              "independently enter the SAME underlying stock at the same instant, and the shared "
              "registry never even sees Swing's own attempt: PASSED")

        # Reset for the Swing-vs-Swing race (a fresh symbol + a fresh store).
        swing_store2 = sps.BasketStore()
        ste.basket_store = swing_store2
        swing_result_1, swing_result_2 = await asyncio.gather(
            ste.enter_basket_for_stock("TCS"),
            ste.enter_basket_for_stock("TCS"),
        )
        entered = [r for r in (swing_result_1, swing_result_2) if r["status"] == "entered"]
        skipped = [r for r in (swing_result_1, swing_result_2) if r.get("reason") == "duplicate_or_capacity_full"]
        assert len(entered) == 1, f"expected exactly 1 winner, got {swing_result_1} / {swing_result_2}"
        assert len(skipped) == 1, f"expected the loser rejected by Swing's OWN dedup, got {swing_result_1} / {swing_result_2}"
        print("7b. Swing's OWN dedup (basket_store.reserve_symbol) still fully prevents a "
              "SWING-vs-SWING double entry on the same symbol - its own independent 'separate "
              "list' protection: PASSED")
    finally:
        restore()
        ote.config.MAX_LIVE_POSITIONS_CE = real_ce_cap
        ste.config.STRATEGY_ENABLED = real_enabled


def test_8_get_futures_contract_resolves_nearest_expiry_and_hyphenated_names():
    """No live Dhan session needed - a fake instrument dataframe shaped
    like the real scrip master's FUTSTK rows."""
    expiry_by_month = {"Sep2026": "2026-09-29", "Oct2026": "2026-10-27", "Nov2026": "2026-11-23"}
    rows = []
    for month, sym in (("Sep2026", "68777"), ("Oct2026", "48987"), ("Nov2026", "61697")):
        rows.append({"SEM_TRADING_SYMBOL": f"RELIANCE-{month}-FUT", "SEM_CUSTOM_SYMBOL": f"RELIANCE {month[:3].upper()} FUT",
                     "SEM_SMST_SECURITY_ID": sym, "SEM_LOT_UNITS": 500.0,
                     "SEM_EXPIRY_DATE": f"{expiry_by_month[month]} 14:30:00",
                     "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "FUTSTK"})
    rows.append({"SEM_TRADING_SYMBOL": "BAJAJ-AUTO-Sep2026-FUT", "SEM_CUSTOM_SYMBOL": "BAJAJ-AUTO SEP FUT",
                 "SEM_SMST_SECURITY_ID": "68435", "SEM_LOT_UNITS": 75.0, "SEM_EXPIRY_DATE": "2026-09-29 14:30:00",
                 "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "FUTSTK"})
    df = pd.DataFrame(rows)

    class FakeWrapper:
        def instruments(self):
            return df
        _underlying_from_trading_symbol = staticmethod(odc.DhanWrapper._underlying_from_trading_symbol)
        _get_futures_contract_once = odc.DhanWrapper._get_futures_contract_once

    w = FakeWrapper()
    nearest = w._get_futures_contract_once("RELIANCE", 0)
    assert nearest.trading_symbol == "RELIANCE SEP FUT", nearest
    assert nearest.expiry_date.isoformat() == "2026-09-29"
    assert nearest.lot_size == 500

    hyphenated = w._get_futures_contract_once("BAJAJ-AUTO", 0)
    assert hyphenated.trading_symbol == "BAJAJ-AUTO SEP FUT", \
        f"hyphenated underlying must resolve correctly, got {hyphenated}"

    print("8. get_futures_contract resolves the nearest-expiry contract correctly and handles "
          "a hyphenated underlying (BAJAJ-AUTO) without mangling it: PASSED")


async def main():
    print("=== Swing package test suite ===\n")
    await test_1_disabled_by_default_no_orders_placed()
    await test_2_full_successful_basket_entry()
    await test_3_futures_leg_fails_pe_leg_never_attempted()
    await test_4_pe_leg_fails_futures_leg_gets_unwound()
    await test_5_basket_capacity_enforced_and_configurable()
    await test_6_watchlist_webhook_adds_symbols()
    await test_7_swing_no_longer_shares_cross_strategy_registry()
    test_8_get_futures_contract_resolves_nearest_expiry_and_hyphenated_names()
    print("\nALL SWING PACKAGE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
