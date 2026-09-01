"""
Tests for Swing's proactive funds check (added 1 Sep 2026, user request,
following up on "what happens when the fund shortfall for any basket
order... does bot calculate its available funds and place trade for
next stock basket order which can fit well in available funds?"):
"yes build this get_margin_required and test and deploy it also."

Before this, a fund shortfall was only ever discovered REACTIVELY, via a
real broker-side RMS rejection at order-placement time. Now, every entry
path (basket/basket_hedge/sequential) resolves its contract(s), checks
the REAL required margin (Dhan's own /margincalculator, via the already-
existing get_margin_required) against the account's REAL available
balance (get_fund_limits), and skips the attempt BEFORE placing any real
order if it clearly won't fit - releasing capacity so the next-ranked
candidate (see test_swing_entry_ranking.py) gets tried in the same tick.
The broker's own RMS rejection remains the final safety net regardless
(Dhan's own combo margin benefit, if any, isn't visible to this check -
see _has_sufficient_funds's own docstring).

Also covers a related improvement bundled into this same change: ATM PE
resolution now happens BEFORE the futures leg is ever placed (for
basket/basket_hedge mode's 2-leg entry) - a PE lookup failure now aborts
with NEITHER leg placed, rather than the old behavior of buying futures
first and then unwinding it if the PE lookup failed afterward.

Covers, against the REAL production functions (not reimplemented), with
ONLY the Dhan network boundary mocked:
  1. `_has_sufficient_funds` - sufficient (required <= available) returns
     True; insufficient (required > available) returns False; fails
     OPEN to True on a margin-API or funds-API failure (never blocks
     real trading on a funds-check outage).
  2. `enter_basket_for_stock` (basket mode) skips an entry BEFORE
     placing any order when funds are insufficient - zero orders
     placed, capacity released, a durable `BASKET_INSUFFICIENT_FUNDS`
     event logged.
  3. `_enter_basket_hedge_for_stock` (basket_hedge mode, the live one)
     - identical behavior, own `BASKET_HEDGE_INSUFFICIENT_FUNDS` event.
  4. `_enter_futures_for_stock` (sequential mode, single-leg entry) -
     same check applied to just the one leg, own
     `SEQUENTIAL_INSUFFICIENT_FUNDS` event.
  5. The ranking + funds-check interaction: when the TOP-ranked
     candidate in a monitor tick can't be afforded, the NEXT-ranked one
     is tried instead, within the SAME tick - not just skipped for the
     day.
  6. ATM PE resolution happens before the futures leg is placed - a PE
     lookup failure means ZERO orders are placed at all (not the old
     buy-then-unwind behavior).

HOW TO RUN:
    uv run python tests/test_swing_funds_check.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_funds_check_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
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


def install_all_dhan_mocks(fill_prices=None, margin_per_leg=999.0, available_balance=100000.0,
                            margin_raises=None, funds_raises=None):
    fill_prices = list(fill_prices or [])
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
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0

    margin_calls = []

    def fake_get_margin_required(security_id, exchange_segment, transaction_type, quantity, product_type, price):
        margin_calls.append(security_id)
        if margin_raises:
            raise margin_raises
        return {"totalMargin": margin_per_leg}

    def fake_get_fund_limits():
        if funds_raises:
            raise funds_raises
        return {"availabelBalance": available_balance}

    odc.dhan_wrapper.get_margin_required = fake_get_margin_required
    odc.dhan_wrapper.get_fund_limits = fake_get_fund_limits

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
    return restore, placed_orders, margin_calls


async def test_1_has_sufficient_funds_core_logic():
    # (a) Sufficient - required <= available.
    restore, _, _ = install_all_dhan_mocks(margin_per_leg=1000.0, available_balance=5000.0)
    try:
        ok = await ste._has_sufficient_funds("TEST", [
            ("SEC1", "MARGIN", 100, 500.0), ("SEC2", "MARGIN", 100, 20.0),
        ])
        assert ok is True, "1000+1000=2000 required <= 5000 available must be sufficient"
    finally:
        restore()

    # (b) Insufficient - required > available.
    restore, _, _ = install_all_dhan_mocks(margin_per_leg=3000.0, available_balance=5000.0)
    try:
        ok = await ste._has_sufficient_funds("TEST", [
            ("SEC1", "MARGIN", 100, 500.0), ("SEC2", "MARGIN", 100, 20.0),
        ])
        assert ok is False, "3000+3000=6000 required > 5000 available must be insufficient"
    finally:
        restore()

    # (c) Fails OPEN (True) if get_margin_required itself fails.
    restore, _, _ = install_all_dhan_mocks(margin_raises=ConnectionError("simulated margin API failure"))
    try:
        ok = await ste._has_sufficient_funds("TEST", [("SEC1", "MARGIN", 100, 500.0)])
        assert ok is True, "a margin-API failure must fail OPEN (never block real trading on an outage)"
    finally:
        restore()

    # (d) Fails OPEN (True) if get_fund_limits itself fails.
    restore, _, _ = install_all_dhan_mocks(funds_raises=ConnectionError("simulated funds API failure"))
    try:
        ok = await ste._has_sufficient_funds("TEST", [("SEC1", "MARGIN", 100, 500.0)])
        assert ok is True, "a funds-API failure must fail OPEN (never block real trading on an outage)"
    finally:
        restore()

    print("1. _has_sufficient_funds correctly sums every leg's own standalone margin requirement and "
          "compares against available balance (sufficient/insufficient both correct), and fails OPEN "
          "to 'sufficient' on either the margin-API or funds-API itself failing - a funds-check "
          "outage must never block real trading: PASSED")


async def test_2_basket_mode_skips_before_any_order_when_insufficient():
    store = sps.BasketStore()
    ste.basket_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True

    restore, placed_orders, margin_calls = install_all_dhan_mocks(margin_per_leg=60000.0, available_balance=50000.0)
    try:
        result = await ste.enter_basket_for_stock("RELIANCE")
        assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", result
        assert placed_orders == [], f"NO order should ever be placed when funds are insufficient, got {placed_orders}"
        assert len(margin_calls) == 2, "both legs' own margin must be checked (futures AND PE)"
        assert "RELIANCE" not in store.live_baskets
        assert "RELIANCE" not in store.reserved_symbols, "capacity must be released so a later attempt can retry"

        await asyncio.sleep(0.2)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["event"] == "BASKET_INSUFFICIENT_FUNDS" and e["underlying_symbol"] == "RELIANCE"]
        assert len(events) == 1, events

        print("2. enter_basket_for_stock (basket mode) skips the entry BEFORE placing any real order "
              "when the combined futures+PE margin exceeds available funds - zero orders placed, "
              "capacity released, a durable BASKET_INSUFFICIENT_FUNDS event logged: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_3_basket_hedge_mode_skips_before_any_order_when_insufficient():
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True

    restore, placed_orders, margin_calls = install_all_dhan_mocks(margin_per_leg=60000.0, available_balance=50000.0)
    try:
        result = await ste._enter_basket_hedge_for_stock("HCLTECH")
        assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", result
        assert placed_orders == []
        assert "HCLTECH" not in store.live_positions
        assert "HCLTECH" not in store.reserved_symbols

        await asyncio.sleep(0.2)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["event"] == "BASKET_HEDGE_INSUFFICIENT_FUNDS" and e["underlying_symbol"] == "HCLTECH"]
        assert len(events) == 1, events

        print("3. _enter_basket_hedge_for_stock (basket_hedge mode, the live one) skips the same way "
              "- zero orders placed, capacity released, a durable BASKET_HEDGE_INSUFFICIENT_FUNDS "
              "event logged: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_4_sequential_mode_skips_before_any_order_when_insufficient():
    store = sps.SequentialPositionStore()
    ste.sequential_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True

    restore, placed_orders, margin_calls = install_all_dhan_mocks(margin_per_leg=60000.0, available_balance=50000.0)
    try:
        result = await ste._enter_futures_for_stock("TCS")
        assert result["status"] == "skipped" and result["reason"] == "insufficient_funds", result
        assert placed_orders == []
        assert len(margin_calls) == 1, "only ONE leg here (futures-only entry at NONE->FUTURES)"
        assert "TCS" not in store.live_legs
        assert "TCS" not in store.reserved_symbols

        await asyncio.sleep(0.2)
        events = [e for e in trade_history.read_all_jsonl(ste.SWING_EVENTS_LOG_NAME)
                  if e["event"] == "SEQUENTIAL_INSUFFICIENT_FUNDS" and e["underlying_symbol"] == "TCS"]
        assert len(events) == 1, events

        print("4. _enter_futures_for_stock (sequential mode's single-leg entry) applies the same "
              "check to just the one leg - zero orders placed, capacity released, a durable "
              "SEQUENTIAL_INSUFFICIENT_FUNDS event logged: PASSED")
    finally:
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_5_ranked_tick_falls_through_to_next_candidate_when_top_cannot_afford():
    """The interaction with entry-candidate ranking (user's own original
    framing: "does bot... place trade for next stock basket order which
    can fit well in available funds?") - the top-ranked candidate this
    tick can't be afforded, so the NEXT-ranked one is tried instead,
    within the SAME tick, not just skipped for the rest of the day."""
    store = sps.BasketHedgeStore()
    ste.basket_hedge_store = store
    wl_store = swl.WatchlistStore()
    ste.watchlist_store = wl_store
    await wl_store.add_symbols(["EXPENSIVE", "AFFORDABLE"])

    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True
    real_cap = ste.config.MAX_LIVE_BASKETS
    ste.config.MAX_LIVE_BASKETS = 1

    from datetime import datetime
    entry_states = {
        "EXPENSIVE": ste.SupertrendState(candle_start=datetime(2026, 9, 1, 11, 0, tzinfo=ste.IST), close=110.0,
                                          supertrend=100.0, is_above=True, prev_close=95.0, prev_supertrend=100.0,
                                          prev_is_above=False, volume=999999.0),  # ranked FIRST (highest volume)
        "AFFORDABLE": ste.SupertrendState(candle_start=datetime(2026, 9, 1, 11, 0, tzinfo=ste.IST), close=110.0,
                                           supertrend=100.0, is_above=True, prev_close=95.0, prev_supertrend=100.0,
                                           prev_is_above=False, volume=1.0),  # ranked SECOND (lowest volume)
    }

    def confirm_state():
        return ste.SupertrendState(candle_start=datetime.now(ste.IST), close=10.0, supertrend=9.0, is_above=True,
                                    prev_close=9.5, prev_supertrend=9.0, prev_is_above=True, volume=0.0)

    async def fake_fetch(symbol, interval_minutes):
        if interval_minutes == ste.config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES:
            return entry_states[symbol]
        return confirm_state()

    async def fake_price_confirmed(symbol):
        return True

    real_fetch = ste._fetch_supertrend_state
    real_price_confirmed = ste._is_price_confirmed_above_prev_close
    ste._fetch_supertrend_state = fake_fetch
    ste._is_price_confirmed_above_prev_close = fake_price_confirmed

    # EXPENSIVE's own margin requirement exceeds available funds by
    # itself; AFFORDABLE's does not - but get_margin_required here can't
    # tell symbols apart by security_id alone in this simple fake, so
    # instead make the CHECK symbol-aware directly.
    def fake_get_margin_required(security_id, exchange_segment, transaction_type, quantity, product_type, price):
        if "EXPENSIVE" in security_id:
            return {"totalMargin": 60000.0}
        return {"totalMargin": 100.0}

    restore, placed_orders, _ = install_all_dhan_mocks(fill_prices=[100.0, 20.0], available_balance=50000.0)
    odc.dhan_wrapper.get_margin_required = fake_get_margin_required
    try:
        await ste._basket_hedge_monitor_tick()

        assert "AFFORDABLE" in store.live_positions, \
            f"the next-ranked, AFFORDABLE candidate must be entered once EXPENSIVE is skipped, got {list(store.live_positions)}"
        assert "EXPENSIVE" not in store.live_positions
        assert "EXPENSIVE" in await wl_store.symbols(), \
            "EXPENSIVE stays on the watchlist - it was skipped for funds, not permanently rejected"

        entered_symbols = {o["trading_symbol"].split(" ")[0] for o in placed_orders}
        assert entered_symbols == {"AFFORDABLE"}, \
            f"only AFFORDABLE's own orders should have been placed, got {placed_orders}"

        print("5. When the TOP-ranked candidate this tick can't be afforded, the funds check skips "
              "it (no order placed) and the ranked-entry loop automatically falls through to the "
              "NEXT-ranked candidate within the SAME tick - answering the user's own original "
              "question directly: PASSED")
    finally:
        restore()
        ste._fetch_supertrend_state = real_fetch
        ste._is_price_confirmed_above_prev_close = real_price_confirmed
        ste.config.STRATEGY_ENABLED = real_enabled
        ste.config.MAX_LIVE_BASKETS = real_cap


async def test_6_pe_lookup_failure_places_zero_orders():
    """Bundled improvement: ATM PE resolution now happens BEFORE the
    futures leg is placed (basket/basket_hedge mode) - a PE lookup
    failure means NEITHER leg is ever placed, unlike the old behavior of
    buying futures first and unwinding it afterward."""
    store = sps.BasketStore()
    ste.basket_store = store
    real_enabled = ste.config.STRATEGY_ENABLED
    ste.config.STRATEGY_ENABLED = True

    restore, placed_orders, _ = install_all_dhan_mocks()
    real_get_atm = odc.dhan_wrapper.get_atm_option

    def failing_atm_option(symbol, option_type):
        raise ValueError("simulated ATM lookup failure")

    odc.dhan_wrapper.get_atm_option = failing_atm_option
    try:
        result = await ste.enter_basket_for_stock("SBIN")
        assert result["status"] == "error" and "pe_lookup_failed" in result["reason"], result
        assert placed_orders == [], \
            f"ATM lookup failing BEFORE any order is placed must mean ZERO orders, got {placed_orders}"
        assert "SBIN" not in store.reserved_symbols

        print("6. A PE lookup failure now aborts BEFORE any order is placed at all (ATM resolution "
              "moved ahead of the futures BUY) - zero real orders placed, not the old buy-then-"
              "unwind behavior: PASSED")
    finally:
        odc.dhan_wrapper.get_atm_option = real_get_atm
        restore()
        ste.config.STRATEGY_ENABLED = real_enabled


async def test_7_feature_flag_disables_the_proactive_check():
    real_enabled_flag = ste.config.FUNDS_CHECK_ENABLED
    ste.config.FUNDS_CHECK_ENABLED = False
    restore, _, margin_calls = install_all_dhan_mocks(margin_raises=RuntimeError("must never be called"))
    try:
        ok = await ste._has_sufficient_funds("TEST", [("SEC1", "MARGIN", 100, 500.0)])
        assert ok is True, "with the flag off, must read as sufficient without even attempting the check"
        assert margin_calls == [], "get_margin_required must never even be called while the flag is off"
        print("7. config.FUNDS_CHECK_ENABLED=False disables the proactive check entirely - reads as "
              "sufficient without calling get_margin_required at all (the broker's own reactive RMS "
              "rejection is unaffected by this flag either way): PASSED")
    finally:
        restore()
        ste.config.FUNDS_CHECK_ENABLED = real_enabled_flag


async def main():
    print("=== Swing proactive funds check test suite ===\n")
    await test_1_has_sufficient_funds_core_logic()
    await test_2_basket_mode_skips_before_any_order_when_insufficient()
    await test_3_basket_hedge_mode_skips_before_any_order_when_insufficient()
    await test_4_sequential_mode_skips_before_any_order_when_insufficient()
    await test_5_ranked_tick_falls_through_to_next_candidate_when_top_cannot_afford()
    await test_6_pe_lookup_failure_places_zero_orders()
    await test_7_feature_flag_disables_the_proactive_check()
    print("\nALL SWING FUNDS CHECK TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
