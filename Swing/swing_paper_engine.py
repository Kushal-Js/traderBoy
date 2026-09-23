"""
Swing v2's global paper-trading engine (added 23 Sep 2026, user request) -
the Swing counterpart to breakout_paper_engine.py's Options/Futures/
Luxury design. See that module's own docstring for the full rationale;
this mirrors it as closely as Swing's different (single-poll-loop, no
webhook/breakout-signal) architecture allows.

*** SAME SEMANTIC, CONFIRMED WITH THE USER: this REPLACES real trading,
it does not run alongside it. *** With config.PAPER_MODE_ENABLED=true,
Swing/trading_engine.py's own _monitor_tick calls this module's
process_paper_entry INSTEAD of enter_position_for_stock - not both.
Turning this on means Swing places ZERO real orders until it's turned
back off.

WHY "REAL CONDITIONS" HOLDS BY CONSTRUCTION: every entry gate below is
either the exact same real Dhan/fund_allocation call enter_position_for_
stock itself makes (instrument resolution, funds check, the duplicate-
order guard), or a direct read of this package's own live config -
never a re-implementation that could drift. Exits go one step further
than the Options/Futures/Luxury paper engine even needs to: Swing's own
_exit_reason_for/_evaluate_exit_signal/_get_ltp are called DIRECTLY from
Swing.trading_engine (not duplicated via a hooks table - Swing has only
one strategy, no per-package dispatch needed), against a real Swing.
position_store.Position object (product_type="PAPER", order_id="",
stop_loss_order_id=None) - not a lookalike class, so every Position
property (unrealized_pnl_rs, current-side math, etc.) just works.

ENTRY GATES REPLICATED, same order as enter_position_for_stock:
  1. STRATEGY_ENABLED
  2. resolve_instrument_side (skips EQUITY+BEARISH, long-only)
  3. Volume-floor gate (MCX_VOLUME_FLOOR_GATE_ENABLED / NSE_VOLUME_FLOOR_
     GATE_ENABLED - same real signals.get_supertrend_state read)
  4. Capacity (this module's own paper-only pool, MAX_CONCURRENT_TRADES -
     Swing has one shared counter, no CE/PE split, same as real)
  5. Instrument resolution - the SAME real dhan_wrapper calls
     (get_futures_contract / get_liquid_atm_option / _equity_instrument_
     meta), resolving a real contract/expiry/lot_size, placing no order
  6. FUNDS_CHECK_ENABLED - the same real fund_allocation.has_sufficient_
     bucket_funds against the real "primary" bucket (Swing's own, per
     fund_allocation.py's 2-bucket design) - read-only, no order placed
  7. Duplicate-order guard (dhan_wrapper.get_pending_order_id) - real,
     read-only broker query; mirrors the real structural fix for the
     COALINDIA AMO-retry incident, so paper mode can't spam a "duplicate"
     entry the same way that incident did, even though nothing real is
     ever placed

NOT REPLICATED, and cannot be by construction - places a REAL order:
  - BROKER_STOP_LOSS_ENABLED's resting SL-L order. A paper position's
    protection is the same poll-driven _exit_reason_for check that
    already backstops every real position even when its OWN broker-side
    stop failed to place - not a weaker guarantee than the real path
    ever silently falls back to.
  - Real order placement itself (place_market_order/place_mcx_market_
    order) - the entire point of paper mode.
  - COPPER's structure-break "mark this agreement consumed" side effect
    (signals.mark_structure_break_consumed) - deliberately left to the
    REAL path only, so a paper run can never consume a real agreement a
    genuine entry might still need.

LOGGING: one record per closed paper trade to history/<date>_swing_
paper_trades.log, the same field shape trade_history.record_closed_
trade produces for a real Swing trade (via Position's own
option_trading_symbol/option_type aliases), plus "mode": "paper" so it's
never confused with a real trade record but is otherwise directly
comparable/diffable against one - same convention breakout_paper_
engine.py already established for Options/Futures/Luxury.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from Options.dhan_client import dhan_wrapper
import fund_allocation
import trade_history
from Swing import config
from Swing import signals
from Swing import trading_engine as swing_te
from Swing.position_store import (
    Position,
    entry_transaction_type,
    resolve_instrument_side,
    resolved_option_type_for,
)

logger = logging.getLogger("swing_paper_engine")

PAPER_TRADES_LOG_NAME = "swing_paper_trades"

# --------------------------------------------------------------------- #
# Paper-only state - never touches the real Swing PositionStore or
# trade_history log. In-memory only, same "a restart just starts today's
# paper simulation fresh" precedent as breakout_paper_engine.py.
# --------------------------------------------------------------------- #
_positions: dict[str, Position] = {}   # underlying_symbol -> Position
_lock = asyncio.Lock()


def _now_ist():
    return swing_te._now_ist()


# --------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------- #
async def process_paper_entry(symbol: str, regime: str) -> dict:
    """Called from Swing/trading_engine.py's own _monitor_tick INSTEAD of
    enter_position_for_stock when config.PAPER_MODE_ENABLED is true - see
    this module's own docstring for why that's a full replacement."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled", "mode": "paper"}

    basket_type = config.BASKET_TYPE.upper()
    is_mcx = symbol in config.MCX_SYMBOLS
    effective_basket_type = "OPTIONS" if symbol in config.MCX_OPTIONS_ONLY_SYMBOLS else basket_type
    side = resolve_instrument_side(effective_basket_type, regime)
    if side is None:
        return {"symbol": symbol, "status": "skipped", "reason": "equity_long_only", "mode": "paper"}

    volume_floor_enabled = config.MCX_VOLUME_FLOOR_GATE_ENABLED if is_mcx else config.NSE_VOLUME_FLOOR_GATE_ENABLED
    volume_floor_ratio_min = config.MCX_VOLUME_FLOOR_RATIO_MIN if is_mcx else config.NSE_VOLUME_FLOOR_RATIO_MIN
    if volume_floor_enabled:
        st = await signals.get_supertrend_state(symbol)
        vol_ratio = st.volume_ratio if st else None
        if vol_ratio is not None and vol_ratio < volume_floor_ratio_min:
            gate_reason = "mcx_volume_floor_gate" if is_mcx else "nse_volume_floor_gate"
            return {"symbol": symbol, "status": "skipped", "reason": gate_reason, "vol_ratio": vol_ratio, "mode": "paper"}

    async with _lock:
        if symbol in _positions:
            return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full", "mode": "paper"}
        if len(_positions) >= config.MAX_CONCURRENT_TRADES:
            return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full", "mode": "paper"}
        _positions[symbol] = None  # placeholder, matches real reserve-then-fill ordering

    loop = asyncio.get_running_loop()
    try:
        option_type = resolved_option_type_for(effective_basket_type, regime)
        try:
            if effective_basket_type == "FUTURES":
                contract = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
                trading_symbol, security_id, lot_size = contract.trading_symbol, contract.security_id, contract.lot_size
                exchange_segment, product_type = "NSE_FNO", config.FUTURES_PRODUCT
                quantity = lot_size * config.QUANTITY_LOTS
                pnl_multiplier = quantity
            elif effective_basket_type == "OPTIONS":
                atm = await loop.run_in_executor(None, dhan_wrapper.get_liquid_atm_option, symbol, option_type)
                if atm is None:
                    async with _lock:
                        _positions.pop(symbol, None)
                    return {"symbol": symbol, "status": "skipped", "reason": "no_liquid_contract_available", "mode": "paper"}
                if atm.expiry_date == _now_ist().date():
                    async with _lock:
                        _positions.pop(symbol, None)
                    return {"symbol": symbol, "status": "skipped_expiry_day",
                             "option_trading_symbol": atm.trading_symbol, "mode": "paper"}
                trading_symbol, security_id, lot_size = atm.trading_symbol, atm.security_id, atm.lot_size
                quantity = lot_size * config.QUANTITY_LOTS
                if is_mcx:
                    exchange_segment, product_type = "MCX_COMM", config.MCX_PRODUCT
                    pnl_multiplier = config.MCX_PNL_MULTIPLIERS[symbol] * config.QUANTITY_LOTS
                else:
                    exchange_segment, product_type = "NSE_FNO", config.OPTIONS_PRODUCT
                    pnl_multiplier = quantity
            else:  # EQUITY
                meta = await loop.run_in_executor(None, dhan_wrapper._equity_instrument_meta, symbol)
                trading_symbol, security_id, lot_size = symbol, meta["security_id"], None
                exchange_segment, product_type = "NSE_EQ", config.EQUITY_PRODUCT
                quantity = config.EQUITY_QUANTITY
                pnl_multiplier = quantity
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not resolve the %s instrument for a paper entry", symbol, effective_basket_type)
            async with _lock:
                _positions.pop(symbol, None)
            return {"symbol": symbol, "status": "error", "reason": "instrument_resolution_failed", "mode": "paper"}

        if config.FUNDS_CHECK_ENABLED:
            try:
                price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
                sufficient = await fund_allocation.has_sufficient_bucket_funds(
                    config.FUND_BUCKET, symbol,
                    [(security_id, product_type, quantity, price, exchange_segment)],
                    buffer_rs=config.FUNDS_CHECK_BUFFER_RS,
                )
            except Exception:  # noqa: BLE001
                logger.exception("%s: could not price the leg for the paper funds check - proceeding optimistically", symbol)
                sufficient = True
            if not sufficient:
                async with _lock:
                    _positions.pop(symbol, None)
                return {"symbol": symbol, "status": "skipped", "reason": "insufficient_funds",
                        "trading_symbol": trading_symbol, "mode": "paper"}

        transaction_type = entry_transaction_type(side)
        try:
            existing_order_id = await loop.run_in_executor(
                None, dhan_wrapper.get_pending_order_id, trading_symbol, transaction_type,
                "MCX" if exchange_segment == "MCX_COMM" else "NSE",
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not check for an already-resting entry order - proceeding anyway", symbol)
            existing_order_id = None
        if existing_order_id:
            async with _lock:
                _positions.pop(symbol, None)
            return {"symbol": symbol, "status": "already_pending", "order_id": existing_order_id,
                    "trading_symbol": trading_symbol, "mode": "paper"}

        if exchange_segment in ("NSE_FNO", "MCX_COMM"):
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, trading_symbol)

        fill_price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
        if not fill_price:
            if exchange_segment in ("NSE_FNO", "MCX_COMM"):
                await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, trading_symbol)
            async with _lock:
                _positions.pop(symbol, None)
            return {"symbol": symbol, "status": "error", "reason": "no_ltp_available", "mode": "paper"}

        st = await signals.get_supertrend_state(symbol)
        entry_candle_start = st.candle_start if st else None

        position = Position(
            underlying_symbol=symbol, trading_symbol=trading_symbol, basket_type=effective_basket_type, regime=regime,
            instrument_side=side, exchange_segment=exchange_segment, product_type="PAPER",
            quantity=quantity, lot_size=lot_size, entry_price=fill_price, best_price=fill_price,
            target_price=swing_te.target_price_for(side, fill_price, config.TARGET_PCT),
            hard_stop_loss=swing_te.hard_stop_for(side, fill_price, config.HARD_STOP_LOSS_PCT),
            order_id="", pnl_multiplier=pnl_multiplier, resolved_option_type=option_type,
            supertrend_entry_candle_start=entry_candle_start, stop_loss_order_id=None,
        )
        async with _lock:
            _positions[symbol] = position

        logger.warning(
            "%s: SWING PAPER entry - %s entry=%.2f qty=%d side=%s", symbol, trading_symbol, fill_price, quantity, side,
        )
        return {"symbol": symbol, "status": "paper_entered", "trading_symbol": trading_symbol,
                "entry_price": fill_price, "mode": "paper"}
    except Exception as exc:  # noqa: BLE001
        async with _lock:
            _positions.pop(symbol, None)
        logger.exception("%s: unexpected error entering a paper position", symbol)
        return {"symbol": symbol, "status": "error", "reason": str(exc), "mode": "paper"}


# --------------------------------------------------------------------- #
# Exit / monitoring - reuses Swing.trading_engine's real, unmodified
# _get_ltp/_evaluate_exit_signal/_exit_reason_for directly (see module
# docstring - Swing has only one strategy, no hooks table needed).
# --------------------------------------------------------------------- #
async def _log_paper_trade(position: Position) -> None:
    pnl = None
    if position.exit_price is not None and position.entry_price is not None:
        pnl = swing_te.unrealized_pnl_rs(position.instrument_side, position.entry_price, position.exit_price, position.pnl_multiplier)
    record = {
        "strategy": "Swing", "underlying_symbol": position.underlying_symbol,
        "option_trading_symbol": position.option_trading_symbol, "option_type": position.option_type,
        "basket_type": position.basket_type, "instrument_side": position.instrument_side,
        "quantity": position.quantity, "product_type": position.product_type,
        "entry_price": position.entry_price, "exit_price": position.exit_price,
        "exit_reason": position.exit_reason, "pnl": pnl,
        "opened_at": position.opened_at.isoformat() if position.opened_at else None,
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        "order_id": "", "mode": "paper", "signal_source": "swing_v2",
        "logged_at": datetime.now().isoformat(),
    }
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, trade_history.append_jsonl, PAPER_TRADES_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("Could not append Swing paper trade record for %s - in-memory state unaffected",
                          position.underlying_symbol)


async def _exit_one(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    loop = asyncio.get_running_loop()
    if position.exchange_segment in ("NSE_FNO", "MCX_COMM"):
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.trading_symbol)
    position.exit_price = exit_price
    position.exit_reason = reason
    position.closed_at = datetime.now()
    position.status = "CLOSED"
    async with _lock:
        _positions.pop(symbol, None)
    pnl = swing_te.unrealized_pnl_rs(position.instrument_side, position.entry_price, exit_price, position.pnl_multiplier)
    logger.warning("%s: SWING PAPER exit - %s exit=%.2f reason=%s pnl=%+.2f",
                    symbol, position.trading_symbol, exit_price, reason, pnl)
    await _log_paper_trade(position)


async def _check_one(symbol: str, position: Position) -> None:
    try:
        ltp = await swing_te._get_ltp(position)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for Swing paper position %s", position.trading_symbol)
        return
    if ltp is None:
        return
    position.best_price = ltp if swing_te.is_more_favorable(position.instrument_side, ltp, position.best_price) else position.best_price

    reason = swing_te._exit_reason_for(position, ltp)
    if not reason:
        reason = await swing_te._evaluate_exit_signal(symbol, position)
    if reason:
        await _exit_one(symbol, position, ltp, reason)


async def paper_engine_monitor_loop() -> None:
    """Started once from main.py's own lifespan, alongside breakout_paper_
    engine's own task - cheap no-op when config.PAPER_MODE_ENABLED is off
    (nothing in _positions to check)."""
    logger.info("Swing paper-trading engine monitor loop started.")
    while True:
        try:
            async with _lock:
                snapshot = list(_positions.items())
            await asyncio.gather(*[
                _check_one(symbol, position) for symbol, position in snapshot if position is not None
            ])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Error in Swing paper-trading engine monitor loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)


async def snapshot() -> dict:
    """Read-only observability - every currently-open Swing paper position."""
    async with _lock:
        items = list(_positions.items())
    return {
        symbol: {
            "trading_symbol": pos.trading_symbol, "basket_type": pos.basket_type,
            "instrument_side": pos.instrument_side, "entry_price": pos.entry_price,
            "best_price": pos.best_price, "target_price": pos.target_price,
            "hard_stop_loss": pos.hard_stop_loss,
            "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
        }
        for symbol, pos in items if pos is not None
    }
