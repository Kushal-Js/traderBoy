"""
Tests for the Nifty50 open gap-down / sharp-fall CE cool-off (added 11
Sep 2026, user request: "evaluate if Nifty50 has a Gap Down opening of
more than 100 points or is sharp falling when market open, then wait for
10 mins before placing any CE orders otherwise proceed as normal").

This is a single shared, market-wide computation - dhan_client.py's
DhanWrapper.evaluate_nifty_open_condition()/should_delay_ce_entry() -
consumed identically by Options/Futures/Luxury's own webhook handlers,
same pattern as refresh_supertrend_signal already being one shared
computation gated per-package by each package's own ENABLE_* flag. See
Options/config.py's "Nifty50 open gap-down / sharp-fall CE cool-off"
comment block for the full design rationale.

Covers:
  1. A >100-point gap-down at today's open alone triggers delay_ce.
  2. No big gap, but the price has already fallen >=0.3% from today's
     open by the time this is first checked, also triggers delay_ce
     (the "or is sharp falling" half of the user's request).
  3. A normal/flat/up day never triggers delay_ce.
  4. Evaluated ONCE per day and cached - a second call the same day with
     WORSE underlying data does not change the already-cached verdict
     (a one-shot judgment "at the open", not a running re-evaluation).
  5. should_delay_ce_entry() is True strictly before delay_until and
     False at/after it.
  6. A fetch failure fails OPEN (never blocks CE entries) and is not
     cached, so a later good fetch can still be judged.
  7. Against the REAL production webhook handler (Options.option_main):
     a CE alert during an active cool-off is ignored with
     reason="nifty_gap_down_ce_delay" and zero orders placed; a PE alert
     during the SAME cool-off proceeds completely normally (PE is never
     gated); ENABLE_GAP_DOWN_CE_DELAY=False cleanly bypasses the check
     even while the underlying condition would otherwise delay CE.

HOW TO RUN:
    uv run python tests/test_nifty_gap_down_ce_delay.py
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_nifty_gap_down_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.config as ocfg
import Options.position_store as ops
import Options.trading_engine as ote
import Options.option_main as om
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

IST = odc.IST
W = odc.dhan_wrapper
FUTURE_EXPIRY = date.today() + timedelta(days=25)


def _index_bars(day, opens_and_closes, interval_min=1):
    """[(open, close), ...] starting at 09:15 on `day`, `interval_min` apart."""
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    ts, op, cl = [], [], []
    for i, (o, c) in enumerate(opens_and_closes):
        ts.append(int((start + timedelta(minutes=interval_min * i)).timestamp()))
        op.append(float(o))
        cl.append(float(c))
    return ts, op, cl


def _install_nifty_series(yesterday_last_close: float, today_bars: list[tuple[float, float]]):
    """Wires W._client so fetch_continuous_intraday('13', 'IDX_I', 'INDEX', 1)
    returns one yesterday bar (closing at yesterday_last_close) followed by
    `today_bars` [(open, close), ...] for today. Returns a restore fn."""
    saved_client = W._client
    today = datetime.now(IST).date()
    yest = today - timedelta(days=1)
    yts, yop, ycl = _index_bars(yest, [(yesterday_last_close, yesterday_last_close)])
    tts, top, tcl = _index_bars(today, today_bars)
    merged = {"timestamp": yts + tts, "open": yop + top, "close": ycl + tcl}

    def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
        assert security_id == W.NIFTY_SECURITY_ID
        assert exchange_segment == "IDX_I" and instrument_type == "INDEX"
        return {"status": "success", "data": merged}

    W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))

    def restore():
        W._client = saved_client
    return restore


def _reset_cache():
    W._nifty_open_condition_cache = None


def test_1_gap_down_over_100_points_triggers_delay():
    _reset_cache()
    restore = _install_nifty_series(25000.0, [(24880.0, 24885.0)])  # gap = -120, no further fall
    try:
        result = W.evaluate_nifty_open_condition()
        assert result["evaluated"] is True, result
        assert result["gap_points"] == -120.0, result  # today_open (24880.0) - prev_close (25000.0)
        assert result["gap_down"] is True, result
        assert result["sharp_falling"] is False, result
        assert result["delay_ce"] is True, result
        assert result["delay_until"] is not None
        print("1. A >100-point gap-down at today's open alone triggers delay_ce: PASSED")
    finally:
        restore()
        _reset_cache()


def test_2_sharp_fall_without_a_big_gap_also_triggers_delay():
    _reset_cache()
    # Gap is only -20 (below the 100-point threshold) but price has already
    # fallen further to 24900 by the time this is checked: (24980-24900)/24980
    # = 0.32% >= the 0.3% GAP_DOWN_SHARP_FALL_PCT default.
    restore = _install_nifty_series(25000.0, [(24980.0, 24980.0), (24980.0, 24940.0), (24980.0, 24900.0)])
    try:
        result = W.evaluate_nifty_open_condition()
        assert result["gap_down"] is False, result
        assert result["sharp_falling"] is True, result
        assert result["delay_ce"] is True, result
        print("2. No big gap but already falling >=0.3% from today's open also triggers delay_ce: PASSED")
    finally:
        restore()
        _reset_cache()


def test_3_normal_day_never_triggers_delay():
    _reset_cache()
    restore = _install_nifty_series(25000.0, [(25010.0, 25010.0), (25010.0, 25005.0), (25010.0, 25000.0)])
    try:
        result = W.evaluate_nifty_open_condition()
        assert result["gap_down"] is False, result
        assert result["sharp_falling"] is False, result
        assert result["delay_ce"] is False, result
        assert result["delay_until"] is None, result
        assert W.should_delay_ce_entry() is False
        print("3. A normal/flat/up day never triggers delay_ce: PASSED")
    finally:
        restore()
        _reset_cache()


def test_4_evaluated_once_per_day_and_cached():
    _reset_cache()
    restore1 = _install_nifty_series(25000.0, [(24880.0, 24880.0)])  # big gap-down
    try:
        first = W.evaluate_nifty_open_condition()
        assert first["delay_ce"] is True
    finally:
        restore1()

    # Same day, now wire a completely different (flat, no-delay) series -
    # the cached verdict from the FIRST check this day must NOT change.
    restore2 = _install_nifty_series(25000.0, [(25010.0, 25010.0)])
    try:
        second = W.evaluate_nifty_open_condition()
        assert second is first or second["gap_points"] == first["gap_points"], \
            "a later call the same day must return the cached (first) verdict, not recompute"
        assert second["delay_ce"] is True, "cached verdict must still say delay_ce (from the FIRST check)"
        print("4. Evaluated once per day and cached - a later call with different data doesn't change it: PASSED")
    finally:
        restore2()
        _reset_cache()


def test_5_should_delay_ce_entry_respects_the_delay_window():
    _reset_cache()
    restore = _install_nifty_series(25000.0, [(24880.0, 24880.0)])
    try:
        result = W.evaluate_nifty_open_condition()
        delay_until = result["delay_until"]
        just_before = delay_until - timedelta(seconds=1)
        just_after = delay_until + timedelta(seconds=1)
        assert W.should_delay_ce_entry(now=just_before) is True
        assert W.should_delay_ce_entry(now=just_after) is False
        print("5. should_delay_ce_entry() is True strictly before delay_until, False at/after it: PASSED")
    finally:
        restore()
        _reset_cache()


def test_6_fetch_failure_fails_open_and_is_not_cached():
    _reset_cache()
    saved_client = W._client

    def boom(**kw):
        raise RuntimeError("DH-904 rate limit")

    W._client = types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=boom))
    try:
        result = W.evaluate_nifty_open_condition()
        assert result["evaluated"] is False, result
        assert result["delay_ce"] is False, result
        assert W.should_delay_ce_entry() is False
        assert W._nifty_open_condition_cache is None, "a failed evaluation must not be cached"
    finally:
        W._client = saved_client

    # A subsequent good fetch must still be judged normally (not stuck
    # permanently un-evaluated from the earlier failure).
    restore = _install_nifty_series(25000.0, [(24880.0, 24880.0)])
    try:
        result = W.evaluate_nifty_open_condition()
        assert result["evaluated"] is True and result["delay_ce"] is True, result
        print("6. A fetch failure fails open and isn't cached - a later good fetch is judged normally: PASSED")
    finally:
        restore()
        _reset_cache()


# --------------------------------------------------------------------------- #
# Integration: the REAL Options webhook handler
# --------------------------------------------------------------------------- #
def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL" if option_type == "CE" else f"{symbol} FAKE EXP PUT",
                      strike=1000.0, option_type=option_type, lot_size=500,
                      security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Same approach as test_deep_integration.py's own helper - the real
    dhan_wrapper singleton, network boundary only."""
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 100000.0}
    odc.dhan_wrapper.place_market_order = lambda trading_symbol, quantity, transaction_type, tag=None, product_type=None: {
        "order_id": f"FAKE-{trading_symbol}-{transaction_type}", "is_amo": False}
    odc.dhan_wrapper.place_stop_loss_limit_order = lambda trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SLL-{trading_symbol}"}
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)
    return restore


def fake_ranked(stocks, top_n, prefer_highest):
    return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]


async def test_7_real_ce_alert_ignored_during_cooloff_pe_unaffected_flag_bypasses():
    """Wiring test for the webhook handler itself - does it call and honor
    should_delay_ce_entry() for CE only, gated by ENABLE_GAP_DOWN_CE_DELAY?
    The gap/sharp-fall MATH is already fully covered by tests 1-6 above, so
    here should_delay_ce_entry/evaluate_nifty_open_condition are mocked
    directly to a fixed verdict, rather than driving them indirectly
    through fetch_continuous_intraday + real wall-clock time (which would
    make this test's outcome depend on what time of day it happens to run,
    since should_delay_ce_entry()'s delay window is anchored to real
    MARKET_OPEN_TIME, not a frozen clock)."""
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 5
    ote.config.MAX_LIVE_POSITIONS_PE = 5

    real_flag = ocfg.ENABLE_GAP_DOWN_CE_DELAY
    ocfg.ENABLE_GAP_DOWN_CE_DELAY = True
    real_rank = om.rank_and_pick_top_stocks
    om.rank_and_pick_top_stocks = fake_ranked
    real_should_delay = odc.dhan_wrapper.should_delay_ce_entry
    real_evaluate = odc.dhan_wrapper.evaluate_nifty_open_condition
    fake_condition = {
        "date": datetime.now(IST).date(), "evaluated": True,
        "prev_close": 25000.0, "today_open": 24880.0, "latest_close": 24880.0,
        "gap_points": -120.0, "gap_down": True, "fall_pct": 0.0, "sharp_falling": False,
        "delay_ce": True, "delay_until": datetime.now(IST).replace(hour=9, minute=25, second=0, microsecond=0),
    }
    odc.dhan_wrapper.should_delay_ce_entry = lambda: True  # cool-off active
    odc.dhan_wrapper.evaluate_nifty_open_condition = lambda: fake_condition
    restore_mocks = install_all_dhan_mocks()
    order_calls = []
    real_place = odc.dhan_wrapper.place_market_order
    odc.dhan_wrapper.place_market_order = lambda *a, **k: order_calls.append((a, k)) or {
        "order_id": "SHOULD-NOT-HAPPEN", "is_amo": False}
    try:
        ce_payload = om.ChartinkWebhookPayload(
            stocks="RELIANCE", trigger_prices="1", triggered_at="9:16 am",
            scan_name="gap-down-test-ce", scan_url="gap-down-test-ce",
            alert_name="CE during cool-off",
        )
        result = await om._handle_chartink_webhook(ce_payload, "CE", True)
        assert result["status"] == "ignored", result
        assert result["reason"] == "nifty_gap_down_ce_delay", result
        assert order_calls == [], f"no order should be placed while the cool-off is active, got {order_calls}"
        assert store.live_positions == {}, "zero CE positions should have opened"

        await asyncio.sleep(0.3)
        alerts = trade_history.read_all_webhook_alerts("Options")
        matches = [a for a in alerts if a["reason"] == "nifty_gap_down_ce_delay"]
        assert len(matches) == 1 and matches[0]["status"] == "ignored", alerts
        print("7a. A real CE alert during an active Nifty gap-down cool-off is ignored with "
              "reason='nifty_gap_down_ce_delay', zero orders placed, durably logged: PASSED")

        # PE must be completely unaffected by the very same active cool-off.
        pe_payload = om.ChartinkWebhookPayload(
            stocks="TCS", trigger_prices="1", triggered_at="9:16 am",
            scan_name="gap-down-test-pe", scan_url="gap-down-test-pe",
            alert_name="PE during cool-off",
        )
        result = await om._handle_chartink_webhook(pe_payload, "PE", False)
        assert result["status"] == "processed", result
        assert result["entries"][0]["status"] == "entered", result["entries"]
        assert "TCS" in store.live_positions
        print("7b. A real PE alert during the SAME active cool-off proceeds completely normally "
              "(PE is never gated by this): PASSED")

        # Flag off must bypass the check even with the cool-off still active.
        ocfg.ENABLE_GAP_DOWN_CE_DELAY = False
        ce_payload_2 = om.ChartinkWebhookPayload(
            stocks="SBIN", trigger_prices="1", triggered_at="9:16 am",
            scan_name="gap-down-test-ce-2", scan_url="gap-down-test-ce-2",
            alert_name="CE with the flag off",
        )
        result = await om._handle_chartink_webhook(ce_payload_2, "CE", True)
        assert result["status"] == "processed", result
        assert result["entries"][0]["status"] == "entered", result["entries"]
        assert "SBIN" in store.live_positions
        print("7c. ENABLE_GAP_DOWN_CE_DELAY=False cleanly bypasses the check even while the "
              "underlying condition would otherwise delay CE: PASSED")
    finally:
        odc.dhan_wrapper.place_market_order = real_place
        restore_mocks()
        odc.dhan_wrapper.should_delay_ce_entry = real_should_delay
        odc.dhan_wrapper.evaluate_nifty_open_condition = real_evaluate
        om.rank_and_pick_top_stocks = real_rank
        ocfg.ENABLE_GAP_DOWN_CE_DELAY = real_flag


async def main():
    print("=== Nifty50 open gap-down / sharp-fall CE cool-off test suite ===\n")
    test_1_gap_down_over_100_points_triggers_delay()
    test_2_sharp_fall_without_a_big_gap_also_triggers_delay()
    test_3_normal_day_never_triggers_delay()
    test_4_evaluated_once_per_day_and_cached()
    test_5_should_delay_ce_entry_respects_the_delay_window()
    test_6_fetch_failure_fails_open_and_is_not_cached()
    await test_7_real_ce_alert_ignored_during_cooloff_pe_unaffected_flag_bypasses()
    print("\nALL NIFTY GAP-DOWN CE-DELAY CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
