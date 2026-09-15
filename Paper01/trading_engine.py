"""
Paper01's entry/exit orchestration - built almost entirely out of
Options.trading_engine's own pure/read-only functions, reused directly
rather than reimplemented, so "exact same rules as Options" holds by
construction (see this package's config.py docstring) and can never
silently drift as Options' own thresholds are tuned later.

Reused as-is from Options.trading_engine: rank_and_pick_top_stocks,
_exit_reason_for (the full current MAX_LOSS_HIT -> TARGET_HIT ->
PROFIT_PROTECTION_HIT -> TRAILING_SL_HIT/STOP_LOSS_HIT -> SUPERTREND_EXIT
-> EMA_CROSS_EXIT -> LIQUIDITY_GUARD_ZERO_VOLUME ladder),
_supertrend_signal_for, _ema_cross_signal_for, _get_ltp,
_capture_supertrend_entry_candle, is_past_square_off_time,
_todays_square_off_time, _parse_hhmm_today, _now_ist. All of these read
Options.config internally, so Paper01 automatically inherits Options'
live threshold values with zero copying.

Guards replicated from Options.trading_engine._process_one_entry, in the
SAME order, but counted against Paper01's OWN daily log (paper_position_
store.count_opened_today/loss_exit_count_today) instead of the real
trade_history log - a real Options trade for a symbol never affects
Paper01's guards for that symbol, and vice versa:
  1. MAX_DAILY_ENTRIES_PER_SYMBOL (Options.config value, own counter)
  2. RSI loss-reentry block (Options.config toggle, own loss counter,
     dhan_wrapper.is_rsi_loss_reentry_blocked - a generic, read-only,
     underlying-keyed signal, safe to reuse as-is)
  3. LOSS_REPEAT_BLOCK (Options.config toggle/count/reasons, own loss
     counter)
  4. Paper01's own reserve_symbol (own capacity pool)

Intentionally SKIPPED (confirmed with the user - none of these apply to
a paper trade):
  - cross_strategy_registry.try_claim - exists to stop two REAL
    strategies both risking capital on the same underlying at once.
    Paper01 risks no capital, so claiming would only risk momentarily
    blocking a real strategy's own entry for no benefit. Paper01 will
    happily paper-trade a stock Options/Futures/Luxury/Swing already
    hold a real position in, and vice versa.
  - dhan_wrapper.has_open_position_for_underlying - no real broker
    position exists to collide with.
  - FUNDS_CHECK_ENABLED - no real capital to check margin against.
  - BROKER_STOP_LOSS_ENABLED - no real order exists to attach a broker-
    side SL-L to; the software exit ladder (_exit_reason_for) is the only
    enforcement mechanism, exactly as it already is for a real position
    whenever that flag is off.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from Options import config as options_config
from Options.dhan_client import dhan_wrapper
from Options.position_store import Position
from Options.trading_engine import (
    _capture_supertrend_entry_candle,
    _ema_cross_signal_for,
    _exit_reason_for,
    _get_ltp,
    _now_ist,
    _parse_hhmm_today,
    _supertrend_signal_for,
    _todays_square_off_time,
)

from .position_store import paper_position_store as store

logger = logging.getLogger("paper01_trading_engine")


# --------------------------------------------------------------------------- #
# Step 1: entry
# --------------------------------------------------------------------------- #
async def _process_one_paper_entry(symbol: str, option_type: str) -> dict:
    loop = asyncio.get_running_loop()

    entries_today = await store.count_opened_today(symbol)
    if entries_today >= options_config.MAX_DAILY_ENTRIES_PER_SYMBOL:
        logger.info(
            "%s: paper-skipped - already entered %d time(s) today, at the daily cap of %d",
            symbol, entries_today, options_config.MAX_DAILY_ENTRIES_PER_SYMBOL,
        )
        return {"symbol": symbol, "status": "skipped", "reason": "daily_reentry_cap_reached"}

    if options_config.ENABLE_RSI_LOSS_REENTRY_BLOCK:
        loss_hits_today = await store.loss_exit_count_today(symbol, ("MAX_LOSS_HIT",))
        if loss_hits_today >= 1:
            blocked = await loop.run_in_executor(None, dhan_wrapper.is_rsi_loss_reentry_blocked, symbol)
            if blocked:
                reason = dhan_wrapper.rsi_loss_reentry_reason(symbol)
                logger.info("%s: paper-skipped - MAX_LOSS_HIT already hit today and RSI is %s - "
                            "not re-entering until it recovers", symbol, reason)
                return {"symbol": symbol, "status": "skipped", "reason": "rsi_loss_reentry_block_active"}

    if options_config.LOSS_REPEAT_BLOCK_ENABLED:
        loss_count = await store.loss_exit_count_today(symbol, options_config.LOSS_REPEAT_BLOCK_EXIT_REASONS)
        if loss_count >= options_config.LOSS_REPEAT_BLOCK_COUNT:
            logger.info(
                "%s: paper-skipped - already hit a loss-based exit %d time(s) today (limit %d)",
                symbol, loss_count, options_config.LOSS_REPEAT_BLOCK_COUNT,
            )
            return {"symbol": symbol, "status": "skipped", "reason": "loss_repeat_block_active"}

    if not await store.reserve_symbol(symbol, option_type):
        logger.info("%s: paper-skipped - already open/in-flight, or no Paper01 capacity", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

    try:
        return await _enter_single_paper_position(symbol, option_type)
    except Exception as exc:  # noqa: BLE001
        await store.release_symbol(symbol)
        logger.exception("Failed to enter paper position for %s", symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc)}


async def enter_paper_positions_for_stocks(ranked: list[tuple[str, float]], option_type: str) -> list[dict]:
    return await asyncio.gather(*[_process_one_paper_entry(sym, option_type) for sym, _ in ranked])


async def _enter_single_paper_position(symbol: str, option_type: str) -> dict:
    loop = asyncio.get_running_loop()

    atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, option_type)

    if atm.expiry_date == _now_ist().date():
        await store.release_symbol(symbol)
        logger.info("%s: paper-skipped - %s expires today and no later expiry is available yet",
                    symbol, atm.trading_symbol)
        return {"symbol": symbol, "status": "skipped_expiry_day", "option_trading_symbol": atm.trading_symbol}

    quantity = atm.lot_size * options_config.QUANTITY_LOTS

    # Real-time WS ticks for this paper position too - the shared feed
    # supports multiple subscribers, so this doesn't take anything away
    # from Options' own subscriptions.
    await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, atm.trading_symbol)

    entry_price = await _get_ltp(atm.trading_symbol)
    if not entry_price:
        entry_price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
    if not entry_price:
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, atm.trading_symbol)
        await store.release_symbol(symbol)
        return {"symbol": symbol, "status": "error", "reason": "no_ltp_available"}

    entry_candle_start = await _capture_supertrend_entry_candle(loop, symbol)

    position = Position(
        underlying_symbol=symbol,
        option_trading_symbol=atm.trading_symbol,
        option_type=atm.option_type,
        quantity=quantity,
        lot_size=atm.lot_size,
        entry_price=entry_price,
        highest_price=entry_price,
        target_price=entry_price * (1 + options_config.TARGET_PCT),
        hard_stop_loss=entry_price * (1 - options_config.STOP_LOSS_PCT),
        order_id="",
        product_type="PAPER",
        supertrend_entry_candle_start=entry_candle_start,
    )
    await store.add_position(position)

    return {
        "symbol": symbol, "status": "paper_entered", "option_trading_symbol": atm.trading_symbol,
        "quantity": quantity, "entry_price": entry_price,
    }


# --------------------------------------------------------------------------- #
# Step 2: monitoring / exits
# --------------------------------------------------------------------------- #
async def _exit_paper_position(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
    await store.close_position(symbol, exit_price, reason)


async def _check_one_paper_position(symbol: str, position: Position) -> None:
    try:
        ltp = await _get_ltp(position.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for paper position %s", position.option_trading_symbol)
        return
    if ltp is None:
        return

    await store.update_highest_price(symbol, ltp)

    loop = asyncio.get_running_loop()
    supertrend_against_position = False
    if options_config.ENABLE_SUPERTREND_EXIT:
        await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, position.underlying_symbol)
        supertrend_against_position = _supertrend_signal_for(position)

    ema_cross_against_position = False
    if options_config.ENABLE_EMA_CROSS_EXIT:
        await loop.run_in_executor(None, dhan_wrapper.refresh_ema_cross_signal, position.underlying_symbol)
        ema_cross_against_position = _ema_cross_signal_for(position)

    liquidity_guard_triggered = False
    if options_config.LIQUIDITY_GUARD_ENABLED:
        await loop.run_in_executor(None, dhan_wrapper.refresh_liquidity_signal, position.option_trading_symbol)
        liquidity_guard_triggered = bool(dhan_wrapper.get_cached_illiquid(position.option_trading_symbol))

    reason = _exit_reason_for(
        position, ltp, supertrend_against_position, liquidity_guard_triggered, ema_cross_against_position,
    )
    if reason:
        await _exit_paper_position(symbol, position, ltp, reason)


async def on_paper_price_tick(trading_symbol: str, ltp: float) -> None:
    """Event-driven mirror of Options.trading_engine.on_price_tick, fired
    from the same shared WebSocket feed thread - fast reactive exits for
    paper positions too, not just the REST-polled fallback below."""
    try:
        match = next(
            ((sym, pos) for sym, pos in store.live_positions.items()
             if pos.option_trading_symbol == trading_symbol),
            None,
        )
        if not match:
            return
        symbol, position = match

        await store.update_highest_price(symbol, ltp)

        supertrend_against_position = options_config.ENABLE_SUPERTREND_EXIT and _supertrend_signal_for(position)
        ema_cross_against_position = options_config.ENABLE_EMA_CROSS_EXIT and _ema_cross_signal_for(position)
        liquidity_guard_triggered = options_config.LIQUIDITY_GUARD_ENABLED and bool(
            dhan_wrapper.get_cached_illiquid(position.option_trading_symbol)
        )

        reason = _exit_reason_for(
            position, ltp, supertrend_against_position, liquidity_guard_triggered, ema_cross_against_position,
        )
        if reason:
            await _exit_paper_position(symbol, position, ltp, reason)
    except Exception:  # noqa: BLE001
        logger.exception("on_paper_price_tick failed for %s", trading_symbol)


async def _square_off_all_paper(reason: str) -> None:
    positions = list(store.live_positions.items())
    for symbol, position in positions:
        try:
            ltp = await _get_ltp(position.option_trading_symbol)
            if ltp is None:
                ltp = position.highest_price
            await _exit_paper_position(symbol, position, ltp, reason)
        except Exception:  # noqa: BLE001
            logger.exception("Paper square-off failed for %s", symbol)


async def paper_monitor_loop() -> None:
    """Poll-loop fallback/heartbeat, same interval Options itself uses
    (Options.config.MONITOR_INTERVAL_SECONDS) - the WS tick path above
    (on_paper_price_tick) handles most exits reactively; this keeps
    Supertrend/EMA/liquidity caches warm and catches anything the feed
    missed, plus the one daily EOD square-off, mirroring
    Options.trading_engine.monitor_loop's own square-off timing exactly."""
    logger.info("Paper01 monitor loop started (PAPER ONLY - no real orders will ever be placed).")
    squared_off_today_for: set = set()
    while True:
        try:
            await store.maybe_reset_for_new_day()
            cutoff = _todays_square_off_time()
            if cutoff is not None:
                now = _now_ist()
                square_off_at = _parse_hhmm_today(cutoff)
                today_key = now.date()
                if now >= square_off_at and today_key not in squared_off_today_for:
                    await _square_off_all_paper("EOD_SQUARE_OFF")
                    squared_off_today_for.add(today_key)
                elif now < square_off_at:
                    positions = list(store.live_positions.items())
                    await asyncio.gather(*[_check_one_paper_position(sym, pos) for sym, pos in positions])
            else:
                positions = list(store.live_positions.items())
                await asyncio.gather(*[_check_one_paper_position(sym, pos) for sym, pos in positions])
        except Exception:  # noqa: BLE001
            logger.exception("Error in Paper01 monitor loop tick")
        await asyncio.sleep(options_config.MONITOR_INTERVAL_SECONDS)
