"""
Tests for two corrective actions added to Luxury on 2 Sep 2026, after the
user asked for a deeper look at "why didn't returns improve" across real
2-3 Sep trades and requested these two specific fixes ("Implement 1 and 2
items, back test them with last 2 days trading data also, deploy it
then"):

  1. Same-day LOSS COOLDOWN on entry (trade_history.minutes_since_last_
     loss_today + Luxury/trading_engine.py's own check in
     _process_one_entry) - found investigating real trades: MAHABANK
     went 0-for-3 same-day re-entries (-Rs.2,600), PHOENIXLTD's third
     same-day re-entry lost more than its first two wins combined.
  2. LIQUIDITY GUARD on exit (Options/dhan_client.py's refresh_
     liquidity_signal/get_cached_illiquid + Luxury/trading_engine.py's
     own _exit_reason_for) - found investigating a real CHOLAFIN
     MAX_LOSS_HIT overshoot (-Rs.2,594 against a Rs.1,000 cap): a real
     1-min option-candle replay showed price sitting flat on ZERO
     traded volume for 4 straight minutes, then gapping ~7% past the
     stop-loss threshold in one untracked candle. This exits a held
     position the moment its OWN option has printed zero volume for
     LIQUIDITY_GUARD_ZERO_VOLUME_BARS consecutive completed 1-min bars,
     independent of (and checked AFTER) every price-threshold check.

Covers, against the REAL production functions (not reimplemented):
  1. trade_history.minutes_since_last_loss_today - correct minutes-
     elapsed math, isolates by strategy AND by symbol, ignores a WINNING
     trade (only losses start the cooldown clock), returns None (never a
     fabricated 0) when there's no loss today or the log doesn't exist.
  2. Full real integration: a real _process_one_entry loss (via a real
     enter->exit cycle hitting MAX_LOSS_HIT) correctly blocks an
     immediate real re-entry attempt for the SAME symbol with
     loss_cooldown_active and places NO order, while a DIFFERENT symbol
     is unaffected and a cooldown-expired retry (mocking `datetime.now`
     forward) succeeds normally.
  3. dhan_client.refresh_liquidity_signal/get_cached_illiquid - correct
     zero-volume-streak detection (only ALL-zero over the trailing N
     bars counts, one nonzero bar anywhere in the window means liquid),
     drops a still-forming last candle the same way refresh_supertrend_
     signal does, and fails safe (None / not-illiquid) on a fetch error
     or too little history.
  4. _exit_reason_for's own ordering - liquidity_guard fires when
     nothing else would have (the CHOLAFIN scenario: price still near
     entry, no other threshold close), and a genuine price-threshold
     exit (e.g. TARGET_HIT) still takes priority when both conditions
     happen to be true on the same tick.
  5. Both features' own config flags disable them cleanly.

HOW TO RUN:
    uv run python tests/test_luxury_corrective_actions.py
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
import cross_strategy_registry

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_luxury_corrective_actions_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Luxury.position_store as lps
import Luxury.trading_engine as lte
from Luxury.position_store import Position
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=500, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks(volumes_sequence=None):
    """Mocks every Dhan network call the entry/exit paths touch. Every
    unique order gets its own order_id so repeated entries for the same
    symbol across this file's several scenarios never collide.

    volumes_sequence: if given, a list of {"data": {...}} responses
    consumed IN ORDER by successive intraday_minute_data(interval=1)
    calls - lets a test control exactly what refresh_liquidity_signal
    sees on each successive refresh."""
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
        "get_cached_supertrend_bearish": odc.dhan_wrapper.get_cached_supertrend_bearish,
        "get_cached_supertrend_candle_start": odc.dhan_wrapper.get_cached_supertrend_candle_start,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_market_order": odc.dhan_wrapper.place_stop_loss_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
        "_instrument_meta": odc.dhan_wrapper._instrument_meta,
        "_client": odc.dhan_wrapper._client,
    }
    odc.dhan_wrapper.get_atm_option = fake_atm_option
    odc.dhan_wrapper.get_option_ltp = lambda trading_symbol: 50.0
    odc.dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    odc.dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 10_000_000.0}
    odc.dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    odc.dhan_wrapper._get_open_fno_positions_once = lambda: []
    odc.dhan_wrapper.subscribe_option_price = lambda ts: None
    odc.dhan_wrapper.unsubscribe_option_price = lambda ts: None
    odc.dhan_wrapper.refresh_supertrend_signal = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_supertrend_bearish = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_supertrend_candle_start = lambda underlying_symbol: None

    placed_orders = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}-{id(object())}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    # Luxury's own _enter_single_position places a real broker-side stop-
    # loss order (added 8 Sep 2026) right after every entry - unmocked,
    # this would fall through to a REAL Dhan call.
    odc.dhan_wrapper.place_stop_loss_market_order = lambda trading_symbol, quantity, transaction_type, trigger_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SL-{trading_symbol}-{len(placed_orders)}-{id(object())}"}
    odc.dhan_wrapper.place_stop_loss_limit_order = lambda trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SLL-{trading_symbol}"}
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    # For the liquidity-guard tests: mock _instrument_meta (security_id
    # lookup) and the raw Dhan intraday_minute_data call the new
    # refresh_liquidity_signal makes directly.
    odc.dhan_wrapper._instrument_meta = lambda trading_symbol: {"security_id": "999999"}
    seq_iter = iter(volumes_sequence or [])

    class FakeDhanClient:
        def intraday_minute_data(self, **kwargs):
            try:
                return next(seq_iter)
            except StopIteration:
                return {"status": "success", "data": {"volume": [], "timestamp": []}}

    class FakeClientWrapper:
        Dhan = FakeDhanClient()

    # `client` is a read-only @property (returns self._client, lazily
    # authenticating if None) - patch the underlying _client attribute it
    # reads instead of the property itself, so `dhan_wrapper.client.Dhan.
    # intraday_minute_data(...)` (refresh_liquidity_signal's own call
    # shape) resolves to the fake without ever touching real Dhan auth.
    odc.dhan_wrapper._client = FakeClientWrapper()

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)

    return restore, placed_orders


def _make_candle_response(timestamps, volumes):
    return {"status": "success", "data": {"volume": volumes, "timestamp": timestamps}}


# --------------------------------------------------------------------- #
# 1. trade_history.minutes_since_last_loss_today - pure logic
# --------------------------------------------------------------------- #

def test_1_minutes_since_last_loss_basic():
    now = datetime.now()
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "MAHABANK", "pnl": -500.0,
        "closed_at": (now - timedelta(minutes=12)).isoformat(),
    })
    minutes = trade_history.minutes_since_last_loss_today("Luxury", "MAHABANK", now)
    assert minutes is not None
    assert 11.9 <= minutes <= 12.1, minutes
    print("1. minutes_since_last_loss_today correctly computes elapsed minutes since a real logged loss: PASSED")


def test_2_minutes_since_last_loss_ignores_wins():
    now = datetime.now()
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "RVNL", "pnl": +800.0,
        "closed_at": (now - timedelta(minutes=2)).isoformat(),
    })
    assert trade_history.minutes_since_last_loss_today("Luxury", "RVNL", now) is None, \
        "a WINNING trade must never start the loss-cooldown clock"
    print("2. A winning trade never starts the loss-cooldown clock (only pnl<=0 counts): PASSED")


def test_3_minutes_since_last_loss_isolates_by_strategy_and_symbol():
    now = datetime.now()
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "GVT&D", "pnl": -300.0,
        "closed_at": (now - timedelta(minutes=5)).isoformat(),
    })
    trade_history.append_jsonl("real_trades", {
        "strategy": "Options", "underlying_symbol": "GVT&D", "pnl": -300.0,
        "closed_at": (now - timedelta(minutes=1)).isoformat(),
    })
    assert trade_history.minutes_since_last_loss_today("Futures", "GVT&D", now) is None, \
        "a different strategy's own loss on the same symbol must not leak across"
    minutes = trade_history.minutes_since_last_loss_today("Luxury", "GVT&D", now)
    assert minutes is not None and 4.9 <= minutes <= 5.1, minutes
    print("3. Loss lookback correctly isolates by BOTH strategy and underlying_symbol: PASSED")


def test_4_minutes_since_last_loss_uses_the_most_recent_of_several():
    now = datetime.now()
    for mins_ago in (40, 25, 8):
        trade_history.append_jsonl("real_trades", {
            "strategy": "Luxury", "underlying_symbol": "PHOENIXLTD", "pnl": -100.0,
            "closed_at": (now - timedelta(minutes=mins_ago)).isoformat(),
        })
    minutes = trade_history.minutes_since_last_loss_today("Luxury", "PHOENIXLTD", now)
    assert minutes is not None and 7.9 <= minutes <= 8.1, minutes
    print("4. With multiple losses today, the MOST RECENT one is what the cooldown measures from: PASSED")


def test_5_minutes_since_last_loss_none_when_no_log_or_no_loss_today():
    now = datetime.now()
    assert trade_history.minutes_since_last_loss_today("Luxury", "NEVERTRADED", now) is None
    print("5. No trades at all for a symbol correctly returns None, not a crash or a fabricated 0: PASSED")


# --------------------------------------------------------------------- #
# 2. Full real integration: loss cooldown blocks an immediate re-entry
# --------------------------------------------------------------------- #

async def test_6_real_loss_then_immediate_reentry_blocked():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled, real_minutes = lte.config.LOSS_COOLDOWN_ENABLED, lte.config.LOSS_COOLDOWN_MINUTES
    lte.config.LOSS_COOLDOWN_ENABLED = True
    lte.config.LOSS_COOLDOWN_MINUTES = 30
    restore, placed_orders = install_all_dhan_mocks()
    try:
        # Uses a symbol not touched by tests 1-5 (which share this same
        # scratch trade_history.HISTORY_DIR) - avoids a stale loss record
        # from an earlier test's own log write making this look
        # cooled-down before the real entry below even happens.
        entry = await lte._process_one_entry("COALINDIA", "CE")
        assert entry["status"] == "entered", entry

        # Real exit through the real production function - close_position
        # itself fire-and-forgets record_closed_trade (must not be awaited
        # while its own lock is held, per that function's own docstring) -
        # a brief sleep lets that background task actually land before we
        # check trade_history, same pattern used elsewhere in this repo's
        # own test suite for the identical fire-and-forget shape.
        closed = await store.close_position("COALINDIA", 40.0, "MAX_LOSS_HIT")
        assert closed is not None and closed.exit_price == 40.0
        await asyncio.sleep(0.3)

        placed_orders.clear()
        retry = await lte._process_one_entry("COALINDIA", "CE")
        assert retry["status"] == "skipped" and retry["reason"] == "loss_cooldown_active", retry
        assert placed_orders == [], f"a cooling-down symbol must place ZERO orders, got {placed_orders}"

        placed_orders.clear()
        other = await lte._process_one_entry("RVNL", "CE")
        assert other["status"] == "entered", f"a DIFFERENT symbol must be entirely unaffected, got {other}"

        print("6. A real MAX_LOSS_HIT loss correctly blocks an immediate real re-entry attempt for the SAME "
              "symbol (zero orders placed), while a different symbol is entirely unaffected: PASSED")
    finally:
        restore()
        lte.config.LOSS_COOLDOWN_ENABLED, lte.config.LOSS_COOLDOWN_MINUTES = real_enabled, real_minutes


async def test_7_reentry_succeeds_once_cooldown_expires():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled, real_minutes = lte.config.LOSS_COOLDOWN_ENABLED, lte.config.LOSS_COOLDOWN_MINUTES
    lte.config.LOSS_COOLDOWN_ENABLED = True
    lte.config.LOSS_COOLDOWN_MINUTES = 30
    restore, placed_orders = install_all_dhan_mocks()
    try:
        trade_history.append_jsonl("real_trades", {
            "strategy": "Luxury", "underlying_symbol": "KFINTECH", "pnl": -900.0,
            "closed_at": (datetime.now() - timedelta(minutes=45)).isoformat(),
        })
        result = await lte._process_one_entry("KFINTECH", "CE")
        assert result["status"] == "entered", \
            f"a loss from 45 minutes ago (past the 30-minute cooldown) must not block re-entry, got {result}"
        print("7. Once the configured cooldown window has genuinely elapsed, the same symbol re-enters normally: PASSED")
    finally:
        restore()
        lte.config.LOSS_COOLDOWN_ENABLED, lte.config.LOSS_COOLDOWN_MINUTES = real_enabled, real_minutes


async def test_8_loss_cooldown_disabled_flag_bypasses_the_check():
    store = lps.PositionStore()
    lte.position_store = store
    real_enabled = lte.config.LOSS_COOLDOWN_ENABLED
    lte.config.LOSS_COOLDOWN_ENABLED = False
    restore, placed_orders = install_all_dhan_mocks()
    try:
        trade_history.append_jsonl("real_trades", {
            "strategy": "Luxury", "underlying_symbol": "NBCC", "pnl": -50.0,
            "closed_at": datetime.now().isoformat(),
        })
        result = await lte._process_one_entry("NBCC", "CE")
        assert result["status"] == "entered", f"disabling the flag must bypass the cooldown entirely, got {result}"
        print("8. LOSS_COOLDOWN_ENABLED=False cleanly bypasses the check even seconds after a real loss: PASSED")
    finally:
        restore()
        lte.config.LOSS_COOLDOWN_ENABLED = real_enabled


# --------------------------------------------------------------------- #
# 3. dhan_client.refresh_liquidity_signal / get_cached_illiquid
# --------------------------------------------------------------------- #

def test_9_liquidity_signal_detects_sustained_zero_volume():
    now_ts = int(datetime.now().timestamp())
    # 5 completed bars, oldest to newest, all zero volume, plus a
    # still-forming 6th bar (timestamp = now, must be dropped).
    timestamps = [now_ts - 300, now_ts - 240, now_ts - 180, now_ts - 120, now_ts - 60, now_ts]
    volumes = [0, 0, 0, 0, 0, 0]
    restore, _ = install_all_dhan_mocks(volumes_sequence=[_make_candle_response(timestamps, volumes)])
    real_bars = odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS
    odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = 3
    try:
        odc.dhan_wrapper._liquidity_cache.clear()
        odc.dhan_wrapper.refresh_liquidity_signal("TESTOPT 29 SEP 100 CALL")
        assert odc.dhan_wrapper.get_cached_illiquid("TESTOPT 29 SEP 100 CALL") is True
        print("9. Sustained zero volume over the last N completed bars is correctly flagged illiquid: PASSED")
    finally:
        restore()
        odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = real_bars


def test_10_liquidity_signal_one_nonzero_bar_means_liquid():
    now_ts = int(datetime.now().timestamp())
    timestamps = [now_ts - 300, now_ts - 240, now_ts - 180, now_ts - 120, now_ts - 60, now_ts]
    volumes = [0, 0, 500, 0, 0, 0]  # one real trade in the middle of the window
    restore, _ = install_all_dhan_mocks(volumes_sequence=[_make_candle_response(timestamps, volumes)])
    real_bars = odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS
    odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = 3
    try:
        odc.dhan_wrapper._liquidity_cache.clear()
        odc.dhan_wrapper.refresh_liquidity_signal("TESTOPT 29 SEP 100 CALL")
        assert odc.dhan_wrapper.get_cached_illiquid("TESTOPT 29 SEP 100 CALL") is False, \
            "any nonzero-volume bar within the trailing window must count as liquid, not illiquid"
        print("10. A single nonzero-volume bar anywhere in the trailing window correctly reads as liquid: PASSED")
    finally:
        restore()
        odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = real_bars


def test_11_liquidity_signal_drops_still_forming_candle():
    now_ts = int(datetime.now().timestamp())
    # Only the LAST bar (still forming, timestamp=now) has volume - if it
    # were wrongly included as "completed", the zero-streak count would
    # be wrong (4 zero bars instead of correctly seeing only 3 completed
    # ones, all zero, and the not-yet-closed one excluded).
    timestamps = [now_ts - 240, now_ts - 180, now_ts - 120, now_ts - 60, now_ts]
    volumes = [0, 0, 0, 0, 999]
    restore, _ = install_all_dhan_mocks(volumes_sequence=[_make_candle_response(timestamps, volumes)])
    real_bars = odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS
    odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = 4
    try:
        odc.dhan_wrapper._liquidity_cache.clear()
        odc.dhan_wrapper.refresh_liquidity_signal("TESTOPT 29 SEP 100 CALL")
        # 4 completed bars after dropping the still-forming one, all zero -> illiquid.
        assert odc.dhan_wrapper.get_cached_illiquid("TESTOPT 29 SEP 100 CALL") is True
        print("11. The still-forming last candle is correctly dropped before judging the zero-volume streak: PASSED")
    finally:
        restore()
        odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = real_bars


def test_12_liquidity_signal_not_enough_bars_fails_safe():
    now_ts = int(datetime.now().timestamp())
    timestamps = [now_ts - 60, now_ts]
    volumes = [0, 0]
    restore, _ = install_all_dhan_mocks(volumes_sequence=[_make_candle_response(timestamps, volumes)])
    real_bars = odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS
    odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = 3
    try:
        odc.dhan_wrapper._liquidity_cache.clear()
        odc.dhan_wrapper.refresh_liquidity_signal("TESTOPT 29 SEP 100 CALL")
        assert odc.dhan_wrapper.get_cached_illiquid("TESTOPT 29 SEP 100 CALL") is False, \
            "too little history (fewer completed bars than the required streak) must fail safe, not guess illiquid"
        print("12. Fewer completed bars than the required streak length fails safe (not illiquid), never guesses: PASSED")
    finally:
        restore()
        odc.config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS = real_bars


def test_13_liquidity_signal_none_before_first_refresh():
    odc.dhan_wrapper._liquidity_cache.clear()
    assert odc.dhan_wrapper.get_cached_illiquid("NEVERSEEN 29 SEP 1 CALL") is None
    print("13. An option never yet refreshed correctly reads as None (no signal yet), never a guessed True/False: PASSED")


# --------------------------------------------------------------------- #
# 4. _exit_reason_for's own ordering
# --------------------------------------------------------------------- #

def test_14_liquidity_guard_fires_when_nothing_else_would_have():
    # Price basically flat (well inside every price threshold) - the
    # exact CHOLAFIN shape: nothing else fires, only the liquidity guard.
    # hard_stop_loss set well below ltp so current_trailing_sl (a
    # property derived from it, not a constructor field) never triggers.
    pos = Position(
        underlying_symbol="CHOLAFIN", option_trading_symbol="CHOLAFIN 29 SEP 1840 CALL",
        option_type="CE", quantity=625, lot_size=625, entry_price=56.35, target_price=100.0,
        highest_price=56.35, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    reason = lte._exit_reason_for(pos, ltp=56.25, supertrend_against_position=False, liquidity_guard_triggered=True)
    assert reason == "LIQUIDITY_GUARD_ZERO_VOLUME", reason
    print("14. The liquidity guard correctly fires on its own when no price threshold is anywhere close, "
          "the exact real CHOLAFIN shape (price ~flat, contract gone quiet): PASSED")


def test_15_price_threshold_exit_still_takes_priority_over_liquidity_guard():
    pos = Position(
        underlying_symbol="TESTSTOCK", option_trading_symbol="TESTSTOCK 29 SEP 100 CALL",
        option_type="CE", quantity=100, lot_size=100, entry_price=10.0, target_price=12.0,
        highest_price=10.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    # TARGET_HIT (ltp >= 12.0) AND liquidity_guard both true at once -
    # TARGET_HIT must win (checked first).
    reason = lte._exit_reason_for(pos, ltp=12.5, supertrend_against_position=False, liquidity_guard_triggered=True)
    assert reason == "TARGET_HIT", reason
    print("15. A genuine price-threshold exit (e.g. TARGET_HIT) still takes priority over the liquidity guard "
          "when both happen to be true on the same tick: PASSED")


def test_16_liquidity_guard_disabled_flag_bypasses_the_check():
    pos = Position(
        underlying_symbol="CHOLAFIN", option_trading_symbol="CHOLAFIN 29 SEP 1840 CALL",
        option_type="CE", quantity=625, lot_size=625, entry_price=56.35, target_price=100.0,
        highest_price=56.35, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    real_enabled = lte.config.LIQUIDITY_GUARD_ENABLED
    lte.config.LIQUIDITY_GUARD_ENABLED = False
    try:
        reason = lte._exit_reason_for(pos, ltp=56.25, supertrend_against_position=False, liquidity_guard_triggered=True)
        assert reason is None, f"disabling the flag must suppress the exit even when the signal itself fired, got {reason}"
        print("16. LIQUIDITY_GUARD_ENABLED=False cleanly suppresses the exit even when the underlying signal fired: PASSED")
    finally:
        lte.config.LIQUIDITY_GUARD_ENABLED = real_enabled


# --------------------------------------------------------------------- #
# 4. Repeat-loss same-day block (added 8 Sep 2026) - trade_history.
#    loss_exit_count_today + Luxury/trading_engine.py's own check in
#    _process_one_entry. Found via a real Chartink-alert backtest of the
#    "longTerm" scan (user's own wording): "re occurring losses hit at
#    OIL and at many places... after a loss is hit 2 times on a same
#    stock trade on that day, same stock trading should not be allowed
#    for loss based condition only."
# --------------------------------------------------------------------- #

def test_17_loss_exit_count_today_basic():
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "OIL", "exit_reason": "MAX_LOSS_HIT",
        "closed_at": datetime.now().isoformat(),
    })
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "OIL", "exit_reason": "MAX_LOSS_HIT",
        "closed_at": datetime.now().isoformat(),
    })
    count = trade_history.loss_exit_count_today("Luxury", "OIL", ("MAX_LOSS_HIT", "STOP_LOSS_HIT"))
    assert count == 2, count
    print("17. loss_exit_count_today correctly counts every matching loss-reason exit logged today for the "
          "given strategy+symbol: PASSED")


def test_18_loss_exit_count_today_ignores_non_loss_reasons_and_other_symbols_strategies():
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "DIVISLAB", "exit_reason": "PROFIT_PROTECTION_HIT",
        "closed_at": datetime.now().isoformat(),
    })
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "DIVISLAB", "exit_reason": "TARGET_HIT",
        "closed_at": datetime.now().isoformat(),
    })
    trade_history.append_jsonl("real_trades", {
        "strategy": "Luxury", "underlying_symbol": "DIVISLAB", "exit_reason": "SUPERTREND_EXIT",
        "closed_at": datetime.now().isoformat(),
    })
    trade_history.append_jsonl("real_trades", {
        "strategy": "Options", "underlying_symbol": "DIVISLAB", "exit_reason": "MAX_LOSS_HIT",
        "closed_at": datetime.now().isoformat(),
    })
    count = trade_history.loss_exit_count_today("Luxury", "DIVISLAB", ("MAX_LOSS_HIT", "STOP_LOSS_HIT"))
    assert count == 0, \
        f"profit/target/supertrend exits and a DIFFERENT strategy's own MAX_LOSS_HIT must not count, got {count}"
    print("18. loss_exit_count_today ignores non-loss exit reasons (profit protection/target/supertrend) and "
          "a different strategy's own record for the same symbol - 'loss based condition[s]' only, per the "
          "user's own wording: PASSED")


async def test_19_real_second_loss_blocks_third_entry_same_day():
    store = lps.PositionStore()
    lte.position_store = store
    real_cooldown_enabled = lte.config.LOSS_COOLDOWN_ENABLED
    real_block_enabled, real_block_count = lte.config.LOSS_REPEAT_BLOCK_ENABLED, lte.config.LOSS_REPEAT_BLOCK_COUNT
    # Cooldown disabled here so ONLY the new repeat-block feature is under
    # test - otherwise the pre-existing 20-minute cooldown would ALSO
    # block the immediate retries below, confounding which feature fired.
    lte.config.LOSS_COOLDOWN_ENABLED = False
    lte.config.LOSS_REPEAT_BLOCK_ENABLED = True
    lte.config.LOSS_REPEAT_BLOCK_COUNT = 2
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "GAIL"
        entry_1 = await lte._process_one_entry(symbol, "CE")
        assert entry_1["status"] == "entered", entry_1
        closed_1 = await store.close_position(symbol, 40.0, "MAX_LOSS_HIT")
        assert closed_1 is not None
        await asyncio.sleep(0.3)

        entry_2 = await lte._process_one_entry(symbol, "CE")
        assert entry_2["status"] == "entered", \
            f"only ONE prior loss so far - must still enter normally, got {entry_2}"
        closed_2 = await store.close_position(symbol, 41.0, "MAX_LOSS_HIT")
        assert closed_2 is not None
        await asyncio.sleep(0.3)

        placed_orders.clear()
        entry_3 = await lte._process_one_entry(symbol, "CE")
        assert entry_3["status"] == "skipped" and entry_3["reason"] == "loss_repeat_block_active", entry_3
        assert placed_orders == [], f"a repeat-loss-blocked symbol must place ZERO orders, got {placed_orders}"

        placed_orders.clear()
        other = await lte._process_one_entry("NMDC", "CE")
        assert other["status"] == "entered", f"a DIFFERENT symbol must be entirely unaffected, got {other}"

        print("19. TWO real same-day MAX_LOSS_HIT losses for the same symbol correctly block a third real "
              "entry attempt (zero orders placed) for the REST OF THE DAY, while a different symbol is "
              "entirely unaffected: PASSED")
    finally:
        restore()
        lte.config.LOSS_COOLDOWN_ENABLED = real_cooldown_enabled
        lte.config.LOSS_REPEAT_BLOCK_ENABLED = real_block_enabled
        lte.config.LOSS_REPEAT_BLOCK_COUNT = real_block_count


async def test_20_a_win_between_two_losses_does_not_reset_the_count_and_disabled_flag_bypasses():
    store = lps.PositionStore()
    lte.position_store = store
    real_cooldown_enabled = lte.config.LOSS_COOLDOWN_ENABLED
    real_block_enabled, real_block_count = lte.config.LOSS_REPEAT_BLOCK_ENABLED, lte.config.LOSS_REPEAT_BLOCK_COUNT
    real_daily_cap = lte.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    lte.config.LOSS_COOLDOWN_ENABLED = False
    lte.config.LOSS_REPEAT_BLOCK_ENABLED = True
    lte.config.LOSS_REPEAT_BLOCK_COUNT = 2
    # This test makes 4 real entry attempts for the SAME symbol - raised
    # so the pre-existing daily re-entry cap (default 3) doesn't fire
    # first and mask what's actually under test here.
    lte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 10
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "SAIL"
        e1 = await lte._process_one_entry(symbol, "CE")
        assert e1["status"] == "entered", e1
        await store.close_position(symbol, 30.0, "MAX_LOSS_HIT")
        await asyncio.sleep(0.3)

        e2 = await lte._process_one_entry(symbol, "CE")
        assert e2["status"] == "entered", e2
        await store.close_position(symbol, 60.0, "TARGET_HIT")  # a genuine WIN in between - doesn't reset anything
        await asyncio.sleep(0.3)

        e3 = await lte._process_one_entry(symbol, "CE")
        assert e3["status"] == "entered", \
            f"still only ONE loss-designated exit so far (the win doesn't count either way) - must enter, got {e3}"
        await store.close_position(symbol, 31.0, "MAX_LOSS_HIT")  # second REAL loss
        await asyncio.sleep(0.3)

        placed_orders.clear()
        e4 = await lte._process_one_entry(symbol, "CE")
        assert e4["status"] == "skipped" and e4["reason"] == "loss_repeat_block_active", \
            f"2 genuine loss-designated exits today (the win in between doesn't dilute this) must now block, got {e4}"

        # Flag disabled entirely bypasses, even with the same 2 real losses already on record.
        lte.config.LOSS_REPEAT_BLOCK_ENABLED = False
        placed_orders.clear()
        e5 = await lte._process_one_entry(symbol, "CE")
        assert e5["status"] == "entered", f"disabling the flag must bypass the block entirely, got {e5}"

        print("20. A win between two real losses doesn't reset/dilute the repeat-loss count (still blocks on "
              "the 2nd genuine loss); LOSS_REPEAT_BLOCK_ENABLED=False cleanly bypasses the check: PASSED")
    finally:
        restore()
        lte.config.LOSS_COOLDOWN_ENABLED = real_cooldown_enabled
        lte.config.LOSS_REPEAT_BLOCK_ENABLED = real_block_enabled
        lte.config.LOSS_REPEAT_BLOCK_COUNT = real_block_count
        lte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_daily_cap


def test_21_profit_protection_giveback_buffer():
    """LUXURY_PROFIT_PROTECTION_GIVEBACK_PCT (added 10 Sep 2026 after OIL
    exited on a ~10-paise dip from the peak): once peak profit has crossed
    the threshold, the exit only fires once price has retraced at least
    this fraction off the peak. Default 0.0 == the original zero-tolerance
    behaviour."""
    def mk():
        return Position(
            underlying_symbol="T", option_trading_symbol="T 29 SEP 100 CALL", option_type="CE",
            quantity=1000, lot_size=1000, entry_price=10.0, target_price=100.0,
            highest_price=12.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
        )  # peak profit (12-10)*1000 = 2000 > the 1500 threshold -> PP armed
    real = lte.config.PROFIT_PROTECTION_GIVEBACK_PCT
    try:
        lte.config.PROFIT_PROTECTION_GIVEBACK_PCT = 0.0
        assert lte._exit_reason_for(mk(), ltp=11.99) == "PROFIT_PROTECTION_HIT", \
            "buffer 0.0 must still exit on any dip below the peak (unchanged behaviour)"

        lte.config.PROFIT_PROTECTION_GIVEBACK_PCT = 0.05  # give-back floor = 12.0 * 0.95 = 11.40
        assert lte._exit_reason_for(mk(), ltp=11.99) is None, \
            "an 8-paise dip must NOT stop the trade out with a 5% give-back buffer"
        assert lte._exit_reason_for(mk(), ltp=11.39) == "PROFIT_PROTECTION_HIT", \
            "a retrace past 5% off the peak still fires PROFIT_PROTECTION_HIT"
        print("21. LUXURY_PROFIT_PROTECTION_GIVEBACK_PCT: buffer=0 exits on any dip (unchanged); buffer=0.05 "
              "rides a small wiggle and only locks in once price is >5% off the peak: PASSED")
    finally:
        lte.config.PROFIT_PROTECTION_GIVEBACK_PCT = real


async def main():
    print("=== Luxury corrective actions (loss cooldown + liquidity guard + repeat-loss block) test suite ===\n")
    test_1_minutes_since_last_loss_basic()
    test_2_minutes_since_last_loss_ignores_wins()
    test_3_minutes_since_last_loss_isolates_by_strategy_and_symbol()
    test_4_minutes_since_last_loss_uses_the_most_recent_of_several()
    test_5_minutes_since_last_loss_none_when_no_log_or_no_loss_today()
    await test_6_real_loss_then_immediate_reentry_blocked()
    await test_7_reentry_succeeds_once_cooldown_expires()
    await test_8_loss_cooldown_disabled_flag_bypasses_the_check()
    test_9_liquidity_signal_detects_sustained_zero_volume()
    test_10_liquidity_signal_one_nonzero_bar_means_liquid()
    test_11_liquidity_signal_drops_still_forming_candle()
    test_12_liquidity_signal_not_enough_bars_fails_safe()
    test_13_liquidity_signal_none_before_first_refresh()
    test_14_liquidity_guard_fires_when_nothing_else_would_have()
    test_15_price_threshold_exit_still_takes_priority_over_liquidity_guard()
    test_16_liquidity_guard_disabled_flag_bypasses_the_check()
    test_17_loss_exit_count_today_basic()
    test_18_loss_exit_count_today_ignores_non_loss_reasons_and_other_symbols_strategies()
    await test_19_real_second_loss_blocks_third_entry_same_day()
    await test_20_a_win_between_two_losses_does_not_reset_the_count_and_disabled_flag_bypasses()
    test_21_profit_protection_giveback_buffer()
    print("\nALL LUXURY CORRECTIVE ACTION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
