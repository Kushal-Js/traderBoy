"""
Breakout-scanner paper-trading engine (added 22 Sep 2026, user request) -
a shared, generic module (like breakout_signal.py itself) so a single
flag per package can redirect EVERY breakout-scanner-sourced signal for
that package into a real-conditions PAPER simulation instead of a real
order.

*** IMPORTANT SEMANTIC, CONFIRMED WITH THE USER: this REPLACES real
trading, it does not run alongside it. *** With BREAKOUT_PAPER_MODE_
ENABLED=true for a package, its _breakout_entry_fn calls this module
INSTEAD of trading_engine._process_one_entry - not both. Since the
breakout scanner is currently the SOLE entry path for Options/Luxury/
Futures (see the b889608/e3bf5a2 commits), turning this on for a
package means that package places ZERO real orders until it's turned
back off - this is a full paper-mode switch for that package's entire
real-money trading, not an "also run a shadow copy" feature like
Paper01 (which genuinely does run alongside Options' real trading).

WHY "REAL CONDITIONS" HOLDS BY CONSTRUCTION: same reasoning Paper01's
own module docstring already establishes for Options specifically, here
generalized to Options/Luxury/Futures via a per-strategy dispatch table
(STRATEGY) instead of hardcoded imports - every entry gate and every
exit condition is the EXACT SAME real function each package's own
trading_engine.py uses for real trades (each reading that package's own
config module internally), never a re-implementation that could drift.
Paper positions are real Position objects (Options/Luxury/Futures'
OWN position_store.Position, whichever the strategy is), with
product_type="PAPER" and order_id="" - not a separate lookalike class -
so current_trailing_sl and every other Position property/method just
works, identically to a real position.

ENTRY GATES REPLICATED, same order as each package's real
_process_one_entry, counted against THIS module's own paper-only daily
counters (never the real trade_history log, and vice versa - a real
loss today never blocks a paper re-entry and vice versa):
  1. MAX_DAILY_ENTRIES_PER_SYMBOL
  2. RSI-loss-reentry block (only after a paper MAX_LOSS_HIT today)
  3. LOSS_REPEAT_BLOCK (+ trend-strength check at exactly 1 prior loss)
  4. Volume-floor gate (only for packages that have one - Options/
     Futures; getattr defaults False, matching Luxury's real absence)
  5. Capacity (this module's own paper-only pool per strategy+option_
     type, respecting MAX_LIVE_POSITIONS_CE/PE + the real opening-burst
     slot)
  6. cross_strategy_registry.try_claim/release_claim (added 23 Sep 2026,
     user request: "uses cross_strategy_registry and everything that is
     used in live for real trades" - the SAME real, process-wide registry
     Options/Futures/Luxury's real _process_one_entry claims, not a
     paper-only lookalike. Matters most in MIXED mode - one package for
     real, another on paper - so a paper leg can't simulate an entry into
     a symbol a real package has already genuinely claimed/opened, and
     vice versa. Claimed for the full duration of this function, released
     in a finally, identical scoping to the real function.
  7. dhan_wrapper.has_open_position_for_underlying (real, read-only
     broker query) - skips the paper entry if the broker already shows a
     real open FNO position for this underlying, same belt-and-suspenders
     reasoning _process_one_entry itself uses.
  8. Liquid-contract resolution (dhan_wrapper.get_liquid_atm_option -
     the real function, resolves a real contract, places no order)
  9. Option-liquidity entry gate (LIQUIDITY_ENTRY_GATE_ENABLED -
     reversal_filters.check_option_liquidity on the resolved contract;
     defaults true in all 3 packages, added here explicitly - Paper01's
     own template omits it, but "real conditions" for a data-quality
     gate that's live-on-by-default is worth the one extra REST call)
  10. FUNDS_CHECK_ENABLED (added 23 Sep 2026, same user request) - the
      SAME real fund_allocation.has_sufficient_bucket_funds check against
      the real account's real available balance (a live, read-only
      margin-calculator + fund-limit query - no order is placed either
      way), so a paper entry that the real account genuinely couldn't
      afford is skipped exactly like a real one would be, not silently
      allowed through on paper money.

NOT REPLICATED, and cannot be by construction - both place a REAL order
at the broker, which contradicts "paper" outright:
  - BROKER_STOP_LOSS_ENABLED's resting SL-L order. A paper position's
    protection is the same poll/tick-driven _exit_reason_for check that
    already backstops every real position even when ITS OWN broker-side
    stop failed to place - not a weaker guarantee than the real path ever
    silently falls back to.
  - Real order placement itself (place_market_order) - the entire point
    of paper mode.

INTENTIONALLY SKIPPED, same rationale Paper01 already established (no
real capital at risk to protect): cross_strategy_registry.try_claim,
has_open_position_for_underlying, FUNDS_CHECK_ENABLED,
BROKER_STOP_LOSS_ENABLED. The pre-entry checks that DO still apply
(trading windows, allowed time, square-off, gap-down CE delay) are
checked by each package's own _breakout_entry_fn BEFORE this module is
even called - see that function's own docstring.

MONITORING/EXITS: one shared loop (paper_engine_monitor_loop, started
once from main.py's own lifespan, like UniverseDispatcher) polls every
open paper position across all 3 packages, using each position's OWN
strategy's real _get_ltp/_supertrend_signal_for/_ema_cross_signal_for/
_exit_reason_for - the exact same functions, same priority order
(MAX_LOSS_HIT -> TARGET_HIT -> PROFIT_PROTECTION_HIT -> TRAILING_SL_HIT/
STOP_LOSS_HIT -> SUPERTREND_EXIT -> EMA_CROSS_EXIT ->
LIQUIDITY_GUARD_ZERO_VOLUME) real trades use.

LOGGING: on close, one record per trade to
history/<date>_breakout_paper_trades.log - the SAME field shape
trade_history.record_closed_trade produces for a real trade (strategy,
underlying_symbol, option_trading_symbol, option_type, quantity,
entry_price, exit_price, exit_reason, pnl, opened_at, closed_at,
order_id, logged_at), plus "mode": "paper" and "signal_source":
"breakout_scanner" so it's never confused with a real trade record but
is otherwise directly comparable/diffable against one.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional

from Options.dhan_client import dhan_wrapper
import cross_strategy_registry
import fund_allocation
import reversal_filters
import trade_history

logger = logging.getLogger("breakout_paper_engine")

PAPER_TRADES_LOG_NAME = "breakout_paper_trades"


@dataclass
class _StrategyHooks:
    position_cls: type
    cfg: object
    get_ltp: Callable
    supertrend_signal_for: Callable
    ema_cross_signal_for: Callable
    underlying_move_confirms_exit: Callable
    exit_reason_for: Callable
    capture_supertrend_entry_candle: Callable


def _build_hooks() -> dict[str, _StrategyHooks]:
    """Deferred import (avoids a circular import at module load time -
    Options/Luxury/Futures' own *_main.py modules import breakout_signal.py
    which is imported early; this module is only imported by them at
    lifespan-start time, well after all 3 packages' own modules exist)."""
    from Options import config as options_cfg
    from Options import trading_engine as options_te
    from Options.position_store import Position as OptionsPosition
    from Luxury import config as luxury_cfg
    from Luxury import trading_engine as luxury_te
    from Luxury.position_store import Position as LuxuryPosition
    from Futures import config as futures_cfg
    from Futures import trading_engine as futures_te
    from Futures.position_store import Position as FuturesPosition

    return {
        "Options": _StrategyHooks(
            OptionsPosition, options_cfg, options_te._get_ltp, options_te._supertrend_signal_for,
            options_te._ema_cross_signal_for, options_te._underlying_move_confirms_exit,
            options_te._exit_reason_for, options_te._capture_supertrend_entry_candle,
        ),
        "Luxury": _StrategyHooks(
            LuxuryPosition, luxury_cfg, luxury_te._get_ltp, luxury_te._supertrend_signal_for,
            luxury_te._ema_cross_signal_for, luxury_te._underlying_move_confirms_exit,
            luxury_te._exit_reason_for, luxury_te._capture_supertrend_entry_candle,
        ),
        "Futures": _StrategyHooks(
            FuturesPosition, futures_cfg, futures_te._get_ltp, futures_te._supertrend_signal_for,
            futures_te._ema_cross_signal_for, futures_te._underlying_move_confirms_exit,
            futures_te._exit_reason_for, futures_te._capture_supertrend_entry_candle,
        ),
    }


_HOOKS: dict[str, _StrategyHooks] = {}


def _hooks(strategy: str) -> _StrategyHooks:
    if not _HOOKS:
        _HOOKS.update(_build_hooks())
    return _HOOKS[strategy]


# --------------------------------------------------------------------- #
# Runtime paper-mode ON/OFF switch (added 24 Sep 2026, user request: "turn
# paper trading on or off... without deployment just by calling an
# endpoint"). Each package's own BREAKOUT_PAPER_MODE_ENABLED (Options/
# Futures/Luxury config.py) stays exactly as it was - the static .env-
# configured STARTUP default - but is now only the FALLBACK a strategy
# reads when no runtime override has ever been set for it. An override,
# once set via POST /paper-mode (main.py), takes priority and is
# persisted to PAPER_MODE_OVERRIDE_FILE (gitignored, same data/ dir
# convention as Swing/watchlist.py's own runtime-editable file) so it
# SURVIVES a restart - including the automatic 08:00 IST morning-refresh
# restart - instead of silently reverting to whatever .env says. Loaded
# lazily (once) rather than at module import time, matching _hooks'
# own deferred-import reasoning above.
#
# Deliberately does NOT touch _resolve_entry's ("Options"/"Futures"/
# "Luxury"'s own *_main.py) existing "paper REPLACES real, not an
# addition" semantic, or any already-open position - flipping a
# strategy INTO paper mode leaves its current real position(s) to be
# managed for real through to their own close; flipping OUT of paper
# mode leaves any currently-open PAPER position simulated to its own
# close too. Only NEW entries from that point on are affected - same
# behavior every other paper-mode flag in this codebase already has.
# --------------------------------------------------------------------- #
PAPER_MODE_OVERRIDE_FILE = Path("data/paper_mode_overrides.json")
_paper_mode_overrides: dict[str, bool] = {}
_paper_mode_overrides_loaded = False


def _load_paper_mode_overrides() -> None:
    global _paper_mode_overrides_loaded
    if _paper_mode_overrides_loaded:
        return
    _paper_mode_overrides_loaded = True
    if PAPER_MODE_OVERRIDE_FILE.exists():
        try:
            _paper_mode_overrides.update(json.loads(PAPER_MODE_OVERRIDE_FILE.read_text()))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not read %s - starting with no runtime paper-mode overrides "
                "(every strategy falls back to its own .env BREAKOUT_PAPER_MODE_ENABLED)",
                PAPER_MODE_OVERRIDE_FILE,
            )


def is_paper_mode_enabled(strategy: str) -> bool:
    """The one thing _resolve_entry (Options/Futures/Luxury's own *_main.py)
    should call instead of reading config.BREAKOUT_PAPER_MODE_ENABLED
    directly - everything else about paper-mode dispatch is unchanged."""
    _load_paper_mode_overrides()
    if strategy in _paper_mode_overrides:
        return _paper_mode_overrides[strategy]
    return bool(_hooks(strategy).cfg.BREAKOUT_PAPER_MODE_ENABLED)


def paper_mode_source(strategy: str) -> str:
    _load_paper_mode_overrides()
    return "runtime_override" if strategy in _paper_mode_overrides else "env_default"


async def set_paper_mode(strategy: str, enabled: bool) -> None:
    _load_paper_mode_overrides()
    async with _lock:
        _paper_mode_overrides[strategy] = enabled
        PAPER_MODE_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAPER_MODE_OVERRIDE_FILE.write_text(json.dumps(_paper_mode_overrides, indent=2))
    logger.info("%s: paper mode set to %s via runtime override (persisted to %s)",
                strategy, enabled, PAPER_MODE_OVERRIDE_FILE)


# --------------------------------------------------------------------- #
# Paper-only state - never touches any real package's own PositionStore
# or trade_history log. In-memory only (paper positions don't need to
# survive a restart the way real ones do - a restart just starts today's
# paper simulation fresh, same as Paper01's own precedent).
# --------------------------------------------------------------------- #
_positions: dict[tuple[str, str], object] = {}  # (strategy, symbol) -> Position
_lock = asyncio.Lock()
_today_key: Optional[str] = None
_entries_today: dict[tuple, int] = {}
_max_loss_hits_today: dict[tuple, int] = {}
_loss_count_today: dict[tuple, int] = {}


def _reset_daily_counters_if_new_day() -> None:
    global _today_key
    today = datetime.now().date().isoformat()
    if _today_key != today:
        _today_key = today
        _entries_today.clear()
        _max_loss_hits_today.clear()
        _loss_count_today.clear()


# --------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------- #
async def process_paper_entry(strategy: str, symbol: str, option_type: str) -> dict:
    """Entry point each package's own _breakout_entry_fn calls when
    BREAKOUT_PAPER_MODE_ENABLED is true, INSTEAD OF trading_engine.
    _process_one_entry - see this module's own docstring for why that's
    a full replacement, not an addition."""
    h = _hooks(strategy)
    cfg = h.cfg
    loop = asyncio.get_running_loop()
    _reset_daily_counters_if_new_day()
    key = (strategy, symbol)

    async with _lock:
        entries_today = _entries_today.get(key, 0)
        if entries_today >= cfg.MAX_DAILY_ENTRIES_PER_SYMBOL:
            return {"symbol": symbol, "status": "skipped", "reason": "daily_reentry_cap_reached", "mode": "paper"}

        if getattr(cfg, "ENABLE_RSI_LOSS_REENTRY_BLOCK", False) and _max_loss_hits_today.get(key, 0) >= 1:
            blocked = await loop.run_in_executor(None, dhan_wrapper.is_rsi_loss_reentry_blocked, symbol)
            if blocked:
                reason = dhan_wrapper.rsi_loss_reentry_reason(symbol)
                logger.info("%s %s: paper-skipped - MAX_LOSS_HIT already hit today and RSI is %s", strategy, symbol, reason)
                return {"symbol": symbol, "status": "skipped", "reason": "rsi_loss_reentry_block_active", "mode": "paper"}

        loss_count = _loss_count_today.get(key, 0)
        if getattr(cfg, "LOSS_REPEAT_BLOCK_ENABLED", False) and loss_count >= getattr(cfg, "LOSS_REPEAT_BLOCK_COUNT", 2):
            return {"symbol": symbol, "status": "skipped", "reason": "loss_repeat_block_active", "mode": "paper"}
        if getattr(cfg, "LOSS_REENTRY_TREND_CHECK_ENABLED", False) and loss_count >= 1:
            passes, adx, er = await reversal_filters.check_trend_strength(symbol)
            if not passes:
                return {"symbol": symbol, "status": "skipped", "reason": "loss_reentry_trend_check_failed",
                        "adx": adx, "er": er, "mode": "paper"}

        if getattr(cfg, "VOLUME_FLOOR_GATE_ENABLED", False):
            passes, vol_ratio = await reversal_filters.check_volume_floor(symbol, cfg.VOLUME_FLOOR_RATIO_MIN)
            if not passes:
                return {"symbol": symbol, "status": "skipped", "reason": "volume_floor_gate",
                        "vol_ratio": vol_ratio, "mode": "paper"}

    # cross_strategy_registry claim - SAME real, process-wide registry the
    # real _process_one_entry claims, checked here (before capacity/
    # reserve) to match its exact ordering. Held for the rest of this
    # function via the try/finally below, identical scoping to the real
    # path - matters most in MIXED mode (one package real, another paper).
    if not await cross_strategy_registry.try_claim(symbol, strategy):
        return {"symbol": symbol, "status": "skipped", "reason": "claimed_by_another_strategy", "mode": "paper"}
    try:
        async with _lock:
            cap = cfg.MAX_LIVE_POSITIONS_CE if option_type == "CE" else cfg.MAX_LIVE_POSITIONS_PE
            if option_type == "CE" and getattr(cfg, "BURST_CAPACITY_ENABLED", False):
                now_t = datetime.now().time()
                sh, sm = (int(x) for x in cfg.BURST_WINDOW_START.split(":"))
                eh, em = (int(x) for x in cfg.BURST_WINDOW_END.split(":"))
                if dtime(sh, sm) <= now_t <= dtime(eh, em):
                    cap += cfg.BURST_EXTRA_SLOTS_CE
            current_open = sum(1 for (s, _sym), pos in _positions.items()
                                if s == strategy and pos.option_type == option_type)
            if current_open >= cap:
                return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full", "mode": "paper"}
            if key in _positions:
                return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full", "mode": "paper"}

            # Reserve the slot before any await below, matching the real
            # reserve-then-fill ordering every package's own PositionStore uses.
            _positions[key] = None  # placeholder, replaced below or removed on failure

        # Broker-truth belt-and-suspenders check (real, read-only) - same
        # reasoning _process_one_entry itself uses: our own in-memory
        # reservation only guards duplicates within THIS paper pool, not a
        # real broker position that predates it (another process instance,
        # a manual trade, or a real position this same underlying already
        # has open for real, in mixed mode).
        already_open = await loop.run_in_executor(None, dhan_wrapper.has_open_position_for_underlying, symbol)
        if already_open:
            async with _lock:
                _positions.pop(key, None)
            return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker", "mode": "paper"}

        atm = await loop.run_in_executor(None, dhan_wrapper.get_liquid_atm_option, symbol, option_type)
        if atm is None:
            async with _lock:
                _positions.pop(key, None)
            return {"symbol": symbol, "status": "skipped", "reason": "no_liquid_contract_available", "mode": "paper"}

        if atm.expiry_date == datetime.now().date():
            async with _lock:
                _positions.pop(key, None)
            return {"symbol": symbol, "status": "skipped_expiry_day", "option_trading_symbol": atm.trading_symbol, "mode": "paper"}

        if getattr(cfg, "LIQUIDITY_ENTRY_GATE_ENABLED", False):
            passes, _is_illiquid = await reversal_filters.check_option_liquidity(atm.trading_symbol)
            if not passes:
                async with _lock:
                    _positions.pop(key, None)
                return {"symbol": symbol, "status": "skipped", "reason": "option_illiquid_at_entry",
                        "option_trading_symbol": atm.trading_symbol, "mode": "paper"}

        quantity = atm.lot_size * cfg.QUANTITY_LOTS

        if getattr(cfg, "FUNDS_CHECK_ENABLED", False):
            # Real, read-only margin-calculator + fund-limit query - no
            # order is placed either way, so this is safe to run exactly
            # as-is in paper mode. Uses the same "secondary" bucket
            # Options/Futures/Luxury's real entries check (see fund_
            # allocation.py's own 2-bucket docstring).
            try:
                price_for_funds = await h.get_ltp(atm.trading_symbol)
                if not price_for_funds:
                    price_for_funds = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
                sufficient = await fund_allocation.has_sufficient_bucket_funds(
                    "secondary", symbol, [(atm.security_id, cfg.OPTIONS_PRODUCT, quantity, price_for_funds)],
                )
            except Exception:  # noqa: BLE001
                logger.exception("%s %s: could not price the leg for the paper funds check - proceeding optimistically",
                                  strategy, symbol)
                sufficient = True
            if not sufficient:
                async with _lock:
                    _positions.pop(key, None)
                return {"symbol": symbol, "status": "skipped", "reason": "insufficient_funds",
                        "option_trading_symbol": atm.trading_symbol, "mode": "paper"}

        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, atm.trading_symbol)

        entry_price = await h.get_ltp(atm.trading_symbol)
        if not entry_price:
            entry_price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
        if not entry_price:
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, atm.trading_symbol)
            async with _lock:
                _positions.pop(key, None)
            return {"symbol": symbol, "status": "error", "reason": "no_ltp_available", "mode": "paper"}

        entry_candle_start, entry_underlying_price = await h.capture_supertrend_entry_candle(loop, symbol)

        position = h.position_cls(
            underlying_symbol=symbol,
            option_trading_symbol=atm.trading_symbol,
            option_type=option_type,
            quantity=quantity,
            lot_size=atm.lot_size,
            entry_price=entry_price,
            highest_price=entry_price,
            target_price=entry_price * (1 + cfg.TARGET_PCT),
            hard_stop_loss=entry_price * (1 - cfg.STOP_LOSS_PCT),
            order_id="",
            product_type="PAPER",
            supertrend_entry_candle_start=entry_candle_start,
            entry_underlying_price=entry_underlying_price,
        )

        async with _lock:
            _positions[key] = position
            _entries_today[key] = entries_today + 1

        logger.warning(
            "%s %s: PAPER entry (breakout scanner signal) - %s entry=%.2f qty=%d",
            strategy, symbol, atm.trading_symbol, entry_price, quantity,
        )
        return {
            "symbol": symbol, "status": "paper_entered", "option_trading_symbol": atm.trading_symbol,
            "quantity": quantity, "entry_price": entry_price, "mode": "paper",
        }
    except Exception as exc:  # noqa: BLE001
        async with _lock:
            _positions.pop(key, None)
        logger.exception("%s %s: failed to enter paper position", strategy, symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc), "mode": "paper"}
    finally:
        await cross_strategy_registry.release_claim(symbol, strategy)


# --------------------------------------------------------------------- #
# Exit / monitoring
# --------------------------------------------------------------------- #
async def _log_paper_trade(strategy: str, position) -> None:
    pnl = None
    if position.exit_price is not None and position.entry_price is not None:
        pnl = (position.exit_price - position.entry_price) * position.quantity
    record = {
        "strategy": strategy, "underlying_symbol": position.underlying_symbol,
        "option_trading_symbol": position.option_trading_symbol, "option_type": position.option_type,
        "quantity": position.quantity, "product_type": position.product_type,
        "entry_price": position.entry_price, "exit_price": position.exit_price,
        "exit_reason": position.exit_reason, "pnl": pnl,
        "opened_at": position.opened_at.isoformat() if position.opened_at else None,
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        "order_id": "", "mode": "paper", "signal_source": "breakout_scanner",
        "logged_at": datetime.now().isoformat(),
    }
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, trade_history.append_jsonl, PAPER_TRADES_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("Could not append paper trade record for %s %s - in-memory state unaffected",
                          strategy, position.underlying_symbol)


async def _exit_one(strategy: str, symbol: str, position, exit_price: float, reason: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
    position.exit_price = exit_price
    position.exit_reason = reason
    position.closed_at = datetime.now()
    position.status = "CLOSED"
    key = (strategy, symbol)
    async with _lock:
        _positions.pop(key, None)
        pnl = (exit_price - position.entry_price) * position.quantity
        if pnl < 0:
            _loss_count_today[key] = _loss_count_today.get(key, 0) + 1
            if reason == "MAX_LOSS_HIT":
                _max_loss_hits_today[key] = _max_loss_hits_today.get(key, 0) + 1
    logger.warning(
        "%s %s: PAPER exit - %s exit=%.2f reason=%s pnl=%+.2f",
        strategy, symbol, position.option_trading_symbol, exit_price, reason,
        (exit_price - position.entry_price) * position.quantity,
    )
    await _log_paper_trade(strategy, position)


async def _check_one(strategy: str, symbol: str, position) -> None:
    h = _hooks(strategy)
    cfg = h.cfg
    loop = asyncio.get_running_loop()
    try:
        ltp = await h.get_ltp(position.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for paper position %s %s", strategy, position.option_trading_symbol)
        return
    if ltp is None:
        return

    position.highest_price = max(position.highest_price, ltp)

    supertrend_against_position = False
    if getattr(cfg, "ENABLE_SUPERTREND_EXIT", False):
        await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, position.underlying_symbol)
        supertrend_against_position = h.supertrend_signal_for(position)

    ema_cross_against_position = False
    if getattr(cfg, "ENABLE_EMA_CROSS_EXIT", False):
        await loop.run_in_executor(None, dhan_wrapper.refresh_ema_cross_signal, position.underlying_symbol)
        ema_cross_against_position = h.ema_cross_signal_for(position)

    if supertrend_against_position or ema_cross_against_position:
        confirmed = h.underlying_move_confirms_exit(position)
        supertrend_against_position = supertrend_against_position and confirmed
        ema_cross_against_position = ema_cross_against_position and confirmed

    liquidity_guard_triggered = False
    if getattr(cfg, "LIQUIDITY_GUARD_ENABLED", False):
        await loop.run_in_executor(None, dhan_wrapper.refresh_liquidity_signal, position.option_trading_symbol)
        liquidity_guard_triggered = bool(dhan_wrapper.get_cached_illiquid(position.option_trading_symbol))

    reason = h.exit_reason_for(position, ltp, supertrend_against_position, liquidity_guard_triggered, ema_cross_against_position)
    if reason:
        await _exit_one(strategy, symbol, position, ltp, reason)


async def paper_engine_monitor_loop() -> None:
    """Started once from main.py's own lifespan (like UniverseDispatcher) -
    cheap no-op when no package has BREAKOUT_PAPER_MODE_ENABLED on (nothing
    in _positions to check). Reuses Options.config.MONITOR_INTERVAL_SECONDS
    for cadence, same as every other monitor loop in this codebase."""
    from Options import config as options_cfg
    logger.info("Breakout-scanner paper-trading engine monitor loop started.")
    while True:
        try:
            _reset_daily_counters_if_new_day()
            async with _lock:
                snapshot = list(_positions.items())
            await asyncio.gather(*[
                _check_one(strategy, symbol, position)
                for (strategy, symbol), position in snapshot if position is not None
            ])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Error in breakout-scanner paper-trading engine monitor loop tick")
        await asyncio.sleep(options_cfg.MONITOR_INTERVAL_SECONDS)


async def snapshot() -> dict:
    """Read-only observability - every currently-open paper position
    across all 3 packages."""
    async with _lock:
        items = list(_positions.items())
    return {
        f"{strategy}:{symbol}": {
            "option_trading_symbol": pos.option_trading_symbol, "option_type": pos.option_type,
            "entry_price": pos.entry_price, "highest_price": pos.highest_price,
            "target_price": pos.target_price, "hard_stop_loss": pos.hard_stop_loss,
            "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
        }
        for (strategy, symbol), pos in items if pos is not None
    }
