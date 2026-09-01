"""
Full end-to-end INTEGRATION suite for the Swing package - user request
31 Aug 2026 ("add and run integration test suite also"), on top of
tests/test_swing_package.py's own engine-level coverage (all-or-nothing
rollback, capacity, get_futures_contract, cross_strategy_registry).

This file goes one layer further: complete real-world FLOWS through the
actual webhook functions end to end - multi-stock mixed-outcome entries,
a full enter-then-square-off lifecycle with trade_history verification,
and broker-position reconciliation scenarios test_swing_package.py never
exercises at all (a realistic 4-way mixed reconciliation alongside
Options/Futures/Luxury, and the "unpaired leg" safety behavior - a
Swing-attributed futures position with no matching PE leg must NOT be
silently reconstructed into a partial basket).

Same mocking philosophy as every other deep-integration suite in this
repo - mocks only the Dhan network boundary (the shared dhan_wrapper
singleton), keeps all real webhook/engine/store/trade_history LOGIC
completely real.

Covers:
  1. Full webhook-level multi-stock entry with a MIXED outcome (some
     entered, some skipped for capacity) - through the real
     chartink_webhook_swing_enter function, not enter_basket_for_stock
     directly.
  2. A full lifecycle: enter a basket via the real webhook, confirm it's
     live via basket_store.snapshot(), manually square it off, confirm
     it's closed with the right reason and BOTH legs appear as closed
     trades in trade_history tagged "Swing".
  3. A realistic 4-way reconciliation: Options-owned, Futures-owned,
     Luxury-owned, AND a genuine Swing-owned (paired FUTSTK+OPTSTK)
     broker position all at once - Swing's own reconcile_broker_positions
     picks up only its own paired basket, zero cross-contamination with
     any of the other three.
  4. The "unpaired leg" safety behavior: a Swing-attributed FUTSTK
     position with NO matching OPTSTK counterpart must NOT be
     reconstructed into a partial basket - reconcile_broker_positions()
     must return nothing for it (a clear warning is logged instead).
  5. The watchlist webhook and the enter webhook operating independently
     in the same flow - adding a stock to the watchlist doesn't affect a
     separate basket entry for a different stock, and both webhook
     alerts persist correctly tagged ("Swing" vs "Swing-Watchlist").

HOW TO RUN:
    uv run python tests/test_swing_integration.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_integration_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
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


def install_all_dhan_mocks():
    """Same completeness as test_swing_package.py's own helper (includes
    refresh_supertrend_signal/get_cached_supertrend_candle_start so a real
    cross-package call never falls through to a live Dhan login - see
    that file's own test-construction-bug note for why this matters)."""
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
        "get_open_fno_positions": odc.dhan_wrapper.get_open_fno_positions,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.place_market_order = lambda trading_symbol, quantity, transaction_type, tag=None, product_type=None: {
        "order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=1, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


async def test_1_full_webhook_multi_stock_mixed_capacity_outcome():
    store = sps.BasketStore()
    sm.basket_store = store
    ste.basket_store = store

    real_enabled, real_cap = ste.config.STRATEGY_ENABLED, ste.config.MAX_LIVE_BASKETS
    real_mode = ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = True
    ste.config.MAX_LIVE_BASKETS = 2
    # This suite specifically exercises BASKET mode's own mechanics (the
    # webhook is now mode-dispatched, see swing_main.py's own docstring -
    # added 1 Sep 2026, default mode moved to "sequential") - pin it
    # explicitly rather than relying on whatever the ambient default is.
    ste.config.STRATEGY_MODE = "basket"
    restore = install_all_dhan_mocks()
    try:
        payload = sm.SwingWebhookPayload(
            stocks="RELIANCE,TCS,SBIN", alert_name="integration-test-1",
        )
        result = await sm.chartink_webhook_swing_enter(payload)
        assert result["status"] == "processed", result

        entered = [e for e in result["entries"] if e["status"] == "entered"]
        skipped = [e for e in result["entries"] if e["status"] == "skipped"]
        assert len(entered) == 2, f"expected exactly 2 entered (capacity=2), got {result['entries']}"
        assert len(skipped) == 1, f"expected exactly 1 skipped, got {result['entries']}"
        assert skipped[0]["reason"] == "duplicate_or_capacity_full"
        assert len(store.live_baskets) == 2

        await asyncio.sleep(0.3)
        alerts = trade_history.read_all_jsonl("webhook_alerts")
        swing_alerts = [a for a in alerts if a["strategy"] == "Swing"]
        assert len(swing_alerts) == 1 and swing_alerts[0]["status"] == "processed"
        print("1. Full webhook-level multi-stock entry through the REAL chartink_webhook_swing_enter: "
              "3 requested, exactly 2 entered (capacity=2), 1 correctly skipped, webhook_alerts logged: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap
        ste.config.STRATEGY_MODE = real_mode


async def test_2_full_lifecycle_enter_then_manual_squareoff():
    store = sps.BasketStore()
    sm.basket_store = store
    ste.basket_store = store

    real_enabled = ste.config.STRATEGY_ENABLED
    real_mode = ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "basket"  # see test_1's own comment
    restore = install_all_dhan_mocks()
    try:
        enter_payload = sm.SwingWebhookPayload(stocks="RELIANCE", alert_name="integration-test-2")
        enter_result = await sm.chartink_webhook_swing_enter(enter_payload)
        assert enter_result["entries"][0]["status"] == "entered", enter_result

        snapshot = await store.snapshot()
        assert len(snapshot["live_baskets"]) == 1
        assert snapshot["live_baskets"][0]["underlying_symbol"] == "RELIANCE"

        squareoff_result = await sm.manual_square_off()
        assert squareoff_result["live_baskets"] == [], "basket must be fully closed after manual square-off"
        assert len(squareoff_result["closed_baskets_today"]) == 1
        closed = squareoff_result["closed_baskets_today"][0]
        assert closed["exit_reason"] == "MANUAL_SQUARE_OFF"
        assert closed["futures_leg"]["status"] == "CLOSED"
        assert closed["option_leg"]["status"] == "CLOSED"

        await asyncio.sleep(0.3)
        closed_trades = trade_history.read_all_jsonl("real_trades")
        swing_closed = [t for t in closed_trades if t["strategy"] == "Swing" and t["underlying_symbol"] == "RELIANCE"]
        assert len(swing_closed) == 2, f"expected both legs closed in trade_history, got {swing_closed}"
        assert {t["option_type"] for t in swing_closed} == {"FUT", "PE"}
        assert all(t["exit_reason"] == "MANUAL_SQUARE_OFF" for t in swing_closed)
        print("2. Full lifecycle (webhook entry -> live basket -> manual square-off -> both legs "
              "closed in trade_history, tagged Swing, reason MANUAL_SQUARE_OFF): PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = real_enabled
        ste.config.STRATEGY_MODE = real_mode


async def test_3_four_way_reconciliation_with_a_real_swing_basket():
    """Extends the same pattern test_luxury_package.py's own 3-way
    reconciliation test uses, to 4 strategies - Options, Futures, Luxury,
    and Swing (whose basket needs TWO broker positions, FUTSTK+OPTSTK,
    correctly paired) all with real broker positions at once."""
    class _FakePos:
        def __init__(self, symbol, ts, entry_price):
            self.underlying_symbol = symbol
            self.option_trading_symbol = ts
            self.option_type = "CE"
            self.quantity = 100
            self.entry_price = entry_price
            self.opened_at = datetime.now()
            self.order_id = "OID"

    swing_futures_leg = sps.Leg(
        underlying_symbol="HDFCBANK", option_trading_symbol="HDFCBANK-Sep2026-FUT", option_type="FUT",
        quantity=250, lot_size=250, entry_price=1700.0, order_id="OID-FUT", product_type="MARGIN",
    )
    swing_option_leg = sps.Leg(
        underlying_symbol="HDFCBANK", option_trading_symbol="HDFCBANK-Sep2026-1700-PE", option_type="PE",
        quantity=250, lot_size=250, entry_price=30.0, order_id="OID-PE", product_type="MARGIN",
    )

    await trade_history.record_opened_position("Options", _FakePos("RELIANCE", "RELIANCE 25 SEP 1400 CALL", 20.0))
    await trade_history.record_opened_position("Futures", _FakePos("SBIN", "SBIN 25 SEP 800 CALL", 15.0))
    await trade_history.record_opened_position("Luxury", _FakePos("TCS", "TCS 25 SEP 4000 CALL", 50.0))
    await trade_history.record_opened_position("Swing", swing_futures_leg)
    await trade_history.record_opened_position("Swing", swing_option_leg)
    await asyncio.sleep(0.2)

    fake_broker_positions = [
        {"trading_symbol": "RELIANCE 25 SEP 1400 CALL", "underlying_symbol": "RELIANCE", "option_type": "CE",
         "lot_size": 500, "quantity": 500, "avg_price": 20.0, "product_type": "MARGIN"},
        {"trading_symbol": "SBIN 25 SEP 800 CALL", "underlying_symbol": "SBIN", "option_type": "CE",
         "lot_size": 750, "quantity": 750, "avg_price": 15.0, "product_type": "MARGIN"},
        {"trading_symbol": "TCS 25 SEP 4000 CALL", "underlying_symbol": "TCS", "option_type": "CE",
         "lot_size": 150, "quantity": 150, "avg_price": 50.0, "product_type": "MARGIN"},
        {"trading_symbol": "HDFCBANK-Sep2026-FUT", "underlying_symbol": "HDFCBANK", "option_type": None,
         "lot_size": 250, "quantity": 250, "avg_price": 1700.0, "product_type": "MARGIN"},
        {"trading_symbol": "HDFCBANK-Sep2026-1700-PE", "underlying_symbol": "HDFCBANK", "option_type": "PE",
         "lot_size": 250, "quantity": 250, "avg_price": 30.0, "product_type": "MARGIN"},
    ]
    real_get_open = odc.dhan_wrapper.get_open_fno_positions
    real_subscribe = odc.dhan_wrapper.subscribe_option_price
    odc.dhan_wrapper.get_open_fno_positions = lambda: fake_broker_positions
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    try:
        swing_reconciled = await ste.reconcile_broker_positions()
        assert len(swing_reconciled) == 1, f"expected exactly 1 Swing basket, got {swing_reconciled}"
        basket = swing_reconciled[0]
        assert basket.underlying_symbol == "HDFCBANK"
        assert basket.futures_leg.option_trading_symbol == "HDFCBANK-Sep2026-FUT"
        assert basket.option_leg.option_trading_symbol == "HDFCBANK-Sep2026-1700-PE"
        assert basket.futures_leg.reconciled and basket.option_leg.reconciled
        # Confirm zero cross-contamination - none of the other 3 strategies' symbols leaked in.
        reconciled_symbols = {b.underlying_symbol for b in swing_reconciled}
        assert reconciled_symbols == {"HDFCBANK"}, f"Swing must not pick up RELIANCE/SBIN/TCS: {reconciled_symbols}"
        print("3. 4-way reconciliation (Options/Futures/Luxury/Swing all with real broker positions "
              "at once): Swing correctly reconciled ONLY its own paired HDFCBANK basket "
              "(FUTSTK+OPTSTK), zero cross-contamination with the other 3 strategies: PASSED")
    finally:
        odc.dhan_wrapper.get_open_fno_positions = real_get_open
        odc.dhan_wrapper.subscribe_option_price = real_subscribe


async def test_4_unpaired_leg_is_never_reconstructed_into_a_partial_basket():
    """A Swing-attributed FUTSTK position with NO matching OPTSTK leg -
    reconcile_broker_positions() must return NOTHING for it, not a
    partial/unhedged basket."""
    orphan_leg = sps.Leg(
        underlying_symbol="INFY", option_trading_symbol="INFY-Sep2026-FUT", option_type="FUT",
        quantity=300, lot_size=300, entry_price=1500.0, order_id="OID-ORPHAN", product_type="MARGIN",
    )
    await trade_history.record_opened_position("Swing", orphan_leg)
    await asyncio.sleep(0.2)

    fake_broker_positions = [
        {"trading_symbol": "INFY-Sep2026-FUT", "underlying_symbol": "INFY", "option_type": None,
         "lot_size": 300, "quantity": 300, "avg_price": 1500.0, "product_type": "MARGIN"},
    ]
    real_get_open = odc.dhan_wrapper.get_open_fno_positions
    odc.dhan_wrapper.get_open_fno_positions = lambda: fake_broker_positions
    try:
        reconciled = await ste.reconcile_broker_positions()
        assert reconciled == [], \
            f"an unpaired leg must NEVER be reconstructed into a partial basket, got {reconciled}"
        print("4. An unpaired Swing leg (futures with no matching PE) is correctly left "
              "un-reconciled (not silently turned into a partial/unhedged basket): PASSED")
    finally:
        odc.dhan_wrapper.get_open_fno_positions = real_get_open


async def test_5_watchlist_and_enter_webhooks_operate_independently():
    store = sps.BasketStore()
    sm.basket_store = store
    ste.basket_store = store
    wl_store = swl.WatchlistStore()
    sm.watchlist_store = wl_store

    real_enabled = ste.config.STRATEGY_ENABLED
    real_mode = ste.config.STRATEGY_MODE
    ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = True
    ste.config.STRATEGY_MODE = "basket"  # see test_1's own comment
    restore = install_all_dhan_mocks()
    try:
        watchlist_payload = sm.SwingWebhookPayload(stocks="ICICIBANK", alert_name="integration-test-5-watchlist")
        watchlist_result = await sm.chartink_webhook_swing_watchlist(watchlist_payload)
        assert watchlist_result["status"] == "processed"
        assert watchlist_result["added"] == ["ICICIBANK"]

        enter_payload = sm.SwingWebhookPayload(stocks="WIPRO", alert_name="integration-test-5-enter")
        enter_result = await sm.chartink_webhook_swing_enter(enter_payload)
        assert enter_result["entries"][0]["status"] == "entered", enter_result

        # The watchlist add must not have affected the basket, and the
        # basket entry must not have affected the watchlist.
        assert "WIPRO" not in await wl_store.symbols()
        assert "ICICIBANK" not in store.live_baskets
        assert "ICICIBANK" in await wl_store.symbols()
        assert "WIPRO" in store.live_baskets

        await asyncio.sleep(0.3)
        alerts = trade_history.read_all_jsonl("webhook_alerts")
        swing_enter_alerts = [a for a in alerts if a["strategy"] == "Swing"
                               and "integration-test-5" in (a["alert_name"] or "")]
        swing_watchlist_alerts = [a for a in alerts if a["strategy"] == "Swing-Watchlist"]
        assert len(swing_enter_alerts) == 1
        assert len(swing_watchlist_alerts) >= 1
        print("5. Watchlist webhook and enter webhook operate fully independently in the same "
              "flow - each only affects its own store, both correctly tagged in webhook_alerts "
              "('Swing' vs 'Swing-Watchlist'): PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = sm.config.STRATEGY_ENABLED = real_enabled
        ste.config.STRATEGY_MODE = real_mode


async def main():
    print("=== Swing package FULL INTEGRATION test suite ===\n")
    await test_1_full_webhook_multi_stock_mixed_capacity_outcome()
    await test_2_full_lifecycle_enter_then_manual_squareoff()
    await test_3_four_way_reconciliation_with_a_real_swing_basket()
    await test_4_unpaired_leg_is_never_reconstructed_into_a_partial_basket()
    await test_5_watchlist_and_enter_webhooks_operate_independently()
    print("\nALL SWING INTEGRATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
