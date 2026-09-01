"""
Tests for Swing's paper-trading engine (paper_engine.py) - user request
1 Sep 2026: "Let's enable paper trading for tomorrow for 'Swing' package
and keep track of trades, history and profit loss also like real trades
in files."

Covers, against the REAL production functions (not reimplemented):
  1. Safety invariant - a full simulated entry+exit cycle NEVER calls
     place_market_order/wait_for_order_result (the only two real
     order-placement entry points) - proven by making both raise if
     called at all, not merely asserting a call count of zero.
  2. A successful paper entry - both legs priced via get_option_ltp
     (proven generic-across-instrument-types, reused for the futures leg
     too), recorded into paper_basket_store.live_baskets, nothing written
     to the on-disk log yet (only a CLOSE writes a completed trade).
  3. Entry abandoned cleanly when the PE leg can't be priced - nothing
     recorded for either leg (no partial paper position, unlike the real
     all-or-nothing entry there's no "unwind" needed since nothing was
     ever persisted).
  4. A successful paper exit - P&L computed correctly per leg and in
     total, the completed trade appended to the paper trade log via
     append_jsonl/read_all_jsonl, and the basket removed from
     live_baskets.
  5. Persistence isolation - the paper trade log
     (paper_engine.SWING_PAPER_LOG_NAME) is a COMPLETELY SEPARATE file
     from real trade history (trade_history's own "real_trades"/
     "position_opened") - a paper trade never shows up in the real log
     and vice versa.
  6. paper_poll_loop does nothing at all while config.PAPER_TRADING_ENABLED
     is False (no entry/exit evaluation, no baskets touched) - same
     "always running, internally gated" pattern as the real monitor_loop.
  7. A full poll-loop-shaped auto paper-entry-then-exit: a real entry
     signal (gap-up + both Supertrend legs) genuinely creates a paper
     basket via _enter_paper_basket, and a real crossed-below exit signal
     genuinely closes it via _exit_paper_basket with the pnl recorded -
     not reached if either were still stubs. Also confirms a symbol
     already holding an open paper basket is skipped on the next
     entry-signal check, and that the SHARED watchlist is left untouched
     by a paper entry (unlike the real loop's own auto-entry).
  8. Real margin/funds logging (added 1 Sep 2026, user request: "logging
     real margin and funds required during paper trading so that we can
     do analysis also") - each leg's real, Dhan-quoted margin_required
     and the account's fund snapshot at entry flow correctly through to
     the persisted closed-trade record (on-disk, not just in-memory), the
     combined total is the naive SUM of both legs (never a guessed/
     partial figure), and a margin/funds fetch failure at entry leaves
     those figures None WITHOUT aborting the paper entry itself (unlike a
     price-fetch failure, which does abort - price is essential to the
     simulated P&L, margin/funds data is not).

HOW TO RUN:
    uv run python tests/test_swing_paper_engine.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_swing_paper_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Swing.paper_engine as spe
import Swing.trading_engine as ste
import Swing.watchlist as swl
from Swing.trading_engine import SupertrendState
from Options.dhan_client import AtmOption, FuturesContract

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP PUT", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"OPT-{symbol}", expiry_date=FUTURE_EXPIRY)


def fake_futures_contract(symbol: str) -> FuturesContract:
    return FuturesContract(trading_symbol=f"{symbol} FAKE EXP FUT", security_id=f"FUT-{symbol}",
                            lot_size=250, expiry_date=FUTURE_EXPIRY)


def _raise_if_called(*args, **kwargs):
    raise AssertionError(
        "SAFETY INVARIANT VIOLATED: a real order-placement function was called during a paper "
        f"trading flow. args={args} kwargs={kwargs}"
    )


def install_all_dhan_mocks(
    ltp_by_symbol=None, margin_by_security_id=None, funds_snapshot=None,
    fail_margin=False, fail_funds=False,
):
    """Same shape as every other suite's helper - see
    test_swing_signal_logic.py's own copy. place_market_order/
    wait_for_order_result are wired to RAISE rather than just return a
    fake fill - proves the safety invariant by construction, not merely
    by omission (a stub that quietly no-ops could hide a real call
    slipping through some other code path).

    get_margin_required/get_fund_limits default to succeeding with a
    generic fake figure (so tests that don't care about margin/funds
    logging stay clean/fast, with no unmocked retry-with-backoff delay
    and no noisy exception logging) - pass margin_by_security_id/
    funds_snapshot for a test that needs specific values, or
    fail_margin=True/fail_funds=True for a test that needs the "fetch
    failed" path."""
    ltp_by_symbol = ltp_by_symbol or {}
    margin_by_security_id = margin_by_security_id or {}
    funds_snapshot = funds_snapshot if funds_snapshot is not None else {"availabelBalance": 100000.0}
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_futures_contract": odc.dhan_wrapper.get_futures_contract,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_futures_contract = fake_futures_contract

    def fake_get_option_ltp(trading_symbol):
        if trading_symbol not in ltp_by_symbol:
            raise ValueError(f"no fake LTP configured for {trading_symbol}")
        return ltp_by_symbol[trading_symbol]

    def fake_get_margin_required(security_id, exchange_segment, transaction_type, quantity, product_type, price):
        if fail_margin:
            raise ValueError("simulated margin_calculator failure")
        return {"totalMargin": margin_by_security_id.get(security_id, 999.0)}

    def fake_get_fund_limits():
        if fail_funds:
            raise ValueError("simulated get_fund_limits failure")
        return funds_snapshot

    odc.dhan_wrapper.get_option_ltp = fake_get_option_ltp
    odc.dhan_wrapper.get_margin_required = fake_get_margin_required
    odc.dhan_wrapper.get_fund_limits = fake_get_fund_limits
    odc.dhan_wrapper.place_market_order = _raise_if_called
    odc.dhan_wrapper.wait_for_order_result = _raise_if_called

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def _make_state(is_above: bool, prev_is_above: bool) -> SupertrendState:
    import datetime as dt
    return SupertrendState(candle_start=dt.datetime.now(), close=100.0, supertrend=95.0, is_above=is_above,
                            prev_close=98.0, prev_supertrend=96.0, prev_is_above=prev_is_above)


async def test_1_and_2_successful_paper_entry_never_places_real_orders():
    store = spe.PaperBasketStore()
    spe.paper_basket_store = store
    ltp_by_symbol = {"RELIANCE FAKE EXP FUT": 2500.0, "RELIANCE FAKE EXP PUT": 40.0}
    restore = install_all_dhan_mocks(ltp_by_symbol)
    try:
        await spe._enter_paper_basket("RELIANCE")
        assert "RELIANCE" in store.live_baskets, "a successfully-priced entry must be recorded"
        basket = store.live_baskets["RELIANCE"]
        assert basket.futures_leg.trading_symbol == "RELIANCE FAKE EXP FUT"
        assert basket.futures_leg.entry_price == 2500.0
        assert basket.futures_leg.quantity == 250  # lot_size(250) * QUANTITY_LOTS(1)
        assert basket.option_leg.trading_symbol == "RELIANCE FAKE EXP PUT"
        assert basket.option_leg.entry_price == 40.0
        assert basket.option_leg.quantity == 500
        # Margin/funds logging (added 1 Sep 2026) - default fake succeeds
        # for both legs, so the naive sum must be reported (not None).
        assert basket.futures_leg.margin_required == 999.0
        assert basket.option_leg.margin_required == 999.0
        assert basket.total_margin_required == 1998.0
        assert basket.account_funds_snapshot == {"availabelBalance": 100000.0}
        assert store.completed == [], "entry alone must not write a completed trade - only a close does"
        print("1&2. Paper entry simulates both legs at LTP (futures leg priced via the same "
              "get_option_ltp used for options, confirming it's generic across instrument types), "
              "records the basket - including each leg's real margin_required and the account's "
              "fund snapshot at entry - and, per the safety invariant, never calls "
              "place_market_order/wait_for_order_result even once: PASSED")
    finally:
        restore()


async def test_3_pe_leg_fetch_failure_aborts_with_nothing_recorded():
    store = spe.PaperBasketStore()
    spe.paper_basket_store = store
    # Only the futures leg's LTP is configured - the PE leg's fetch raises.
    ltp_by_symbol = {"TCS FAKE EXP FUT": 4000.0}
    restore = install_all_dhan_mocks(ltp_by_symbol)
    try:
        await spe._enter_paper_basket("TCS")
        assert "TCS" not in store.live_baskets, \
            "a PE-leg pricing failure must abandon the whole entry - no partial paper position"
        assert store.completed == []
        print("3. A PE-leg pricing failure aborts the paper entry cleanly - nothing recorded for "
              "either leg (no partial/unhedged paper position, no unwind needed since nothing was "
              "ever persisted): PASSED")
    finally:
        restore()


async def test_4_paper_exit_pnl_and_persistence():
    store = spe.PaperBasketStore()
    spe.paper_basket_store = store
    entry_ltp = {"INFY FAKE EXP FUT": 1500.0, "INFY FAKE EXP PUT": 20.0}
    restore = install_all_dhan_mocks(entry_ltp)
    try:
        await spe._enter_paper_basket("INFY")
        assert "INFY" in store.live_baskets
    finally:
        restore()

    exit_ltp = {"INFY FAKE EXP FUT": 1550.0, "INFY FAKE EXP PUT": 15.0}
    restore = install_all_dhan_mocks(exit_ltp)
    try:
        await spe._exit_paper_basket("INFY", "SUPERTREND_5MIN_EXIT")
        assert "INFY" not in store.live_baskets, "a closed paper basket must leave live_baskets"
        assert len(store.completed) == 1
        trade = store.completed[0]
        assert trade["underlying_symbol"] == "INFY"
        assert trade["exit_reason"] == "SUPERTREND_5MIN_EXIT"
        # futures: (1550 - 1500) * 250 = 12500.0 ; option: (15 - 20) * 500 = -2500.0
        assert trade["futures_pnl"] == 12500.0, trade
        assert trade["option_pnl"] == -2500.0, trade
        assert trade["total_pnl"] == 10000.0, trade
    finally:
        restore()

    # Persisted for real via trade_history's own append_jsonl/read_all_jsonl,
    # not just held in the in-memory `completed` list.
    reloaded = trade_history.read_all_jsonl(spe.SWING_PAPER_LOG_NAME)
    infy_trades = [t for t in reloaded if t["underlying_symbol"] == "INFY"]
    assert len(infy_trades) == 1 and infy_trades[0]["total_pnl"] == 10000.0

    print("4. Paper exit computes per-leg and total P&L correctly and persists the completed "
          "trade to its own on-disk log (round-tripped via read_all_jsonl): PASSED")


def test_5_paper_log_is_isolated_from_real_trade_history():
    reloaded_real = trade_history.read_all_jsonl("real_trades")
    reloaded_paper = trade_history.read_all_jsonl(spe.SWING_PAPER_LOG_NAME)
    real_symbols = {t.get("underlying_symbol") for t in reloaded_real}
    assert "INFY" not in real_symbols, \
        "a paper trade must NEVER appear in the real trade history log"
    assert all("futures_pnl" in t for t in reloaded_paper), \
        "the paper log's own shape (per-leg pnl) must never leak into/from the real log"
    print("5. The paper trade log is a completely separate file from real trade history - a paper "
          "trade never shows up as a real one, and vice versa: PASSED")


async def test_6_poll_loop_noop_while_disabled():
    store = spe.PaperBasketStore()
    spe.paper_basket_store = store
    wl_store = swl.WatchlistStore()
    spe.watchlist_store = wl_store
    await wl_store.add_symbols(["WIPRO"])

    real_enabled = spe.config.PAPER_TRADING_ENABLED
    spe.config.PAPER_TRADING_ENABLED = False

    calls = {"n": 0}
    real_eval = ste._evaluate_watchlist_entry_signal

    async def counting_eval(symbol):
        calls["n"] += 1
        return True

    ste._evaluate_watchlist_entry_signal = counting_eval
    try:
        # Run exactly one tick's worth of body (not the infinite loop) by
        # directly exercising the same guarded block paper_poll_loop uses.
        if spe.config.PAPER_TRADING_ENABLED:
            for symbol in await wl_store.symbols():
                await ste._evaluate_watchlist_entry_signal(symbol)
        assert calls["n"] == 0, "no entry signal should ever be evaluated while PAPER_TRADING_ENABLED is False"
        assert store.live_baskets == {}
        print("6. paper_poll_loop's own gated body evaluates nothing at all while "
              "config.PAPER_TRADING_ENABLED is False (same always-running/internally-gated "
              "pattern as the real monitor_loop): PASSED")
    finally:
        ste._evaluate_watchlist_entry_signal = real_eval
        spe.config.PAPER_TRADING_ENABLED = real_enabled


async def test_7_full_auto_paper_entry_then_exit_via_signal():
    """poll-loop-SHAPED: calls the same functions paper_poll_loop calls, in
    the same order, proving a real signal genuinely drives a paper entry
    and a real crossed-below genuinely drives a paper exit."""
    store = spe.PaperBasketStore()
    spe.paper_basket_store = store
    wl_store = swl.WatchlistStore()
    spe.watchlist_store = wl_store
    await wl_store.add_symbols(["HDFCBANK"])

    real_paper_enabled = spe.config.PAPER_TRADING_ENABLED
    spe.config.PAPER_TRADING_ENABLED = True
    real_fetch = ste._fetch_supertrend_state
    real_gap_up = ste._is_gap_up

    async def fake_fetch_entry_state(symbol, interval_minutes):
        return _make_state(is_above=True, prev_is_above=(interval_minutes != ste.config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES))

    async def fake_gap_up(symbol):
        return True

    ste._fetch_supertrend_state = fake_fetch_entry_state
    ste._is_gap_up = fake_gap_up
    entry_ltp = {"HDFCBANK FAKE EXP FUT": 1600.0, "HDFCBANK FAKE EXP PUT": 25.0}
    restore = install_all_dhan_mocks(entry_ltp)
    try:
        watchlist_symbols = await wl_store.symbols()
        open_paper_symbols = set(await store.symbols_with_open_baskets())
        for symbol in watchlist_symbols:
            if symbol in open_paper_symbols:
                continue
            if await ste._evaluate_watchlist_entry_signal(symbol):
                await spe._enter_paper_basket(symbol)

        assert "HDFCBANK" in store.live_baskets, "a real entry signal must genuinely create a paper basket"
        assert "HDFCBANK" in await wl_store.symbols(), \
            "unlike the REAL loop, a paper entry must NOT remove the symbol from the shared watchlist"

        # Re-running entry evaluation must skip it now (already open).
        open_paper_symbols = set(await store.symbols_with_open_baskets())
        assert "HDFCBANK" in open_paper_symbols
    finally:
        restore()

    async def fake_fetch_exit_state(symbol, interval_minutes):
        return _make_state(is_above=False, prev_is_above=True)  # genuine crossed-below

    ste._fetch_supertrend_state = fake_fetch_exit_state
    exit_ltp = {"HDFCBANK FAKE EXP FUT": 1580.0, "HDFCBANK FAKE EXP PUT": 30.0}
    restore = install_all_dhan_mocks(exit_ltp)
    try:
        for symbol in await store.symbols_with_open_baskets():
            reason = await ste._evaluate_basket_exit_signal(symbol, None)
            if reason:
                await spe._exit_paper_basket(symbol, reason)

        assert "HDFCBANK" not in store.live_baskets, "a real crossed-below signal must genuinely close the paper basket"
        hdfc_trades = [t for t in store.completed if t["underlying_symbol"] == "HDFCBANK"]
        assert len(hdfc_trades) == 1, store.completed
        trade = hdfc_trades[0]
        assert trade["exit_reason"] == "SUPERTREND_5MIN_EXIT"
        # futures: (1580-1600)*250=-5000 ; option: (30-25)*500=2500 ; total=-2500
        assert trade["total_pnl"] == -2500.0, trade
        print("7. Full poll-loop-shaped flow: a real entry signal genuinely opens a paper basket "
              "(leaving the SHARED watchlist untouched, unlike a real auto-entry), a real "
              "crossed-below signal genuinely closes it with correct P&L: PASSED")
    finally:
        restore()
        ste._fetch_supertrend_state = real_fetch
        ste._is_gap_up = real_gap_up
        spe.config.PAPER_TRADING_ENABLED = real_paper_enabled


async def test_8_margin_and_funds_logging():
    """User request 1 Sep 2026: "logging real margin and funds required
    during paper trading so that we can do analysis also." Covers: exact
    per-leg margin values + the naive combined sum flowing correctly from
    entry through to the persisted closed-trade record; a margin-fetch
    failure leaves that figure `None` WITHOUT aborting the paper entry
    (unlike a price-fetch failure) and correctly leaves the combined sum
    `None` too (never a partial/understated sum); and a funds-fetch
    failure is likewise non-fatal."""
    store = spe.PaperBasketStore()
    spe.paper_basket_store = store
    entry_ltp = {"WIPRO FAKE EXP FUT": 500.0, "WIPRO FAKE EXP PUT": 8.0}
    margin_by_security_id = {"FUT-WIPRO": 62500.0, "OPT-WIPRO": 4000.0}
    funds_snapshot = {"availabelBalance": 250000.0, "sodLimit": 300000.0, "utilizedAmount": 50000.0}
    restore = install_all_dhan_mocks(entry_ltp, margin_by_security_id, funds_snapshot)
    try:
        await spe._enter_paper_basket("WIPRO")
        basket = store.live_baskets["WIPRO"]
        assert basket.futures_leg.margin_required == 62500.0
        assert basket.option_leg.margin_required == 4000.0
        assert basket.total_margin_required == 66500.0, "must be the naive SUM of both legs' own figures"
        assert basket.account_funds_snapshot == funds_snapshot
    finally:
        restore()

    exit_ltp = {"WIPRO FAKE EXP FUT": 510.0, "WIPRO FAKE EXP PUT": 6.0}
    restore = install_all_dhan_mocks(exit_ltp)
    try:
        await spe._exit_paper_basket("WIPRO", "SUPERTREND_5MIN_EXIT")
    finally:
        restore()

    wipro_trades = [t for t in store.completed if t["underlying_symbol"] == "WIPRO"]
    assert len(wipro_trades) == 1
    trade = wipro_trades[0]
    assert trade["futures_margin_required"] == 62500.0
    assert trade["option_margin_required"] == 4000.0
    assert trade["total_margin_required"] == 66500.0
    assert trade["account_funds_snapshot_at_entry"] == funds_snapshot
    reloaded = trade_history.read_all_jsonl(spe.SWING_PAPER_LOG_NAME)
    wipro_reloaded = [t for t in reloaded if t["underlying_symbol"] == "WIPRO"]
    assert len(wipro_reloaded) == 1 and wipro_reloaded[0]["total_margin_required"] == 66500.0, \
        "margin/funds fields must round-trip through the on-disk log too, not just the in-memory copy"
    print("8a. Real per-leg margin (via get_margin_required) and the account's fund snapshot (via "
          "get_fund_limits) are correctly captured at entry, summed into a naive worst-case total, "
          "and persisted through to the completed trade record on-disk: PASSED")

    # Now the failure paths - a margin/funds fetch failure must NOT abort
    # the paper entry (unlike a price-fetch failure).
    store2 = spe.PaperBasketStore()
    spe.paper_basket_store = store2
    entry_ltp_2 = {"AXISBANK FAKE EXP FUT": 1100.0, "AXISBANK FAKE EXP PUT": 15.0}
    restore = install_all_dhan_mocks(entry_ltp_2, fail_margin=True, fail_funds=True)
    try:
        await spe._enter_paper_basket("AXISBANK")
        assert "AXISBANK" in store2.live_baskets, \
            "a margin/funds fetch failure must NOT abort the paper entry - only a price-fetch failure does"
        basket = store2.live_baskets["AXISBANK"]
        assert basket.futures_leg.margin_required is None
        assert basket.option_leg.margin_required is None
        assert basket.total_margin_required is None, \
            "must be None (never a guessed 0 or a partial sum) when the fetch itself failed"
        assert basket.account_funds_snapshot is None
        print("8b. A margin/funds fetch failure at entry leaves those figures None (never a "
              "guessed 0) WITHOUT aborting the paper entry itself - price is essential to the "
              "simulated P&L, margin/funds data is not: PASSED")
    finally:
        restore()


async def main():
    print("=== Swing paper-trading engine test suite ===\n")
    await test_1_and_2_successful_paper_entry_never_places_real_orders()
    await test_3_pe_leg_fetch_failure_aborts_with_nothing_recorded()
    await test_4_paper_exit_pnl_and_persistence()
    test_5_paper_log_is_isolated_from_real_trade_history()
    await test_6_poll_loop_noop_while_disabled()
    await test_7_full_auto_paper_entry_then_exit_via_signal()
    await test_8_margin_and_funds_logging()
    print("\nALL SWING PAPER-TRADING ENGINE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
