"""
Core strategy logic.

Flow:
  1. rank_and_pick_top_stocks()  - from the Chartink alert's stock list, pick
     the top-N by today's %change (highest first).
  2. enter_positions_for_stocks() - for each qualifying stock (not already
     traded today, capacity available), find the ATM option and place a
     BUY MARKET order.
  3. monitor_loop() - background asyncio loop, polls every
     MONITOR_INTERVAL_SECONDS, and exits a leg when target / stop-loss /
     trailing stop-loss is hit, or force-squares-off everything at
     SQUARE_OFF_TIME.
"""
from __future__ import annotations

import asyncio
import logging
import random
import string
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import config
from groww_client import OrderStatus, groww_wrapper
from position_store import OrderRecord, Position, position_store

logger = logging.getLogger("trading_engine")

IST = ZoneInfo(config.MARKET_TZ)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm_today(hhmm: str) -> datetime:
    now = _now_ist()
    hour, minute = map(int, hhmm.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _gen_reference_id(prefix: str, symbol: str) -> str:
    """8-20 alphanumeric chars, at most two hyphens, per Groww's spec."""
    suffix = "".join(random.choices(string.digits, k=6))
    ref = f"{prefix}-{symbol[:6]}-{suffix}"
    return ref[:20]


# --------------------------------------------------------------------------- #
# Step 1: rank stocks from the webhook payload by today's % change
# --------------------------------------------------------------------------- #
def rank_and_pick_top_stocks(stock_symbols: list[str], top_n: int = config.TOP_N_STOCKS) -> list[tuple[str, float]]:
    """Returns [(symbol, pct_change), ...] sorted descending, top_n only.
    Stocks that error out on the quote lookup are skipped (and logged)."""
    scored: list[tuple[str, float]] = []
    for symbol in stock_symbols:
        try:
            pct = groww_wrapper.get_day_change_pct(symbol)
            scored.append((symbol, pct))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s - could not fetch day change: %s", symbol, exc)

    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_n]


# --------------------------------------------------------------------------- #
# Step 2: enter positions
# --------------------------------------------------------------------------- #
async def enter_positions_for_stocks(ranked_stocks: list[tuple[str, float]]) -> list[dict]:
    """Places ATM-option BUY orders for as many of the ranked stocks as
    capacity and dedup rules allow. Returns a per-stock result log."""
    results: list[dict] = []

    for symbol, pct_change in ranked_stocks:
        if await position_store.is_already_traded(symbol):
            msg = f"{symbol}: skipped - already traded/live today"
            logger.info(msg)
            results.append({"symbol": symbol, "status": "skipped", "reason": "duplicate"})
            continue

        if not await position_store.has_capacity():
            msg = f"{symbol}: skipped - {config.MAX_LIVE_POSITIONS} live positions already open"
            logger.info(msg)
            results.append({"symbol": symbol, "status": "skipped", "reason": "capacity_full"})
            continue

        try:
            entry_result = await _enter_single_position(symbol)
            results.append(entry_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to enter position for %s", symbol)
            results.append({"symbol": symbol, "status": "error", "reason": str(exc)})

    return results


async def _enter_single_position(symbol: str) -> dict:
    loop = asyncio.get_running_loop()

    atm = await loop.run_in_executor(
        None, groww_wrapper.get_atm_option, symbol, config.OPTION_TYPE
    )
    quantity = atm.lot_size * config.QUANTITY_LOTS
    ref_id = _gen_reference_id(config.ORDER_REFERENCE_PREFIX, symbol)

    # Subscribe to the option's live price over the WebSocket feed as early
    # as possible so ticks are already flowing by the time we start monitoring.
    await loop.run_in_executor(None, groww_wrapper.subscribe_option_price, atm.trading_symbol)

    order_resp = await loop.run_in_executor(
        None,
        groww_wrapper.place_market_order,
        atm.trading_symbol,
        quantity,
        "BUY",
        ref_id,
    )
    groww_order_id = order_resp["groww_order_id"]

    await position_store.record_order(OrderRecord(
        groww_order_id=groww_order_id,
        order_reference_id=ref_id,
        underlying_symbol=symbol,
        trading_symbol=atm.trading_symbol,
        transaction_type="BUY",
        quantity=quantity,
        status=order_resp.get("order_status") or OrderStatus.NEW,
        remark=order_resp.get("remark", ""),
    ))

    result = await loop.run_in_executor(None, groww_wrapper.wait_for_order_result, groww_order_id)
    await position_store.update_order_status(groww_order_id, result.status, result.remark)

    if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
        await loop.run_in_executor(None, groww_wrapper.unsubscribe_option_price, atm.trading_symbol)
        logger.warning(
            "BUY order %s for %s rejected: status=%s remark=%s",
            groww_order_id, symbol, result.status, result.remark,
        )
        return {
            "symbol": symbol,
            "status": "rejected",
            "order_status": result.status,
            "remark": result.remark,
            "option_trading_symbol": atm.trading_symbol,
            "groww_order_id": groww_order_id,
        }

    fill_price = result.fill_price
    if not fill_price:
        # Order reached a terminal "filled" status but the fill price field
        # hasn't populated yet (rare) - fall back to LTP so target/SL levels
        # aren't computed off zero.
        fill_price = await loop.run_in_executor(
            None, groww_wrapper.get_option_ltp, atm.trading_symbol
        )

    position = Position(
        underlying_symbol=symbol,
        option_trading_symbol=atm.trading_symbol,
        option_type=atm.option_type,
        quantity=quantity,
        lot_size=atm.lot_size,
        entry_price=fill_price,
        highest_price=fill_price,
        target_price=fill_price * (1 + config.TARGET_PCT),
        hard_stop_loss=fill_price * (1 - config.STOP_LOSS_PCT),
        groww_order_id=groww_order_id,
        order_reference_id=ref_id,
    )
    await position_store.add_position(position)

    return {
        "symbol": symbol,
        "status": "entered",
        "order_status": result.status,
        "option_trading_symbol": atm.trading_symbol,
        "quantity": quantity,
        "entry_price": fill_price,
        "groww_order_id": groww_order_id,
    }


# --------------------------------------------------------------------------- #
# Step 3: monitoring / exits
# --------------------------------------------------------------------------- #
async def _exit_position(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    loop = asyncio.get_running_loop()
    ref_id = _gen_reference_id("Ext", symbol)
    try:
        order_resp = await loop.run_in_executor(
            None,
            groww_wrapper.place_market_order,
            position.option_trading_symbol,
            position.quantity,
            "SELL",
            ref_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("SELL order failed for %s (%s) - will retry next tick",
                          symbol, position.option_trading_symbol)
        return  # leave it live so the next monitor tick retries the exit

    groww_order_id = order_resp["groww_order_id"]
    await position_store.record_order(OrderRecord(
        groww_order_id=groww_order_id,
        order_reference_id=ref_id,
        underlying_symbol=symbol,
        trading_symbol=position.option_trading_symbol,
        transaction_type="SELL",
        quantity=position.quantity,
        status=order_resp.get("order_status") or OrderStatus.NEW,
        remark=order_resp.get("remark", ""),
    ))

    result = await loop.run_in_executor(None, groww_wrapper.wait_for_order_result, groww_order_id)
    await position_store.update_order_status(groww_order_id, result.status, result.remark)

    if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
        # The SELL was rejected/cancelled by the exchange - the position is
        # still genuinely open at the broker. Leave it live so the next
        # monitor tick retries the exit, instead of marking it closed.
        logger.warning(
            "SELL order %s for %s rejected: status=%s remark=%s - will retry next tick",
            groww_order_id, symbol, result.status, result.remark,
        )
        return

    final_exit_price = result.fill_price or exit_price
    await position_store.close_position(symbol, final_exit_price, reason)
    await loop.run_in_executor(None, groww_wrapper.unsubscribe_option_price, position.option_trading_symbol)


async def _get_ltp(trading_symbol: str) -> Optional[float]:
    """Prefers the WebSocket feed's cached LTP (near-instant); falls back to
    a REST call if no tick has arrived yet (e.g. subscription just made)."""
    loop = asyncio.get_running_loop()
    ltp = await loop.run_in_executor(None, groww_wrapper.get_cached_option_ltp, trading_symbol)
    if ltp is not None:
        return ltp
    return await loop.run_in_executor(None, groww_wrapper.get_option_ltp, trading_symbol)


async def _check_one_position(symbol: str, position: Position) -> None:
    try:
        ltp = await _get_ltp(position.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for %s", position.option_trading_symbol)
        return

    await position_store.update_highest_price(symbol, ltp)
    trailing_sl = position.current_trailing_sl

    if ltp >= position.target_price:
        await _exit_position(symbol, position, ltp, "TARGET_HIT")
    elif ltp <= trailing_sl:
        reason = "TRAILING_SL_HIT" if trailing_sl > position.hard_stop_loss else "STOP_LOSS_HIT"
        await _exit_position(symbol, position, ltp, reason)


async def _square_off_all(reason: str) -> None:
    positions = dict(position_store.live_positions)  # snapshot of keys/values
    if not positions:
        return
    logger.info("Square-off triggered (%s) for %d open position(s)", reason, len(positions))
    for symbol, position in positions.items():
        try:
            ltp = await _get_ltp(position.option_trading_symbol)
        except Exception:  # noqa: BLE001
            ltp = position.entry_price  # best-effort fallback for logging only
        await _exit_position(symbol, position, ltp, reason)


async def monitor_loop() -> None:
    """Runs forever; polls open positions and enforces exits + EOD square-off."""
    logger.info("Monitor loop started.")
    squared_off_today_for: set = set()

    while True:
        try:
            await position_store.maybe_reset_for_new_day()

            now = _now_ist()
            square_off_at = _parse_hhmm_today(config.SQUARE_OFF_TIME)
            today_key = now.date()

            if now >= square_off_at and today_key not in squared_off_today_for:
                await _square_off_all("EOD_SQUARE_OFF_3_15PM")
                squared_off_today_for.add(today_key)
            elif now < square_off_at:
                positions = list(position_store.live_positions.items())
                await asyncio.gather(
                    *[_check_one_position(sym, pos) for sym, pos in positions]
                )
        except Exception:  # noqa: BLE001
            logger.exception("Error in monitor loop tick")

        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
