"""
Tests for Options' own wiring of three corrective actions ported from
Luxury on 10 Sep 2026 (user request: "make Options have similar rule
set and guard rails as Luxury have, add and deploy also"): the same-day
RSI-gated loss re-entry block, the liquidity guard, and the repeat-loss
same-day block.

The time-based LOSS_COOLDOWN_ENABLED/LOSS_COOLDOWN_MINUTES mechanism
this file used to test was REMOVED on 11 Sep 2026 (user request: "Remove
this cooldown period logic from everywhere and all strategies, instead
create another global common function which checks if RSI...") after a
real INDUSTOWER alert was skipped 1 minute short of its 20-minute
window regardless of whether the stock had recovered. See Options/
config.py's "Same-day RSI-gated loss re-entry block" comment and
Options/dhan_client.py's refresh_rsi_signal/is_rsi_loss_reentry_blocked
for the replacement mechanism - its own RSI computation correctness
(overbought/falling detection, continuous-candle fetch) is covered by
tests/test_rsi_loss_reentry_block.py, so THIS file only tests OPTIONS'
OWN _process_one_entry wiring (does it only look at RSI after a real
loss, does it respect the flag, is the effect a re-checkable condition
rather than a permanent block) by mocking is_rsi_loss_reentry_blocked
directly to a controlled verdict.

The other underlying shared mechanisms (trade_history.loss_exit_count_
today, dhan_client.refresh_liquidity_signal/get_cached_illiquid) are
already fully tested against Luxury's own strategy tag in tests/
test_luxury_corrective_actions.py - those are pure, strategy-agnostic
functions, so this file does NOT re-test their internal logic (that
would just be duplicate coverage of the identical code path under a
different string constant). What IS new and needs its own coverage is
OPTIONS' OWN integration wiring - _process_one_entry's RSI-loss-reentry/
repeat-block checks and _exit_reason_for's liquidity_guard_triggered
parameter - since each package keeps its own copy of trading_engine.py,
so a wiring mistake in one package's own copy wouldn't be caught by the
other's tests.

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

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_options_corrective_actions_test_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.position_store as ops
import Options.trading_engine as ote
from Options.position_store import Position
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
    # Default: never blocks (tests that specifically exercise the RSI-loss-
    # reentry wiring override this to a controlled verdict; every other
    # test in this file never wants a real network call here).
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
# 1. Same-day RSI-gated loss re-entry block - Options' own
#    _process_one_entry wiring (replaces the old time-based cooldown)
# --------------------------------------------------------------------- #

async def test_1_real_loss_plus_rsi_condition_blocks_reentry_other_symbol_unaffected():
    store = ops.PositionStore()
    ote.position_store = store
    real_enabled = ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = True
    restore, placed_orders = install_all_dhan_mocks()
    # RSI condition mocked as "still blocking" (overbought/falling) - the
    # underlying RSI math itself is covered by test_rsi_loss_reentry_block.py.
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: True
    odc.dhan_wrapper.get_cached_rsi = lambda underlying_symbol: 91.5
    odc.dhan_wrapper.get_cached_prev_rsi = lambda underlying_symbol: 93.0
    odc.dhan_wrapper.rsi_loss_reentry_reason = lambda underlying_symbol: "overbought"
    try:
        entry = await ote._process_one_entry("COALINDIA", "CE")
        assert entry["status"] == "entered", entry

        closed = await store.close_position("COALINDIA", 40.0, "MAX_LOSS_HIT")
        assert closed is not None and closed.exit_price == 40.0
        await asyncio.sleep(0.3)

        placed_orders.clear()
        retry = await ote._process_one_entry("COALINDIA", "CE")
        assert retry["status"] == "skipped" and retry["reason"] == "rsi_loss_reentry_block_active", retry
        assert placed_orders == [], f"an RSI-blocked symbol must place ZERO orders, got {placed_orders}"

        # A DIFFERENT symbol with NO loss today must be entirely
        # unaffected - RSI is never even consulted for it (loss_hits_
        # today short-circuits before is_rsi_loss_reentry_blocked runs),
        # even though the mock above would say "blocked" if it were asked.
        placed_orders.clear()
        other = await ote._process_one_entry("RVNL", "CE")
        assert other["status"] == "entered", f"a DIFFERENT symbol must be entirely unaffected, got {other}"

        print("1. A real MAX_LOSS_HIT loss + an RSI condition correctly blocks an immediate real re-entry "
              "attempt for the SAME symbol (zero orders placed), while a different symbol with no loss "
              "today is entirely unaffected: PASSED")
    finally:
        restore()
        ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_enabled


async def test_2_reentry_succeeds_once_rsi_recovers_same_day():
    store = ops.PositionStore()
    ote.position_store = store
    real_enabled = ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = True
    restore, placed_orders = install_all_dhan_mocks()
    # RSI condition mocked as "recovered" (neither overbought nor falling).
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: False
    try:
        trade_history.append_jsonl("real_trades", {
            "strategy": "Options", "underlying_symbol": "KFINTECH", "pnl": -900.0,
            "exit_reason": "MAX_LOSS_HIT", "closed_at": (datetime.now() - timedelta(minutes=2)).isoformat(),
        })
        result = await ote._process_one_entry("KFINTECH", "CE")
        assert result["status"] == "entered", \
            f"once RSI is no longer overbought/falling, the SAME symbol must re-enter normally " \
            f"the SAME day, even though it lost minutes ago - this is a condition, not a timer, got {result}"
        print("2. Once RSI recovers (neither overbought nor falling) the SAME day, the same symbol "
              "re-enters normally even though it lost earlier today - a re-checkable condition, "
              "not a permanent same-day block: PASSED")
    finally:
        restore()
        ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_enabled


async def test_3_rsi_loss_reentry_disabled_flag_bypasses_the_check():
    store = ops.PositionStore()
    ote.position_store = store
    real_enabled = ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    restore, placed_orders = install_all_dhan_mocks()
    # Even mocked as "still blocking" - the flag being off must never even ask.
    odc.dhan_wrapper.is_rsi_loss_reentry_blocked = lambda underlying_symbol: True
    try:
        trade_history.append_jsonl("real_trades", {
            "strategy": "Options", "underlying_symbol": "NBCC", "pnl": -50.0,
            "exit_reason": "MAX_LOSS_HIT", "closed_at": datetime.now().isoformat(),
        })
        result = await ote._process_one_entry("NBCC", "CE")
        assert result["status"] == "entered", \
            f"disabling the flag must bypass the RSI-loss-reentry check entirely, got {result}"
        print("3. ENABLE_RSI_LOSS_REENTRY_BLOCK=False cleanly bypasses the check even with a real loss "
              "on record and RSI mocked as still blocking: PASSED")
    finally:
        restore()
        ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_enabled


# --------------------------------------------------------------------- #
# 2. Liquidity guard - Options' own _exit_reason_for wiring
# --------------------------------------------------------------------- #

def test_4_liquidity_guard_fires_when_nothing_else_would_have():
    pos = Position(
        underlying_symbol="CHOLAFIN", option_trading_symbol="CHOLAFIN 29 SEP 1840 CALL",
        option_type="CE", quantity=625, lot_size=625, entry_price=56.35, target_price=100.0,
        highest_price=56.35, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    reason = ote._exit_reason_for(pos, ltp=56.25, supertrend_against_position=False, liquidity_guard_triggered=True)
    assert reason == "LIQUIDITY_GUARD_ZERO_VOLUME", reason
    print("4. The liquidity guard correctly fires on its own when no price threshold is anywhere close, "
          "the exact real CHOLAFIN shape (price ~flat, contract gone quiet): PASSED")


def test_5_price_threshold_exit_still_takes_priority_over_liquidity_guard():
    pos = Position(
        underlying_symbol="TESTSTOCK", option_trading_symbol="TESTSTOCK 29 SEP 100 CALL",
        option_type="CE", quantity=100, lot_size=100, entry_price=10.0, target_price=12.0,
        highest_price=10.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    real = ote.config.ENABLE_TARGET_EXIT
    ote.config.ENABLE_TARGET_EXIT = True  # this test is specifically about the fixed target
    try:
        reason = ote._exit_reason_for(pos, ltp=12.5, supertrend_against_position=False, liquidity_guard_triggered=True)
        assert reason == "TARGET_HIT", reason
        print("5. A genuine price-threshold exit (e.g. TARGET_HIT) still takes priority over the liquidity guard "
              "when both happen to be true on the same tick: PASSED")
    finally:
        ote.config.ENABLE_TARGET_EXIT = real


def test_5b_target_exit_disabled_flag_suppresses_target_hit_only():
    """ENABLE_TARGET_EXIT (default on for Options; deployed off for Futures,
    10 Sep 2026). Off -> a position at/above target_price is not closed for
    TARGET_HIT, but MAX_LOSS_HIT / PROFIT_PROTECTION_HIT / SL are untouched."""
    real = ote.config.ENABLE_TARGET_EXIT
    try:
        ote.config.ENABLE_TARGET_EXIT = False
        riding = Position(
            underlying_symbol="RUNNER", option_trading_symbol="RUNNER 29 SEP 100 CALL",
            option_type="CE", quantity=100, lot_size=100, entry_price=10.0, target_price=12.0,
            highest_price=13.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
        )
        assert ote._exit_reason_for(riding, ltp=13.0) is None, \
            "flag off: exceeding target_price must not trigger an exit"
        ote.config.ENABLE_TARGET_EXIT = True
        assert ote._exit_reason_for(riding, ltp=13.0) == "TARGET_HIT"

        ote.config.ENABLE_TARGET_EXIT = False
        # quantity=10000 so the same $2 move (entry 10.0 -> ltp 8.0) produces
        # a Rs 20,000 loss - well past MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF's
        # own current value (raised 1200->1500->4500, 12 Sep 2026) regardless
        # of which side of the cutoff this test happens to run on.
        losing = Position(
            underlying_symbol="LOSER", option_trading_symbol="LOSER 29 SEP 100 CALL",
            option_type="CE", quantity=10000, lot_size=10000, entry_price=10.0, target_price=12.0,
            highest_price=10.0, hard_stop_loss=8.4, order_id="X", product_type="MARGIN",
        )
        assert ote._exit_reason_for(losing, ltp=8.0) == "MAX_LOSS_HIT", \
            "disabling the target exit must not affect the loss-side checks"
        print("5b. ENABLE_TARGET_EXIT=False suppresses TARGET_HIT only; MAX_LOSS_HIT still fires: PASSED")
    finally:
        ote.config.ENABLE_TARGET_EXIT = real


def test_5c_ema_cross_exit_gated_by_its_own_flag():
    """ENABLE_EMA_CROSS_EXIT ships off in code (Options/config.py's own
    default) - turned on in production 11 Sep 2026 alongside Luxury (was
    Futures-only before that). Flag forced explicitly both ways here
    (save/restore) rather than assumed from whatever's currently
    deployed, so this stays meaningful regardless: off must ignore even
    a genuine crossed-against-position signal, on must fire EMA_CROSS_EXIT."""
    pos = Position(
        underlying_symbol="EMASTOCK", option_trading_symbol="EMASTOCK 29 SEP 100 CALL",
        option_type="CE", quantity=100, lot_size=100, entry_price=10.0, target_price=99.0,
        highest_price=10.0, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    real = ote.config.ENABLE_EMA_CROSS_EXIT
    try:
        ote.config.ENABLE_EMA_CROSS_EXIT = False
        assert ote._exit_reason_for(pos, ltp=10.0, ema_cross_against_position=True) is None, \
            "flag off must ignore even a genuine crossed-against-position signal"
        ote.config.ENABLE_EMA_CROSS_EXIT = True
        assert ote._exit_reason_for(pos, ltp=10.0, ema_cross_against_position=True) == "EMA_CROSS_EXIT"
        print("5c. Options' EMA-cross exit is correctly gated by ENABLE_EMA_CROSS_EXIT - ignored off, "
              "fires EMA_CROSS_EXIT on: PASSED")
    finally:
        ote.config.ENABLE_EMA_CROSS_EXIT = real


def test_6_liquidity_guard_disabled_flag_bypasses_the_check():
    pos = Position(
        underlying_symbol="CHOLAFIN", option_trading_symbol="CHOLAFIN 29 SEP 1840 CALL",
        option_type="CE", quantity=625, lot_size=625, entry_price=56.35, target_price=100.0,
        highest_price=56.35, hard_stop_loss=1.0, order_id="X", product_type="MARGIN",
    )
    real_enabled = ote.config.LIQUIDITY_GUARD_ENABLED
    ote.config.LIQUIDITY_GUARD_ENABLED = False
    try:
        reason = ote._exit_reason_for(pos, ltp=56.25, supertrend_against_position=False, liquidity_guard_triggered=True)
        assert reason is None, f"disabling the flag must suppress the exit even when the signal itself fired, got {reason}"
        print("6. LIQUIDITY_GUARD_ENABLED=False cleanly suppresses the exit even when the underlying signal fired: PASSED")
    finally:
        ote.config.LIQUIDITY_GUARD_ENABLED = real_enabled


# --------------------------------------------------------------------- #
# 3. Repeat-loss same-day block - Options' own _process_one_entry wiring
# --------------------------------------------------------------------- #

async def test_7_real_second_loss_blocks_third_entry_same_day():
    store = ops.PositionStore()
    ote.position_store = store
    real_rsi_block_enabled = ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    real_block_enabled, real_block_count = ote.config.LOSS_REPEAT_BLOCK_ENABLED, ote.config.LOSS_REPEAT_BLOCK_COUNT
    # RSI-loss-reentry disabled here so ONLY the repeat-block feature is
    # under test - otherwise it would ALSO block the immediate retries
    # below (both fire on the same MAX_LOSS_HIT), confounding which
    # feature fired.
    ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    ote.config.LOSS_REPEAT_BLOCK_ENABLED = True
    ote.config.LOSS_REPEAT_BLOCK_COUNT = 2
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "GAIL"
        entry_1 = await ote._process_one_entry(symbol, "CE")
        assert entry_1["status"] == "entered", entry_1
        closed_1 = await store.close_position(symbol, 40.0, "MAX_LOSS_HIT")
        assert closed_1 is not None
        await asyncio.sleep(0.3)

        entry_2 = await ote._process_one_entry(symbol, "CE")
        assert entry_2["status"] == "entered", \
            f"only ONE prior loss so far - must still enter normally, got {entry_2}"
        closed_2 = await store.close_position(symbol, 41.0, "MAX_LOSS_HIT")
        assert closed_2 is not None
        await asyncio.sleep(0.3)

        placed_orders.clear()
        entry_3 = await ote._process_one_entry(symbol, "CE")
        assert entry_3["status"] == "skipped" and entry_3["reason"] == "loss_repeat_block_active", entry_3
        assert placed_orders == [], f"a repeat-loss-blocked symbol must place ZERO orders, got {placed_orders}"

        placed_orders.clear()
        other = await ote._process_one_entry("NMDC", "CE")
        assert other["status"] == "entered", f"a DIFFERENT symbol must be entirely unaffected, got {other}"

        print("7. TWO real same-day MAX_LOSS_HIT losses for the same symbol correctly block a third real "
              "entry attempt (zero orders placed) for the REST OF THE DAY, while a different symbol is "
              "entirely unaffected: PASSED")
    finally:
        restore()
        ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_rsi_block_enabled
        ote.config.LOSS_REPEAT_BLOCK_ENABLED = real_block_enabled
        ote.config.LOSS_REPEAT_BLOCK_COUNT = real_block_count


async def test_8_a_win_between_two_losses_does_not_reset_the_count_and_disabled_flag_bypasses():
    store = ops.PositionStore()
    ote.position_store = store
    real_rsi_block_enabled = ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK
    real_block_enabled, real_block_count = ote.config.LOSS_REPEAT_BLOCK_ENABLED, ote.config.LOSS_REPEAT_BLOCK_COUNT
    real_daily_cap = ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL
    ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = False
    ote.config.LOSS_REPEAT_BLOCK_ENABLED = True
    ote.config.LOSS_REPEAT_BLOCK_COUNT = 2
    # This test makes 4 real entry attempts for the SAME symbol - raised
    # so the pre-existing daily re-entry cap (default 3) doesn't fire
    # first and mask what's actually under test here.
    ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = 10
    restore, placed_orders = install_all_dhan_mocks()
    try:
        symbol = "SAIL"
        e1 = await ote._process_one_entry(symbol, "CE")
        assert e1["status"] == "entered", e1
        await store.close_position(symbol, 30.0, "MAX_LOSS_HIT")
        await asyncio.sleep(0.3)

        e2 = await ote._process_one_entry(symbol, "CE")
        assert e2["status"] == "entered", e2
        await store.close_position(symbol, 60.0, "TARGET_HIT")  # a genuine WIN in between - doesn't reset anything
        await asyncio.sleep(0.3)

        e3 = await ote._process_one_entry(symbol, "CE")
        assert e3["status"] == "entered", \
            f"still only ONE loss-designated exit so far (the win doesn't count either way) - must enter, got {e3}"
        await store.close_position(symbol, 31.0, "MAX_LOSS_HIT")  # second REAL loss
        await asyncio.sleep(0.3)

        placed_orders.clear()
        e4 = await ote._process_one_entry(symbol, "CE")
        assert e4["status"] == "skipped" and e4["reason"] == "loss_repeat_block_active", \
            f"2 genuine loss-designated exits today (the win in between doesn't dilute this) must now block, got {e4}"

        # Flag disabled entirely bypasses, even with the same 2 real losses already on record.
        ote.config.LOSS_REPEAT_BLOCK_ENABLED = False
        placed_orders.clear()
        e5 = await ote._process_one_entry(symbol, "CE")
        assert e5["status"] == "entered", f"disabling the flag must bypass the block entirely, got {e5}"

        print("8. A win between two real losses doesn't reset/dilute the repeat-loss count (still blocks on "
              "the 2nd genuine loss); LOSS_REPEAT_BLOCK_ENABLED=False cleanly bypasses the check: PASSED")
    finally:
        restore()
        ote.config.ENABLE_RSI_LOSS_REENTRY_BLOCK = real_rsi_block_enabled
        ote.config.LOSS_REPEAT_BLOCK_ENABLED = real_block_enabled
        ote.config.LOSS_REPEAT_BLOCK_COUNT = real_block_count
        ote.config.MAX_DAILY_ENTRIES_PER_SYMBOL = real_daily_cap


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
    real = ote.config.PROFIT_PROTECTION_GIVEBACK_PCT
    try:
        ote.config.PROFIT_PROTECTION_GIVEBACK_PCT = 0.0
        assert ote._exit_reason_for(mk(), ltp=11.99) == "PROFIT_PROTECTION_HIT", \
            "buffer 0.0 must still exit on any dip below the peak (unchanged behaviour)"

        ote.config.PROFIT_PROTECTION_GIVEBACK_PCT = 0.05  # give-back floor = 12.0 * 0.95 = 11.40
        assert ote._exit_reason_for(mk(), ltp=11.99) is None, \
            "an 8-paise dip must NOT stop the trade out with a 5% give-back buffer"
        assert ote._exit_reason_for(mk(), ltp=11.39) == "PROFIT_PROTECTION_HIT", \
            "a retrace past 5% off the peak still fires PROFIT_PROTECTION_HIT"
        print("9. PROFIT_PROTECTION_GIVEBACK_PCT: buffer=0 exits on any dip (unchanged); buffer=0.05 rides "
              "a small wiggle and only locks in once price is >5% off the peak: PASSED")
    finally:
        ote.config.PROFIT_PROTECTION_GIVEBACK_PCT = real


async def main():
    print("=== Options corrective actions (RSI loss-reentry block + liquidity guard + repeat-loss block) test suite ===\n")
    await test_1_real_loss_plus_rsi_condition_blocks_reentry_other_symbol_unaffected()
    await test_2_reentry_succeeds_once_rsi_recovers_same_day()
    await test_3_rsi_loss_reentry_disabled_flag_bypasses_the_check()
    test_4_liquidity_guard_fires_when_nothing_else_would_have()
    test_5_price_threshold_exit_still_takes_priority_over_liquidity_guard()
    test_5b_target_exit_disabled_flag_suppresses_target_hit_only()
    test_5c_ema_cross_exit_gated_by_its_own_flag()
    test_6_liquidity_guard_disabled_flag_bypasses_the_check()
    await test_7_real_second_loss_blocks_third_entry_same_day()
    await test_8_a_win_between_two_losses_does_not_reset_the_count_and_disabled_flag_bypasses()
    test_9_profit_protection_giveback_buffer()
    print("\nALL OPTIONS CORRECTIVE ACTION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
