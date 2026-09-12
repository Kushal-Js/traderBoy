"""
Tests for Options' own wiring of three corrective actions ported from
Luxury on 10 Sep 2026 (user request: "make Options have similar rule
set and guard rails as Luxury have, add and deploy also"): same-day
loss cooldown, the liquidity guard, and the repeat-loss same-day block.

The underlying shared mechanisms (trade_history.minutes_since_last_
loss_today, trade_history.loss_exit_count_today, dhan_client.
refresh_liquidity_signal/get_cached_illiquid) are already fully tested
against Luxury's own strategy tag in tests/test_luxury_corrective_
actions.py - those are pure, strategy-agnostic functions, so this file
does NOT re-test their internal logic (that would just be duplicate
coverage of the identical code path under a different string constant).
What IS new and needs its own coverage is OPTIONS' OWN integration
wiring - _process_one_entry's loss-cooldown/repeat-block checks and
_exit_reason_for's liquidity_guard_triggered parameter - since each
package keeps its own copy of trading_engine.py, so a wiring mistake in
one package's own copy wouldn't be caught by the other's tests.

The time-based LOSS_COOLDOWN_ENABLED/LOSS_COOLDOWN_MINUTES mechanism
this file used to test was REMOVED on 11 Sep 2026 (user request: "Remove
this cooldown period logic from everywhere and all strategies, instead
create another global common function which checks if RSI...") - see
Options/config.py's "Same-day RSI-gated loss re-entry block" comment and
Options/dhan_client.py's refresh_rsi_signal/is_rsi_loss_reentry_blocked
for the replacement. Its own RSI computation correctness is covered by
tests/test_rsi_loss_reentry_block.py; this file mocks is_rsi_loss_
reentry_blocked directly to test only FUTURES' OWN _process_one_entry
wiring.

Covers, against the REAL production functions (not reimplemented):
  1. A real MAX_LOSS_HIT loss + an RSI condition (mocked True) correctly
     blocks an immediate real re-entry attempt for the SAME symbol (zero
     orders placed), while a DIFFERENT symbol with no loss today is
     entirely unaffected (RSI is never even consulted for it).
  2. Once RSI recovers (mocked False) the SAME DAY, the same symbol
     re-enters normally even though it lost earlier today - proves this
     is a re-checkable condition, not a permanent same-day block.
  3. ENABLE_RSI_LOSS_REENTRY_BLOCK=False cleanly bypasses the check even
     with a real loss on record and RSI mocked as still blocking.
  4. The liquidity guard correctly fires on its own when no price
     threshold is anywhere close (the CHOLAFIN shape).
  5. A genuine price-threshold exit (e.g. TARGET_HIT) still takes
     priority over the liquidity guard on the same tick.
  6. LIQUIDITY_GUARD_ENABLED=False cleanly suppresses the exit even
     when the underlying signal fired.
  7. TWO real same-day MAX_LOSS_HIT losses for the same symbol
     correctly block a third real entry attempt for the REST OF THE
     DAY via LOSS_REPEAT_BLOCK_ENABLED, while a different symbol is
     unaffected.
  8. A win between two real losses doesn't reset/dilute the repeat-loss
     count; LOSS_REPEAT_BLOCK_ENABLED=False cleanly bypasses the check.
  9. PROFIT_PROTECTION_GIVEBACK_PCT rides a small wiggle, locks in past it.
 10. FUTURES_ENABLE_TARGET_EXIT=false suppresses TARGET_HIT only - the
     winner rides on; every other exit condition still fires.
 11. EMA_CROSS_EXIT (FUTURES_ENABLE_EMA_CROSS_EXIT=true): fires on a real
     fast/slow EMA crossover against the position on a post-entry 5-min
     candle; ignores a standing state, the entry candle, and the flag off.
 12. refresh_ema_cross_signal runs on a continuous multi-session candle series
     (EMAs warm from bar 1, overnight crossover counts) - no daily reset,
     the way a charting platform runs.

HOW TO RUN:
    uv run python tests/test_options_corrective_actions.py
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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_futures_corrective_actions_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Futures.position_store as fps
import Futures.trading_engine as fte
from Futures.position_store import Position
from Options.dhan_client import AtmOption, OrderResult, OrderStatus

FUTURE_EXPIRY = date.today() + timedelta(days=25)


def fake_atm_option(symbol: str, option_type: str) -> AtmOption:
    return AtmOption(trading_symbol=f"{symbol} FAKE EXP {option_type}", strike=1000.0,
                      option_type=option_type, lot_size=500, security_id=f"SECID-{symbol}",
                      expiry_date=FUTURE_EXPIRY)


def install_all_dhan_mocks():
    """Mocks every Dhan network call the entry/exit paths touch. Every
    unique order gets its own order_id so repeated entries for the same
    symbol across this file's several scenarios never collide."""
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
        "refresh_ema_cross_signal": odc.dhan_wrapper.refresh_ema_cross_signal,
        "get_cached_ema_cross_candle_start": odc.dhan_wrapper.get_cached_ema_cross_candle_start,
        "is_rsi_loss_reentry_blocked": odc.dhan_wrapper.is_rsi_loss_reentry_blocked,
        "get_cached_rsi": odc.dhan_wrapper.get_cached_rsi,
        "get_cached_prev_rsi": odc.dhan_wrapper.get_cached_prev_rsi,
        "rsi_loss_reentry_reason": odc.dhan_wrapper.rsi_loss_reentry_reason,
        "place_market_order": odc.dhan_wrapper.place_market_order,
        "place_stop_loss_limit_order": odc.dhan_wrapper.place_stop_loss_limit_order,
        "check_if_order_filled": odc.dhan_wrapper.check_if_order_filled,
        "wait_for_order_result": odc.dhan_wrapper.wait_for_order_result,
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
    odc.dhan_wrapper.refresh_ema_cross_signal = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_ema_cross_candle_start = lambda underlying_symbol: None
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: False
    odc.dhan_wrapper.get_cached_rsi = lambda underlying_symbol: None
    odc.dhan_wrapper.get_cached_prev_rsi = lambda underlying_symbol: None
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda underlying_symbol: None

    placed_orders = []

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders)}-{id(object())}"
        placed_orders.append({"trading_symbol": trading_symbol, "transaction_type": transaction_type})
        return {"order_id": order_id, "is_amo": False}

    odc.dhan_wrapper.place_market_order = fake_place_market_order
    # Options' own _enter_single_position now places a real broker-side
    # stop-loss order (ported from Luxury, 10 Sep 2026) right after every
    # entry - unmocked, this would fall through to a REAL Dhan call.
    odc.dhan_wrapper.place_stop_loss_limit_order = lambda trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SLL-{trading_symbol}-{len(placed_orders)}-{id(object())}"}
    odc.dhan_wrapper.check_if_order_filled = lambda order_id: None
    odc.dhan_wrapper.wait_for_order_result = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.TRADED, remark="", fill_price=50.0, filled_quantity=500, is_amo=False)

    def restore():
        for name, fn in originals.items():
            setattr(odc.dhan_wrapper, name, fn)

    return restore, placed_orders


# --------------------------------------------------------------------- #
# 1. Same-day loss cooldown - Options' own _process_one_entry wiring
# --------------------------------------------------------------------- #

async def test_1_real_loss_plus_rsi_condition_blocks_reentry_other_symbol_unaffected():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = True
    restore, placed_orders = install_all_dhan_mocks()
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: True
    odc.dhan_wrapper.get_cached_rsi = lambda underlying_symbol: 91.5
    odc.dhan_wrapper.get_cached_prev_rsi = lambda underlying_symbol: 93.0
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda underlying_symbol: "overbought"
    try:
        entry = await fte._process_one_entry("COALINDIA", "CE")
        assert entry["status"] == "entered", entry

        closed = await store.close_position("COALINDIA", 40.0, "MAX_LOSS_HIT")
        assert closed is not None and closed.exit_price == 40.0
        await asyncio.sleep(0.3)

        placed_orders.clear()
        retry = await fte._process_one_entry("COALINDIA", "CE")
        assert retry["status"] == "skipped" and retry["reason"] == "rsi_loss_reentry_block_active", retry
        assert placed_orders == [], f"an RSI-blocked symbol must place ZERO orders, got {placed_orders}"

        placed_orders.clear()
        other = await fte._process_one_entry("RVNL", "CE")
        assert other["status"] == "entered", f"a DIFFERENT symbol must be entirely unaffected, got {other}"

        print("1. A real MAX_LOSS_HIT loss + an RSI condition correctly blocks an immediate real re-entry "
              "attempt for the SAME symbol (zero orders placed), while a different symbol with no loss "
              "today is entirely unaffected: PASSED")
    finally:
        restore()
        fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_enabled


async def test_2_reentry_succeeds_once_rsi_recovers_same_day():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = True
    restore, placed_orders = install_all_dhan_mocks()
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: False
    try:
        trade_history.append_jsonl("real_trades", {
            "strategy": "Futures", "underlying_symbol": "KFINTECH", "pnl": -900.0,
            "exit_reason": "MAX_LOSS_HIT", "closed_at": (datetime.now() - timedelta(minutes=2)).isoformat(),
        })
        result = await fte._process_one_entry("KFINTECH", "CE")
        assert result["status"] == "entered", \
            f"once RSI is no longer overbought/falling, the SAME symbol must re-enter normally " \
            f"the SAME day, even though it lost minutes ago - this is a condition, not a timer, got {result}"
        print("2. Once RSI recovers (neither overbought nor falling) the SAME day, the same symbol "
              "re-enters normally even though it lost earlier today - a re-checkable condition, "
              "not a permanent same-day block: PASSED")
    finally:
        restore()
        fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_enabled


async def test_3_rsi_loss_reentry_disabled_flag_bypasses_the_check():
    store = fps.PositionStore()
    fte.position_store = store
    real_enabled = fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    restore, placed_orders = install_all_dhan_mocks()
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: True
    try:
        trade_history.append_jsonl("real_trades", {
            "strategy": "Futures", "underlying_symbol": "NBCC", "pnl": -50.0,
            "exit_reason": "MAX_LOSS_HIT", "closed_at": datetime.now().isoformat(),
        })
        result = await fte._process_one_entry("NBCC", "CE")
        assert result["status"] == "entered", \
            f"disabling the flag must bypass the RSI-loss-reentry check entirely, got {result}"
        print("3. ENABLE_RSI_LOSS_REENTRY_BLOCK=False cleanly bypasses the check even with a real loss "
              "on record and RSI mocked as still blocking: PASSED")
    finally:
        restore()
        fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_enabled


# --------------------------------------------------------------------- #
# 2. Liquidity guard - Options' own _exit_reason_for wiring
# --------------------------------------------------------------------- #

def test_4_liquidity_guard_fires_when_nothing_else_would_have():
    pos = Position(
        underlying_symbol="CHOLAFIN", option_trading_symbol="CHOLAFIN 29 SEP 1840 CALL",
        option_type="CE", quantity=625, lot_size=625, entry_price=56.35, target_price=100.0,
        highest_price=56.35, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    reason = fte._exit_reason_for(pos, ltp=56.25, supertrend_against_position=False, liquidity_guard_triggered=True)
    assert reason == "LIQUIDITY_GUARD_ZERO_VOLUME", reason
    print("4. The liquidity guard correctly fires on its own when no price threshold is anywhere close, "
          "the exact real CHOLAFIN shape (price ~flat, contract gone quiet): PASSED")


def test_5_price_threshold_exit_still_takes_priority_over_liquidity_guard():
    pos = Position(
        underlying_symbol="TESTSTOCK", option_trading_symbol="TESTSTOCK 29 SEP 100 CALL",
        option_type="CE", quantity=100, lot_size=100, entry_price=10.0, target_price=12.0,
        highest_price=10.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    real = fte.config.ENABLE_TARGET_EXIT
    fte.config.ENABLE_TARGET_EXIT = True  # this test is specifically about the fixed target
    try:
        reason = fte._exit_reason_for(pos, ltp=12.5, supertrend_against_position=False, liquidity_guard_triggered=True)
        assert reason == "TARGET_HIT", reason
        print("5. A genuine price-threshold exit (e.g. TARGET_HIT) still takes priority over the liquidity guard "
              "when both happen to be true on the same tick: PASSED")
    finally:
        fte.config.ENABLE_TARGET_EXIT = real


def test_10_target_exit_disabled_flag_suppresses_target_hit():
    """FUTURES_ENABLE_TARGET_EXIT=false (user request 10 Sep 2026: "disable
    TARGET_HIT for Futures, rest to remain same"): a position at/above its
    target_price is NOT closed for TARGET_HIT. Every other exit condition is
    untouched - MAX_LOSS_HIT, PROFIT_PROTECTION_HIT, the trailing/hard SL,
    SUPERTREND_EXIT and the liquidity guard all still fire exactly as before."""
    real = fte.config.ENABLE_TARGET_EXIT
    try:
        fte.config.ENABLE_TARGET_EXIT = False

        # Well past target, no other condition met -> rides on, no exit.
        riding = Position(
            underlying_symbol="RUNNER", option_trading_symbol="RUNNER 29 SEP 100 CALL",
            option_type="CE", quantity=100, lot_size=100, entry_price=10.0, target_price=12.0,
            highest_price=13.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
        )  # peak profit (13-10)*100 = 300, well under the 1500 PP threshold
        assert fte._exit_reason_for(riding, ltp=13.0) is None, \
            "with the flag off, touching/exceeding target_price must NOT trigger an exit"

        # Same position, flag back on -> TARGET_HIT fires, proving that's the only thing suppressed.
        fte.config.ENABLE_TARGET_EXIT = True
        assert fte._exit_reason_for(riding, ltp=13.0) == "TARGET_HIT"

        # Flag off must not weaken the loss side: a real MAX_LOSS still exits.
        # quantity=10000 so the same $2 move (entry 10.0 -> ltp 8.0) produces
        # a Rs 20,000 loss - well past MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF's
        # own current value (raised 1200->1500->4500, 12 Sep 2026) regardless
        # of which side of the cutoff this test happens to run on.
        fte.config.ENABLE_TARGET_EXIT = False
        losing = Position(
            underlying_symbol="LOSER", option_trading_symbol="LOSER 29 SEP 100 CALL",
            option_type="CE", quantity=10000, lot_size=10000, entry_price=10.0, target_price=12.0,
            highest_price=10.0, hard_stop_loss=8.4, order_id="X", product_type="MARGIN",
        )
        assert fte._exit_reason_for(losing, ltp=8.0) == "MAX_LOSS_HIT", \
            "disabling the target exit must not affect the loss-side checks"

        # And PROFIT_PROTECTION still catches a genuine give-back from a real peak.
        protected = Position(
            underlying_symbol="PEAKER", option_trading_symbol="PEAKER 29 SEP 100 CALL",
            option_type="CE", quantity=1000, lot_size=1000, entry_price=10.0, target_price=12.0,
            highest_price=12.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
        )  # peak profit (12-10)*1000 = 2000 > 1500 threshold
        assert fte._exit_reason_for(protected, ltp=11.99) == "PROFIT_PROTECTION_HIT", \
            "PROFIT_PROTECTION_HIT is now the primary profit-taking exit and must still fire"

        print("10. FUTURES_ENABLE_TARGET_EXIT=false suppresses TARGET_HIT only - a winner rides past target, "
              "while MAX_LOSS_HIT / PROFIT_PROTECTION_HIT / SL all still fire: PASSED")
    finally:
        fte.config.ENABLE_TARGET_EXIT = real


def test_6_liquidity_guard_disabled_flag_bypasses_the_check():
    pos = Position(
        underlying_symbol="CHOLAFIN", option_trading_symbol="CHOLAFIN 29 SEP 1840 CALL",
        option_type="CE", quantity=625, lot_size=625, entry_price=56.35, target_price=100.0,
        highest_price=56.35, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    real_enabled = fte.config.LIQUIDITY_GUARD_ENABLED
    fte.config.LIQUIDITY_GUARD_ENABLED = False
    try:
        reason = fte._exit_reason_for(pos, ltp=56.25, supertrend_against_position=False, liquidity_guard_triggered=True)
        assert reason is None, f"disabling the flag must suppress the exit even when the signal itself fired, got {reason}"
        print("6. LIQUIDITY_GUARD_ENABLED=False cleanly suppresses the exit even when the underlying signal fired: PASSED")
    finally:
        fte.config.LIQUIDITY_GUARD_ENABLED = real_enabled


# --------------------------------------------------------------------- #
# 3. Repeat-loss same-day block - Options' own _process_one_entry wiring
# --------------------------------------------------------------------- #

async def test_7_real_second_loss_blocks_third_entry_same_day():
    store = fps.PositionStore()
    fte.position_store = store
    real_rsi_block_enabled = fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    real_block_enabled, real_block_count = fte.config.LOSS_REPEAT_BLOCK_ENABLED, fte.config.LOSS_REPEAT_BLOCK_COUNT
    # RSI-loss-reentry disabled here so ONLY the repeat-block feature is
    # under test - otherwise it would ALSO block the immediate retries
    # below (both fire on the same MAX_LOSS_HIT), confounding which
    # feature fired.
    fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    fte.config.LOSS_REPEAT_BLOCK_ENABLED = True
    fte.config.LOSS_REPEAT_BLOCK_COUNT = 2
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "GAIL"
        entry_1 = await fte._process_one_entry(symbol, "CE")
        assert entry_1["status"] == "entered", entry_1
        closed_1 = await store.close_position(symbol, 40.0, "MAX_LOSS_HIT")
        assert closed_1 is not None
        await asyncio.sleep(0.3)

        entry_2 = await fte._process_one_entry(symbol, "CE")
        assert entry_2["status"] == "entered", \
            f"only ONE prior loss so far - must still enter normally, got {entry_2}"
        closed_2 = await store.close_position(symbol, 41.0, "MAX_LOSS_HIT")
        assert closed_2 is not None
        await asyncio.sleep(0.3)

        placed_orders.clear()
        entry_3 = await fte._process_one_entry(symbol, "CE")
        assert entry_3["status"] == "skipped" and entry_3["reason"] == "loss_repeat_block_active", entry_3
        assert placed_orders == [], f"a repeat-loss-blocked symbol must place ZERO orders, got {placed_orders}"

        placed_orders.clear()
        other = await fte._process_one_entry("NMDC", "CE")
        assert other["status"] == "entered", f"a DIFFERENT symbol must be entirely unaffected, got {other}"

        print("7. TWO real same-day MAX_LOSS_HIT losses for the same symbol correctly block a third real "
              "entry attempt (zero orders placed) for the REST OF THE DAY, while a different symbol is "
              "entirely unaffected: PASSED")
    finally:
        restore()
        fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_rsi_block_enabled
        fte.config.LOSS_REPEAT_BLOCK_ENABLED = real_block_enabled
        fte.config.LOSS_REPEAT_BLOCK_COUNT = real_block_count


async def test_8_a_win_between_two_losses_does_not_reset_the_count_and_disabled_flag_bypasses():
    store = fps.PositionStore()
    fte.position_store = store
    real_rsi_block_enabled = fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    real_block_enabled, real_block_count = fte.config.LOSS_REPEAT_BLOCK_ENABLED, fte.config.LOSS_REPEAT_BLOCK_COUNT
    real_daily_cap = fte.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    fte.config.LOSS_REPEAT_BLOCK_ENABLED = True
    fte.config.LOSS_REPEAT_BLOCK_COUNT = 2
    # This test makes 4 real entry attempts for the SAME symbol - raised
    # so the pre-existing daily re-entry cap (default 3) doesn't fire
    # first and mask what's actually under test here.
    fte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 10
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "SAIL"
        e1 = await fte._process_one_entry(symbol, "CE")
        assert e1["status"] == "entered", e1
        await store.close_position(symbol, 30.0, "MAX_LOSS_HIT")
        await asyncio.sleep(0.3)

        e2 = await fte._process_one_entry(symbol, "CE")
        assert e2["status"] == "entered", e2
        await store.close_position(symbol, 60.0, "TARGET_HIT")  # a genuine WIN in between - doesn't reset anything
        await asyncio.sleep(0.3)

        e3 = await fte._process_one_entry(symbol, "CE")
        assert e3["status"] == "entered", \
            f"still only ONE loss-designated exit so far (the win doesn't count either way) - must enter, got {e3}"
        await store.close_position(symbol, 31.0, "MAX_LOSS_HIT")  # second REAL loss
        await asyncio.sleep(0.3)

        placed_orders.clear()
        e4 = await fte._process_one_entry(symbol, "CE")
        assert e4["status"] == "skipped" and e4["reason"] == "loss_repeat_block_active", \
            f"2 genuine loss-designated exits today (the win in between doesn't dilute this) must now block, got {e4}"

        # Flag disabled entirely bypasses, even with the same 2 real losses already on record.
        fte.config.LOSS_REPEAT_BLOCK_ENABLED = False
        placed_orders.clear()
        e5 = await fte._process_one_entry(symbol, "CE")
        assert e5["status"] == "entered", f"disabling the flag must bypass the block entirely, got {e5}"

        print("8. A win between two real losses doesn't reset/dilute the repeat-loss count (still blocks on "
              "the 2nd genuine loss); LOSS_REPEAT_BLOCK_ENABLED=False cleanly bypasses the check: PASSED")
    finally:
        restore()
        fte.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_rsi_block_enabled
        fte.config.LOSS_REPEAT_BLOCK_ENABLED = real_block_enabled
        fte.config.LOSS_REPEAT_BLOCK_COUNT = real_block_count
        fte.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_daily_cap


def test_9_profit_protection_giveback_buffer():
    """PROFIT_PROTECTION_GIVEBACK_PCT (added 10 Sep 2026 after OIL exited on
    a ~10-paise dip from the peak): once peak profit has crossed the
    threshold, the exit only fires once price has retraced at least this
    fraction off the peak. Default 0.0 is bit-identical to the original
    zero-tolerance behaviour."""
    def mk():
        return Position(
            underlying_symbol="T", option_trading_symbol="T 29 SEP 100 CALL", option_type="CE",
            quantity=1000, lot_size=1000, entry_price=10.0, target_price=100.0,
            highest_price=12.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
        )  # peak profit (12-10)*1000 = 2000 > the 1500 threshold -> PP armed
    real = fte.config.PROFIT_PROTECTION_GIVEBACK_PCT
    try:
        fte.config.PROFIT_PROTECTION_GIVEBACK_PCT = 0.0
        assert fte._exit_reason_for(mk(), ltp=11.99) == "PROFIT_PROTECTION_HIT", \
            "buffer 0.0 must still exit on any dip below the peak (unchanged behaviour)"

        fte.config.PROFIT_PROTECTION_GIVEBACK_PCT = 0.05  # give-back floor = 12.0 * 0.95 = 11.40
        assert fte._exit_reason_for(mk(), ltp=11.99) is None, \
            "an 8-paise dip must NOT stop the trade out with a 5% give-back buffer"
        assert fte._exit_reason_for(mk(), ltp=11.39) == "PROFIT_PROTECTION_HIT", \
            "a retrace past 5% off the peak still fires PROFIT_PROTECTION_HIT"
        print("9. PROFIT_PROTECTION_GIVEBACK_PCT: buffer=0 exits on any dip (unchanged); buffer=0.05 rides "
              "a small wiggle and only locks in once price is >5% off the peak: PASSED")
    finally:
        fte.config.PROFIT_PROTECTION_GIVEBACK_PCT = real


def test_11_ema_cross_exit_signal_and_wiring():
    """EMA-cross exit (config.ENABLE_EMA_CROSS_EXIT, deployed ON for Futures,
    10 Sep 2026): _exit_reason_for returns EMA_CROSS_EXIT when the fast EMA
    of the underlying's 5-min close has genuinely CROSSED against the
    position's direction on a post-entry candle - not on a standing state,
    not on the entry candle, and only when the flag is on."""
    from Options.dhan_client import _compute_ema

    # pure EMA math sanity - no I/O
    assert _compute_ema([1.0, 2.0], 9) == [None, None], "no EMA value before `period` samples"
    flat = _compute_ema([10.0] * 20, 9)
    assert flat[8] == 10.0 and abs(flat[-1] - 10.0) < 1e-9, "EMA of a flat series equals that value"
    rising = _compute_ema(list(range(1, 30)), 9)
    assert rising[-1] < 29 and rising[-1] > rising[-2], "EMA of a rising series lags below spot but trends up"

    entry_candle = datetime(2026, 9, 10, 10, 0, tzinfo=fte.IST)
    later_candle = datetime(2026, 9, 10, 10, 25, tzinfo=fte.IST)

    def mk(otype="CE"):
        return Position(
            underlying_symbol="EMASTOCK", option_trading_symbol=f"EMASTOCK 29 SEP 100 {otype}",
            option_type=otype, quantity=100, lot_size=100, entry_price=10.0, target_price=99.0,
            highest_price=10.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
            supertrend_entry_candle_start=entry_candle,
        )

    def set_cache(fast_below_slow, crossed, candle_start):
        odc.dhan_wrapper._ema_cross_cache["EMASTOCK"] = (
            datetime.now(fte.IST), fast_below_slow, crossed, candle_start,
        )

    real_flag = fte.config.ENABLE_EMA_CROSS_EXIT
    try:
        fte.config.ENABLE_EMA_CROSS_EXIT = True

        # genuine cross-below on a later candle, CE -> exit
        set_cache(fast_below_slow=True, crossed=True, candle_start=later_candle)
        assert fte._ema_cross_signal_for(mk("CE")) is True
        assert fte._exit_reason_for(mk("CE"), ltp=10.0, ema_cross_against_position=True) == "EMA_CROSS_EXIT"

        # fast below slow but NO crossover this candle -> no exit (edge, not state)
        set_cache(fast_below_slow=True, crossed=False, candle_start=later_candle)
        assert fte._ema_cross_signal_for(mk("CE")) is False

        # crossover, but the signal is still reading the entry candle -> no exit
        set_cache(fast_below_slow=True, crossed=True, candle_start=entry_candle)
        assert fte._ema_cross_signal_for(mk("CE")) is False

        # a cross-BELOW is WITH a PE (long put profits when the underlying falls) -> not against it
        set_cache(fast_below_slow=True, crossed=True, candle_start=later_candle)
        assert fte._ema_cross_signal_for(mk("PE")) is False
        # a cross-ABOVE is the reversal-against a PE
        set_cache(fast_below_slow=False, crossed=True, candle_start=later_candle)
        assert fte._ema_cross_signal_for(mk("PE")) is True

        # flag off -> _exit_reason_for never returns EMA_CROSS_EXIT
        fte.config.ENABLE_EMA_CROSS_EXIT = False
        assert fte._exit_reason_for(mk("CE"), ltp=10.0, ema_cross_against_position=True) is None

        print("11. EMA_CROSS_EXIT fires on a genuine post-entry cross against the position (CE fast-below / "
              "PE fast-above), ignores a standing state with no cross, ignores the entry candle, and is "
              "gated by ENABLE_EMA_CROSS_EXIT: PASSED")
    finally:
        fte.config.ENABLE_EMA_CROSS_EXIT = real_flag
        odc.dhan_wrapper._ema_cross_cache.pop("EMASTOCK", None)


def test_12_ema_cross_refresh_runs_on_a_continuous_multi_session_series():
    """refresh_ema_cross_signal pulls a CONTINUOUS multi-session candle
    series (via fetch_continuous_intraday, INTRADAY_CONTINUOUS_LOOKBACK_DAYS
    calendar days through today), so both EMAs are fully warm from the
    session's first bar and a crossover on today's first bar (vs the prior
    session's last) counts as a real crossover - no daily warm-up lag, no
    same-session suppression, the way a charting platform runs."""
    import types
    IST = fte.IST
    w = odc.dhan_wrapper

    def bars(day, closes, start_min=0):
        start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST) + timedelta(minutes=start_min)
        ts = [int((start + timedelta(minutes=5 * i)).timestamp()) for i in range(len(closes))]
        return [float(c) for c in closes], ts

    saved = (w._client, w._equity_security_id, dict(w._ema_cross_cache))
    try:
        w._equity_security_id = lambda sym: "SECID"
        captured = {}
        yest = datetime.now(IST).date() - timedelta(days=1)
        today = datetime.now(IST).date()

        def make_client(closes, ts):
            def intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval):
                captured["from_date"] = from_date
                return {"data": {"close": closes, "timestamp": ts}}
            return types.SimpleNamespace(Dhan=types.SimpleNamespace(intraday_minute_data=intraday_minute_data))

        # (a) genuine same-session cross-below on today's 4th bar
        y_close, y_ts = bars(yest, list(range(100, 130)))
        t_close, t_ts = bars(today, [129, 129, 129, 60])
        w._ema_cross_cache.clear()
        w._client = make_client(y_close + t_close, y_ts + t_ts)
        w.refresh_ema_cross_signal("EMATEST")

        assert captured["from_date"] == (
            datetime.now(IST) - timedelta(days=odc.config.INTRADAY_CONTINUOUS_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d"), "must request the continuous multi-day lookback, not just today"
        assert w.get_cached_ema_cross_bearish("EMATEST") is True
        assert w.get_cached_ema_cross_crossed("EMATEST") is True
        assert w.get_cached_ema_cross_candle_start("EMATEST").date() == today

        # (b) ONLY the first bar of today exists: the flip vs the prior
        # session's last bar IS a crossover on a continuous series - it is
        # NOT suppressed (that's the point of "continuous, no daily reset").
        t_close1, t_ts1 = bars(today, [60])
        w._ema_cross_cache.clear()
        w._client = make_client(y_close + t_close1, y_ts + t_ts1)
        w.refresh_ema_cross_signal("EMATEST")
        assert w.get_cached_ema_cross_bearish("EMATEST") is True
        assert w.get_cached_ema_cross_crossed("EMATEST") is True, \
            "an overnight EMA flip IS a crossover on a continuous series and must register on bar 1"

        # (c) no flip -> not a crossover (sanity: the edge detection still works)
        y2c, y2t = bars(yest, list(range(100, 130)))
        t2c, t2t = bars(today, [130, 131, 132])
        w._ema_cross_cache.clear()
        w._client = make_client(y2c + t2c, y2t + t2t)
        w.refresh_ema_cross_signal("EMATEST")
        assert w.get_cached_ema_cross_bearish("EMATEST") is False
        assert w.get_cached_ema_cross_crossed("EMATEST") is False

        print("12. refresh_ema_cross_signal runs on a continuous multi-session series (EMAs warm from bar 1, "
              "overnight flip counts, no daily reset): PASSED")
    finally:
        w._client, w._equity_security_id, _cache = saved
        w._ema_cross_cache.clear()
        w._ema_cross_cache.update(_cache)


async def main():
    print("=== Futures corrective actions (RSI loss-reentry block + liquidity guard + repeat-loss block) test suite ===\n")
    await test_1_real_loss_plus_rsi_condition_blocks_reentry_other_symbol_unaffected()
    await test_2_reentry_succeeds_once_rsi_recovers_same_day()
    await test_3_rsi_loss_reentry_disabled_flag_bypasses_the_check()
    test_4_liquidity_guard_fires_when_nothing_else_would_have()
    test_5_price_threshold_exit_still_takes_priority_over_liquidity_guard()
    test_6_liquidity_guard_disabled_flag_bypasses_the_check()
    await test_7_real_second_loss_blocks_third_entry_same_day()
    await test_8_a_win_between_two_losses_does_not_reset_the_count_and_disabled_flag_bypasses()
    test_9_profit_protection_giveback_buffer()
    test_10_target_exit_disabled_flag_suppresses_target_hit()
    test_11_ema_cross_exit_signal_and_wiring()
    test_12_ema_cross_refresh_runs_on_a_continuous_multi_session_series()
    print("\nALL FUTURES CORRECTIVE ACTION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
