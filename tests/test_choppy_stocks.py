"""
Tests for choppy_stocks.py's own standalone module functions - user
request 31 Aug 2026. Originally built as an automatic weekly lot-size
scan, then simplified same-day to a manually-maintained, fixed list
(IDEA, YESBANK, SAGILITY) with no auto-refresh - see NOTES.md entries
#55/#56 and choppy_stocks.py's own module docstring for the full
history. This file tests the CURRENT (manual) design.

**No longer wired into the Options webhook handler** (user request 10
Sep 2026: "remove Choppy-stocks pre-filter from Options... make Options
have similar rule set and guard rails as Luxury have" - Luxury never
had this filter either). The module itself is kept, not deleted, per
this repo's own convention - test 4 below now explicitly proves the
removal (a choppy-listed stock enters normally through the real
webhook), replacing the old test that proved the opposite.

Covers, against the REAL production functions (not reimplemented):
  1. ensure_choppy_list_exists() seeds DEFAULT_CHOPPY_STOCKS when no file
     exists yet, and never overwrites an existing (possibly
     hand-edited) one.
  2. write_choppy_list / read_choppy_list round-trip (normalizes to
     uppercase, dedups, sorts) through real disk I/O.
  3. is_choppy() fails open (excludes nothing) when the file is missing,
     and picks up a change to the file on the very next call - no
     restart/reload step needed, since it deliberately reads fresh each
     time (see its own docstring for why that's safe here).
  4. The real webhook handler (_handle_chartink_webhook) no longer
     excludes anything on the choppy list - a choppy-listed stock now
     reaches ranking and enters normally, same as any other symbol.
     Only the Dhan network boundary is mocked (same approach as
     test_deep_integration.py) - no live Dhan session needed.

HOW TO RUN:
    uv run python tests/test_choppy_stocks.py
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

import choppy_stocks
import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_choppy_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
import Options.option_main as om
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


_scratch_choppy_counter = 0


def _use_scratch_choppy_paths():
    """Points choppy_stocks at a fresh scratch file for one test, returns
    a restore() closure. Never touches the real choppy/ folder. Uses a
    monotonic counter (not id(object()), which CPython can and does
    reuse once the temporary object is collected) so two calls never
    collide on the same scratch directory."""
    global _scratch_choppy_counter
    _scratch_choppy_counter += 1
    real_dir, real_file = choppy_stocks.CHOPPY_DIR, choppy_stocks.CHOPPY_FILE
    choppy_stocks.CHOPPY_DIR = scratch_dir / f"choppy_{_scratch_choppy_counter}"
    choppy_stocks.CHOPPY_FILE = choppy_stocks.CHOPPY_DIR / "choppy_stocks.json"

    def restore():
        choppy_stocks.CHOPPY_DIR, choppy_stocks.CHOPPY_FILE = real_dir, real_file

    return restore


def test_1_ensure_choppy_list_exists_seeds_default_but_never_overwrites():
    restore = _use_scratch_choppy_paths()
    try:
        assert choppy_stocks.read_choppy_list() is None, "expected no file yet"
        choppy_stocks.ensure_choppy_list_exists()
        data = choppy_stocks.read_choppy_list()
        assert data == {"stocks": sorted(choppy_stocks.DEFAULT_CHOPPY_STOCKS)}, data

        # A human has since hand-edited the file - ensure_choppy_list_exists
        # must NOT stomp on that.
        choppy_stocks.write_choppy_list(["RELIANCE", "TCS"])
        choppy_stocks.ensure_choppy_list_exists()
        data_after = choppy_stocks.read_choppy_list()
        assert data_after == {"stocks": ["RELIANCE", "TCS"]}, \
            f"ensure_choppy_list_exists must never overwrite an existing file, got {data_after}"
        print("1. ensure_choppy_list_exists seeds DEFAULT_CHOPPY_STOCKS when missing, "
              "and never overwrites an existing (hand-edited) file: PASSED")
    finally:
        restore()


def test_2_write_and_read_round_trip_normalizes():
    restore = _use_scratch_choppy_paths()
    try:
        written = choppy_stocks.write_choppy_list(["idea", "YesBank", "yesbank", " sagility ", ""])
        assert written == {"stocks": ["IDEA", "SAGILITY", "YESBANK"]}, written
        read_back = choppy_stocks.read_choppy_list()
        assert read_back == written, "read-back must exactly match what was written"
        print("2. write_choppy_list/read_choppy_list round-trip: uppercased, deduped, "
              "sorted, blanks dropped: PASSED")
    finally:
        restore()


def test_3_is_choppy_fails_open_and_picks_up_edits_live():
    restore = _use_scratch_choppy_paths()
    try:
        assert not choppy_stocks.is_choppy("IDEA"), "must fail open (exclude nothing) when no file exists"

        choppy_stocks.write_choppy_list(["IDEA", "YESBANK", "SAGILITY"])
        assert choppy_stocks.is_choppy("IDEA")
        assert choppy_stocks.is_choppy("idea"), "must be case-insensitive"
        assert not choppy_stocks.is_choppy("RELIANCE")

        # Simulates a manual hand-edit while the process keeps running -
        # must take effect on the very next call, no reload step needed.
        choppy_stocks.write_choppy_list(["RELIANCE"])
        assert choppy_stocks.is_choppy("RELIANCE"), "a manual edit must take effect immediately, no restart"
        assert not choppy_stocks.is_choppy("IDEA"), "the old entry must no longer be excluded after the edit"

        print("3. is_choppy fails open with no file, is case-insensitive, and picks up a "
              "manual edit on the very next call with no restart/reload needed: PASSED")
    finally:
        restore()


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP CALL", strike=1000.0, option_type=option_type,
                      lot_size=500, security_id=f"SECID-{symbol}", expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    originals = {
        "get_atm_option": odc.dhan_wrapper.get_atm_option,
        "get_option_ltp": odc.dhan_wrapper.get_option_ltp,
        "get_margin_required": odc.dhan_wrapper.get_margin_required,
        "get_fund_limits": odc.dhan_wrapper.get_fund_limits,
        "has_open_position_for_underlying": odc.dhan_wrapper.has_open_position_for_underlying,
        "_get_open_fno_positions_once": odc.dhan_wrapper._get_open_fno_positions_once,
        "subscribe_option_price": odc.dhan_wrapper.subscribe_option_price,
        "unsubscribe_option_price": odc.dhan_wrapper.unsubscribe_option_price,
        "refresh_supertrend_signal": odc.dhan_wrapper.refresh_supertrend_signal,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "refresh_ema_cross_signal": odc.dhan_wrapper.refresh_ema_cross_signal,
        "get_cached_ema_cross_candle_start": odc.dhan_wrapper.get_cached_ema_cross_candle_start,
        "is_rsi_loss_reentry_blocked": odc.dhan_wrapper.is_rsi_loss_reentry_blocked,
        "get_cached_rsi": odc.dhan_wrapper.get_cached_rsi,
        "get_cached_prev_rsi": odc.dhan_wrapper.get_cached_prev_rsi,
        "rsi_loss_reentry_reason": odc.dhan_wrapper.rsi_loss_reentry_reason,
        "should_delay_ce_entry": odc.dhan_wrapper.should_delay_ce_entry,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    # Found + fixed 8 Sep 2026 - these 4 were MISSING here (unlike every
    # other test file's own equivalent helper), so the proactive funds
    # check and the already-open-at-broker check both fell through to
    # REAL Dhan network calls (and REAL, slow, repeated PIN+TOTP
    # authentication attempts via _retry) every time this file ran -
    # confirmed live: this broke the droplet's own real session token
    # (the same "running local scripts against the same account can
    # invalidate the live process's own token" mechanism as NOTES.md's
    # MAHABANK phantom-exit incident), discovered because a full test
    # suite run happened to immediately precede a real /funds/buckets
    # Internal Server Error on the live droplet.
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 100000.0}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda sym: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda sym: None
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda sym: False
    odc.dhan_wrapper.get_cached_rsi = lambda sym: None
    odc.dhan_wrapper.get_cached_prev_rsi = lambda sym: None
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda sym: None
    odc.dhan_wrapper.should_delay_ce_entry = lambda: False
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


async def test_4_webhook_handler_no_longer_excludes_choppy_stocks():
    """Proves the removal (10 Sep 2026) rather than just asserting
    silence - a choppy-listed stock must reach ranking and enter
    normally now, exactly like any other symbol, matching Luxury's own
    webhook (which never had this filter)."""
    store = ops.PositionStore()
    om.position_store = store
    ote.position_store = store
    ote.config.MAX_LIVE_POSITIONS_CE = 10

    real_rank = om.rank_and_pick_top_stocks

    def fake_ranked(stocks, top_n, prefer_highest):
        return [(s, float(i)) for i, s in enumerate(stocks[:top_n if top_n > 0 else len(stocks)])]

    om.rank_and_pick_top_stocks = fake_ranked
    restore_choppy_paths = _use_scratch_choppy_paths()
    choppy_stocks.write_choppy_list(["IDEA"])
    restore_mocks = install_all_dhan_mocks()
    try:
        payload = om.ChartinkWebhookPayload(
            stocks="RELIANCE,IDEA,TCS", trigger_prices="1,1,1", triggered_at="9:20 am",
            scan_name="choppy-test", scan_url="choppy-test",
            alert_name="Choppy filter removed test",
        )
        result = await om._handle_chartink_webhook(payload, "CE", True)

        assert result["status"] == "processed", result
        assert "choppy_stocks_excluded" not in result, \
            f"the response must no longer carry this key at all now that the filter is gone, got {result.keys()}"
        ranked_symbols = [s for s, _pct in result["ranked_by_day_change_pct"]]
        assert "IDEA" in ranked_symbols, \
            f"IDEA is on the choppy list but must reach ranking now that the filter is removed, got {ranked_symbols}"
        assert set(ranked_symbols) == {"RELIANCE", "IDEA", "TCS"}, ranked_symbols

        entered = [e["symbol"] for e in result["entries"] if e["status"] == "entered"]
        assert set(entered) == {"RELIANCE", "IDEA", "TCS"}, \
            f"all three stocks (choppy-listed or not) should enter normally, got {entered}"
        assert "IDEA" in store.live_positions

        print("4. Real webhook handler no longer excludes anything on the choppy list - IDEA "
              "(choppy-listed) reaches ranking and enters normally, same as RELIANCE/TCS: PASSED")
    finally:
        restore_mocks()
        restore_choppy_paths()
        om.rank_and_pick_top_stocks = real_rank


async def main():
    print("=== choppy_stocks.py test suite (manually-maintained list design) ===\n")
    test_1_ensure_choppy_list_exists_seeds_default_but_never_overwrites()
    test_2_write_and_read_round_trip_normalizes()
    test_3_is_choppy_fails_open_and_picks_up_edits_live()
    await test_4_webhook_handler_no_longer_excludes_choppy_stocks()
    print("\nALL CHOPPY-STOCKS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
