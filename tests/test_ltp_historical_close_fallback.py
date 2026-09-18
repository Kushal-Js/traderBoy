"""
Tests for _get_ltp's new historical-close fallback tier - added 18 Sep
2026 after a real incident: ABB 29 SEP 7200 CALL (Futures) had genuine
trading volume the whole time it was held (confirmed from its own 1-min
candles: hundreds to thousands of contracts trading most minutes), but
get_option_ltp's name-based get_ltp_data() lookup failed ~144 times
across the day ("No LTP returned for ABB 29 SEP 7200 CALL"). With no
fallback, _get_ltp simply raised on each failure, leaving MAX_LOSS_HIT
blind for minutes at a stretch - the position finally closed at a real
Rs 2737.50 loss against a configured Rs 2100 cap once a live LTP read
happened to succeed again.

The fix: when get_option_ltp fails, _get_ltp now tries
dhan_wrapper.get_last_historical_close (security_id-based, already
proven reliable during a live-quote outage - see that function's own
docstring for the ICICIPRULI incident it was originally built for)
before giving up. This keeps the regular poll-loop exit check
evaluating on SOME real price instead of going completely dark.

Covers, against the REAL production functions (not reimplemented):
  1. Options/Futures/Luxury _get_ltp all fall back to the historical
     close when get_option_ltp fails, and do NOT cache that fallback
     value via note_rest_ltp (it's a one-tick reading only).
  2. _get_ltp still raises (preserving the existing LTP-staleness
     escalation path) when BOTH get_option_ltp AND the historical-close
     fallback fail - a true, complete data blackout.
  3. Full integration (Futures): a real position whose live LTP is
     completely unavailable still gets a correct MAX_LOSS_HIT exit via
     _check_one_position, using the historical-close fallback price -
     this is the exact ABB scenario, now caught instead of running past
     the configured cap.

HOW TO RUN:
    uv run python tests/test_ltp_historical_close_fallback.py
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
import cross_strategy_registry

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_ltp_fallback_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.trading_engine as ote
import Options.position_store as ops
import Futures.trading_engine as fte
import Futures.position_store as fps
import Luxury.trading_engine as lte
import Luxury.position_store as lps
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=500, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Mocks every Dhan network call Futures' entry path touches - same
    shape as test_futures_broker_stop_loss.py's own helper, trimmed to
    what test_3 (the full-integration test) needs."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "get_pending_order_id": odc.dhan_wrapper.get_pending_order_id,
        "get_broker_net_quantity": odc.dhan_wrapper.get_broker_net_quantity,
        "cancel_order": odc.dhan_wrapper.cancel_order,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_bearish": odc.dhan_wrapper.get_cached_supertrend_bearish,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "get_cached_underlying_close": odc.dhan_wrapper.get_cached_underlying_close,
        "refresh_ema_cross_signal": odc.dhan_wrapper.refresh_ema_cross_signal,
        "get_cached_ema_cross_candle_start": odc.dhan_wrapper.get_cached_ema_cross_candle_start,
        "refresh_liquidity_signal": odc.dhan_wrapper.refresh_liquidity_signal,
        "get_cached_illiquid": odc.dhan_wrapper.get_cached_illiquid,
        "is_rsi_loss_reentry_blocked": odc.dhan_wrapper.is_rsi_loss_reentry_blocked,
        "get_cached_rsi": odc.dhan_wrapper.get_cached_rsi,
        "get_cached_prev_rsi": odc.dhan_wrapper.get_cached_prev_rsi,
        "rsi_loss_reentry_reason": odc.dhan_wrapper.rsi_loss_reentry_reason,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
        "get_last_historical_close": odc.dhan_wrapper.get_last_historical_close,
        "get_cached_option_ltp": odc.dhan_wrapper.get_cached_option_ltp,
        "note_rest_ltp": odc.dhan_wrapper.note_rest_ltp,
    }
    # Defensive - never let a stray real call reach dhan_wrapper.client
    # (real Dhan auth) from a unit test (see trading-skills' incidents/
    # 2026-09-08-test-suite-real-auth-leak.md).
    odc.dhan_wrapper.get_cached_option_ltp = lambda trading_symbol: None
    odc.dhan_wrapper.note_rest_ltp = lambda trading_symbol, ltp: None
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 10_000_000.0}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper.get_pending_order_id = lambda trading_symbol, transaction_type: None
    odc.dhan_wrapper.get_broker_net_quantity = lambda trading_symbol: 500
    odc.dhan_wrapper.cancel_order = lambda order_id: None
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_bearish = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.get_cached_underlying_close = lambda sym: None
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda sym: None
    odc.dhan_wrapper.refresh_liquidity_signal = lambda ts: None
    odc.dhan_wrapper.get_cached_illiquid = lambda ts: None
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda sym: False
    odc.dhan_wrapper.get_cached_rsi = lambda sym: None
    odc.dhan_wrapper.get_cached_prev_rsi = lambda sym: None
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda sym: None
    odc.dhan_wrapper.get_last_historical_close = lambda trading_symbol: None

    placed_orders = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}"
        placed_orders.append({"order_id": order_id, "trading_symbol": trading_symbol,
                               "transaction_type": transaction_type, "quantity": quantity})
        return {"order_id": order_id, "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        # BUY fills at a fixed, known entry price (147.35, the real ABB
        # entry price); SELL reports no fill_price of its own so
        # _exit_position's `result.fill_price or exit_price` falls back
        # to the exit_price it was called with - exactly what a real
        # market SELL's fill confirmation lag would look like, and lets
        # each test control the exit price via the ltp it passes in.
        order = next((o for o in placed_orders if o["order_id"] == order_id), None)
        if order and order["transaction_type"] == "SELL":
            return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=None,
                                filled_quantity=order["quantity"], is_amo=False)
        return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=147.35,
                            filled_quantity=500, is_amo=False)

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    odc.dhan_wrapper.wait_for_order_result = fake_wait_for_order_result

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore, placed_orders


async def _assert_falls_back(engine, label):
    """Shared body for tests 1a/1b/1c - one per package's own _get_ltp,
    since each package keeps its own copy of trading_engine.py."""
    real_cached = odc.dhan_wrapper.get_cached_option_ltp
    real_get_ltp = odc.dhan_wrapper.get_option_ltp
    real_historical = odc.dhan_wrapper.get_last_historical_close
    real_note = odc.dhan_wrapper.note_rest_ltp
    note_calls = []
    odc.dhan_wrapper.get_cached_option_ltp = lambda ts: None
    odc.dhan_wrapper.get_option_ltp = lambda ts: (_ for _ in ()).throw(
        ValueError(f"No LTP returned for {ts}")
    )
    odc.dhan_wrapper.get_last_historical_close = lambda ts: 125.45
    odc.dhan_wrapper.note_rest_ltp = lambda ts, ltp: note_calls.append((ts, ltp))
    try:
        result = await engine._get_ltp("ABB 29 SEP 7200 CALL")
        assert result == 125.45, f"{label}: expected the historical-close fallback value, got {result}"
        assert note_calls == [], \
            f"{label}: the fallback price must NOT be cached via note_rest_ltp, got {note_calls}"
        print(f"{label}: _get_ltp falls back to the historical close when get_option_ltp fails, "
              f"without caching it as a live LTP: PASSED")
    finally:
        odc.dhan_wrapper.get_cached_option_ltp = real_cached
        odc.dhan_wrapper.get_option_ltp = real_get_ltp
        odc.dhan_wrapper.get_last_historical_close = real_historical
        odc.dhan_wrapper.note_rest_ltp = real_note


async def test_1a_options_get_ltp_falls_back_to_historical_close():
    await _assert_falls_back(ote, "1a. Options")


async def test_1b_futures_get_ltp_falls_back_to_historical_close():
    await _assert_falls_back(fte, "1b. Futures")


async def test_1c_luxury_get_ltp_falls_back_to_historical_close():
    await _assert_falls_back(lte, "1c. Luxury")


async def test_2_get_ltp_still_raises_when_both_sources_fail():
    """A true, complete data blackout (both get_option_ltp AND the
    historical-close fallback fail) must still raise, so the existing
    LTP-staleness escalation (_handle_ltp_staleness, forces a market
    exit after config.LTP_STALE_FORCE_EXIT_MINUTES of CONTINUOUS
    failure) keeps working exactly as before this fix."""
    real_cached = odc.dhan_wrapper.get_cached_option_ltp
    real_get_ltp = odc.dhan_wrapper.get_option_ltp
    real_historical = odc.dhan_wrapper.get_last_historical_close
    odc.dhan_wrapper.get_cached_option_ltp = lambda ts: None
    odc.dhan_wrapper.get_option_ltp = lambda ts: (_ for _ in ()).throw(
        ValueError(f"No LTP returned for {ts}")
    )
    odc.dhan_wrapper.get_last_historical_close = lambda ts: None
    try:
        try:
            await fte._get_ltp("ABB 29 SEP 7200 CALL")
            assert False, "expected _get_ltp to raise when both the live LTP and the historical " \
                           "close fallback are unavailable"
        except ValueError as e:
            assert "No LTP returned" in str(e), str(e)
        print("2. _get_ltp still raises (preserving the existing LTP-staleness escalation path) when "
              "BOTH get_option_ltp AND the historical-close fallback fail: PASSED")
    finally:
        odc.dhan_wrapper.get_cached_option_ltp = real_cached
        odc.dhan_wrapper.get_option_ltp = real_get_ltp
        odc.dhan_wrapper.get_last_historical_close = real_historical


async def test_3_real_position_with_dead_live_ltp_still_hits_max_loss_via_fallback():
    """The actual ABB 29 SEP 7200 CALL scenario, end to end: a real
    position whose live LTP is completely unavailable (get_option_ltp
    always raises, exactly as it did ~144 times that day) must still
    correctly evaluate and fire MAX_LOSS_HIT via _check_one_position,
    using the historical-close fallback price - not go blind until the
    live feed happens to recover."""
    store = fps.PositionStore()
    fte.position_store = store
    real_max_loss_before = fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE
    real_cutoff = fte.config.RISK_THRESHOLD_CUTOFF_TIME
    real_broker_stop = fte.config.BROKER_STOP_LOSS_ENABLED
    fte.config.RISK_THRESHOLD_CUTOFF_TIME = "23:59"  # force "before cutoff" deterministically
    fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE = 2100.0  # matches the real deployed cap
    fte.config.BROKER_STOP_LOSS_ENABLED = False  # isolate the LTP-fallback path itself
    restore, placed_orders = install_all_dhan_mocks()
    try:
        entry = await fte._process_one_entry("ABB", "CE")
        assert entry["status"] == "entered", entry
        entry_price = entry["entry_price"]
        quantity = entry["quantity"]
        assert entry_price == 147.35, entry

        # The real incident: get_option_ltp fails every time (144 real
        # failures that day), but the option genuinely had volume the
        # whole time - the historical close correctly reflects the real,
        # sharply lower price.
        odc.dhan_wrapper.get_option_ltp = lambda ts: (_ for _ in ()).throw(
            ValueError(f"No LTP returned for {ts}")
        )
        fallback_price = 125.45  # the real exit price from the actual incident
        odc.dhan_wrapper.get_last_historical_close = lambda ts: fallback_price
        loss_rs = (entry_price - fallback_price) * quantity
        assert loss_rs > fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE, \
            f"test setup error: {loss_rs} must exceed the {fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE} cap"

        position = store.live_positions["ABB"]
        await fte._check_one_position("ABB", position)

        assert "ABB" not in store.live_positions, \
            "MAX_LOSS_HIT must fire using the historical-close fallback price, even with a fully dead live LTP"
        closed = store.closed_positions_today[0]
        assert closed.exit_reason == "MAX_LOSS_HIT", closed.exit_reason
        assert closed.exit_price == fallback_price, \
            f"expected the exit to use the fallback price {fallback_price}, got {closed.exit_price}"

        print("3. A real position with a completely dead live LTP still gets a correct MAX_LOSS_HIT exit "
              "via the historical-close fallback, instead of running the loss past its configured cap "
              "like the real ABB 29 SEP 7200 CALL incident did: PASSED")
    finally:
        restore()
        fte.config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_CE = real_max_loss_before
        fte.config.RISK_THRESHOLD_CUTOFF_TIME = real_cutoff
        fte.config.BROKER_STOP_LOSS_ENABLED = real_broker_stop


async def main():
    print("=== _get_ltp historical-close fallback test suite ===\n")
    await test_1a_options_get_ltp_falls_back_to_historical_close()
    await test_1b_futures_get_ltp_falls_back_to_historical_close()
    await test_1c_luxury_get_ltp_falls_back_to_historical_close()
    await test_2_get_ltp_still_raises_when_both_sources_fail()
    await test_3_real_position_with_dead_live_ltp_still_hits_max_loss_via_fallback()
    print("\nALL LTP HISTORICAL-CLOSE FALLBACK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
