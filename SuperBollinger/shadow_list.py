"""
The shadow watchlist traded on PAPER, for log analysis (1 Oct 2026, user:
"40 session watchlist in shadow mode for log analysis").

The Friday refresh records a second stock list next to the live HYBRID one
(stock_selection.run_shadow: 40-session fit with the 1-hour filter,
data/super_bollinger_shadow_watchlist.json). Its weekly score is only a
stock-price replay. This module runs Super Bollinger's own entry and exit
rules on that list during the day, on paper, so the two lists can be
compared on real signals, real option prices and the same slot limit:

  entries  the same resting BULLISH trigger (Bollinger.trading_engine.
           _evaluate_entry_signal) with this module's OWN signal bookkeeping
           (a trigger acted on by the real book is still available here and
           vice versa), the same 1-hour-green filter when it is on for real,
           the same ATM CE contract choice and minimum premium, the same
           14:00 entry cutoff, and at most max_concurrent_trades open at
           once (the real limit, counted on this book only);
  exits    max loss, breakeven stop after the same profit, 15:15 square-off
           (trading_engine.exit_reason_for) - calls only: no hedge, no
           scale-in legs;
  books    its own paper book and logs - data/super_bollinger_shadow_paper_
           positions.json, history/<date>_super_bollinger_shadow_paper_trades.log
           (pnl_modeled with the backtests' slippage) and
           history/<date>_super_bollinger_shadow_events.log.
Nothing here places an order or touches the real book, the real slots or
the real signal bookkeeping. It runs as a background task started from the
monitor loop (never delays a real entry/exit/hedge check) and only while no
previous pass is still running. Uses the Dhan LTP call for the paper entry
price, not the quote API, so it never queues behind a real entry's quote.
Setting: shadow_list_mode off | paper (default paper).
GET /super-bollinger/shadow-trades.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import capacity_control
import stock_selection
from Bollinger import config as bcfg, signals
from Bollinger import trading_engine as engine
from Bollinger.paper_book import PaperBook
from Options.dhan_client import dhan_wrapper

from . import entry_filters, settings
from .state import STRATEGY

logger = logging.getLogger("super_bollinger_shadow_list")

SHADOW_STRATEGY = "SuperBollingerShadow"
EVENTS_LOG = "super_bollinger_shadow_events"
PAPER_TRADES_LOG = "super_bollinger_shadow_paper_trades"

shadow_book = PaperBook("data/super_bollinger_shadow_paper_positions.json", PAPER_TRADES_LOG, {
    "strategy": SHADOW_STRATEGY, "entry_mode": "resting", "sides": "long",
    "exit_mode": "max loss + breakeven stop + daily square-off (calls only)", "list": stock_selection.SHADOW_RULE})
PROFILE = engine.Profile(
    name=SHADOW_STRATEGY, entry_mode="resting", sides="long", exit_mode="hold_to_close",
    roll_days=settings.get("roll_expiry_within_trading_days"), daily_square_off_time=settings.get("square_off_time"),
    paper_book=shadow_book, events_log=EVENTS_LOG, paper_only=True,
)

_running = {"on": False}
_list_cache: dict = {"mtime": None, "symbols": [], "as_of": None}


async def _event(event: str, symbol: str, detail: dict) -> None:
    await engine._record_bollinger_event(event, symbol, {"strategy": SHADOW_STRATEGY, **detail}, EVENTS_LOG)


def shadow_symbols() -> tuple[list[str], Optional[str]]:
    """(symbols, as_of) of the latest recorded shadow list; re-read only when the file changes."""
    try:
        mtime = stock_selection.SHADOW_FILE.stat().st_mtime
    except OSError:
        return [], None
    if mtime != _list_cache["mtime"]:
        try:
            record = json.loads(stock_selection.SHADOW_FILE.read_text())
            _list_cache.update(mtime=mtime, symbols=list(record["shadow"]["symbols"]), as_of=record.get("as_of"))
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not read %s", SHADOW_STRATEGY, stock_selection.SHADOW_FILE)
            return [], None
    excluded = set(settings.get("excluded_symbols"))
    return [s for s in _list_cache["symbols"] if s not in excluded and s not in bcfg.INDEX_SYMBOLS], _list_cache["as_of"]


def kick(square_off: bool, entries_open: bool) -> None:
    """From the Super Bollinger monitor tick: start one background pass unless one is still running."""
    if settings.get("shadow_list_mode") == "off" or _running["on"]:
        return
    if not shadow_book.positions and not entries_open:
        return
    _running["on"] = True
    asyncio.create_task(_pass(square_off, entries_open))


async def _pass(square_off: bool, entries_open: bool) -> None:
    try:
        await _exits(square_off)
        if entries_open and not square_off:
            await _entries()
    except Exception:  # noqa: BLE001
        logger.exception("[%s] shadow pass failed", SHADOW_STRATEGY)
    finally:
        _running["on"] = False


async def _exits(square_off: bool) -> None:
    from .trading_engine import _position_exit_reason
    for symbol, pos in list(shadow_book.positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            continue
        if square_off:
            await _close(symbol, ltp, "DAILY_SQUARE_OFF")
            continue
        pos = await shadow_book.update(symbol, ltp)
        if pos is None:
            continue
        reason = _position_exit_reason(pos, ltp)
        if reason:
            await _close(symbol, ltp, reason)


async def _close(symbol: str, price: float, reason: str) -> None:
    record = await shadow_book.close(symbol, price, reason)
    if record is not None:
        await _event("SHADOW_PAPER_CLOSED", symbol, record)


async def _entries() -> None:
    from .trading_engine import _new_position
    symbols, as_of = shadow_symbols()
    cap = capacity_control.get_max_concurrent_trades(STRATEGY)
    PROFILE.roll_days = settings.get("roll_expiry_within_trading_days")
    for i, symbol in enumerate(symbols):
        if len(shadow_book.positions) >= cap:
            return
        if symbol in shadow_book.positions or not signals._symbol_market_open(symbol):
            continue
        if i and not signals.is_symbol_ws_fresh(symbol):
            await asyncio.sleep(bcfg.SYMBOL_PACING_SECONDS)
        entry = await engine._evaluate_entry_signal(symbol, PROFILE)
        if not entry:
            continue
        trigger_price, stop_price = entry[1], entry[2]
        apply_1h = _shadow_applies_1h_filter()
        h1_green = None
        if apply_1h or settings.get("entry_filter_1h") != "off":
            h1_green, detail = await entry_filters.last_hour_green(symbol)
            if apply_1h and h1_green is False:
                await _event("SHADOW_ENTRY_SKIPPED_1H_RED", symbol, {"trigger_price": trigger_price, **detail})
                continue
        try:
            leg = await engine._resolve_option_leg(symbol, "BULLISH", PROFILE)
        except engine._SkipEntry as skip:
            await _event("SHADOW_ENTRY_SKIPPED", symbol, {"trigger_price": trigger_price, **skip.result})
            continue
        leg["quantity"] = leg["lot_size"] * settings.get("quantity_lots")
        try:
            price = await dhan_wrapper.get_option_ltp_async(leg["trading_symbol"])
        except Exception:  # noqa: BLE001
            price = None
        if not price or price < settings.get("min_premium_rs"):
            await _event("SHADOW_ENTRY_SKIPPED", symbol, {"trigger_price": trigger_price, "reason": "no_or_low_premium",
                                                          "trading_symbol": leg["trading_symbol"], "premium": price})
            continue
        pos = _new_position(symbol, leg, price, "SHADOW")
        if not await shadow_book.open(pos):
            continue
        try:
            await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.subscribe_option_price,
                                                             leg["trading_symbol"])
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not WS-subscribe %s", SHADOW_STRATEGY, symbol, leg["trading_symbol"])
        await _event("SHADOW_PAPER_OPENED", symbol, {"trading_symbol": leg["trading_symbol"], "entry_price": price,
                                                     "quantity": leg["quantity"], "trigger_price": trigger_price,
                                                     "stop_price": stop_price, "list_as_of": as_of,
                                                     "entry_filter_1h_applied": apply_1h, "h1_green": h1_green})


def _shadow_applies_1h_filter() -> bool:
    """shadow_list_entry_filter_1h: follow (= the live entry_filter_1h being
    "on"), on, or off (1 Oct 2026 - run the shadow list unfiltered on paper)."""
    mode = settings.get("shadow_list_entry_filter_1h")
    if mode == "follow":
        return settings.get("entry_filter_1h") == "on"
    return mode == "on"


def load() -> list:
    """Startup: restore open shadow paper positions (and their WS subscriptions)."""
    positions = shadow_book.load()
    for pos in positions:
        try:
            dhan_wrapper.subscribe_option_price(pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not re-subscribe %s", SHADOW_STRATEGY, pos.trading_symbol)
    return positions
