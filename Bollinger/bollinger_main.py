"""
Bollinger strategy (added 26 Sep 2026 - see Bollinger/config.py's own
module docstring for the full design). `lifespan` and `router` are
composed into the shared app by the top-level main.py, mounted inside
Swing's own lifespan block (Bollinger reuses Swing's WS candle-feed
module directly - see Bollinger/signals.py).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

import json
from datetime import date

from trade_history import dated_path
from Options.dhan_client import dhan_wrapper

from . import config, signals
from .paper_book import PAPER_TRADES_LOG_NAME, paper_book
from .position_store import position_store
from .trading_engine import _entry_backlog, monitor_loop, on_price_tick, reconcile_broker_positions
from .watchlist import watchlist_store

logger = logging.getLogger("bollinger_main")

router = APIRouter()

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Does NOT authenticate or start the Dhan feed - reuses Options'
    already-authenticated connection and Swing's own candle_feed module.
    Reconciliation and the monitor loop always run regardless of
    config.STRATEGY_ENABLED, same reasoning as every other package here:
    a real, still-open position from before a restart needs to be picked
    back up whether or not NEW entries are currently allowed."""
    global _monitor_task

    try:
        await watchlist_store.sync_from_file()
    except Exception:  # noqa: BLE001
        logger.exception("Could not sync watchlist from data/bollinger_watchlist at startup - continuing without it.")

    try:
        reconciled = await reconcile_broker_positions()
        if reconciled:
            await position_store.reconcile_from_broker(reconciled)
            logger.info("Reconciled %d existing Bollinger position(s) at startup: %s",
                        len(reconciled), [p.underlying_symbol for p in reconciled])
    except Exception:  # noqa: BLE001
        logger.exception("Could not reconcile broker positions at startup - continuing without them.")

    # Open PAPER positions survive restarts (Bollinger/paper_book.py) - reload
    # them and re-subscribe their option prices so exits keep working.
    for pos in paper_book.load():
        try:
            dhan_wrapper.subscribe_option_price(pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("Could not re-subscribe %s for a restored paper position", pos.trading_symbol)

    loop = asyncio.get_running_loop()

    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_price_tick)

    _monitor_task = asyncio.create_task(monitor_loop())

    logger.info(
        "Bollinger strategy startup complete: monitor loop running (reusing Options' Dhan connection and "
        "Swing's WS candle feed). strategy_enabled=%s paper_mode_enabled=%s broker_stop_loss_enabled=%s "
        "max_concurrent_trades=%s fund_bucket=%s entry_mode=%s min_stop_pct=%s min_atm_premium_rs=%s "
        "sides=%s exit_mode=%s daily_square_off_time=%s open_paper_positions=%d",
        config.STRATEGY_ENABLED, config.PAPER_MODE_ENABLED, config.BROKER_STOP_LOSS_ENABLED,
        config.MAX_CONCURRENT_TRADES, config.FUND_BUCKET, config.ENTRY_MODE, config.MIN_STOP_PCT,
        config.MIN_ATM_PREMIUM_RS, config.SIDES, config.EXIT_MODE, config.DAILY_SQUARE_OFF_TIME,
        len(paper_book.positions),
    )
    if not config.PAPER_MODE_ENABLED:
        logger.warning(
            "Bollinger PAPER_MODE_ENABLED=False - REAL ORDERS will be placed for any symbol added to "
            "data/bollinger_watchlist. This strategy has no prior live/paper track record beyond its own "
            "30-day backtest - see trading-skills' learnings/bollinger-vortex-strategy-30day-backtest.md.",
        )
    yield
    if _monitor_task:
        _monitor_task.cancel()


class WatchlistPayload(BaseModel):
    stocks: str  # comma-separated, same convention as every other payload in this codebase

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]


class WatchlistReplacePayload(BaseModel):
    symbols: list[str]


# --------------------------------------------------------------------------- #
# Watchlist management
# --------------------------------------------------------------------------- #
@router.post("/bollinger/watchlist/add")
async def add_to_watchlist(payload: WatchlistPayload):
    stocks = payload.stock_list()
    added = await watchlist_store.add_symbols(stocks)
    return {"requested": stocks, "added": added, "already_on_watchlist": [s for s in stocks if s not in added]}


@router.post("/bollinger/watchlist/remove")
async def remove_from_watchlist(payload: WatchlistPayload):
    stocks = payload.stock_list()
    removed = [s for s in stocks if await watchlist_store.remove_symbol(s)]
    return {"requested": stocks, "removed": removed}


@router.post("/bollinger/watchlist/replace")
async def replace_watchlist(payload: WatchlistReplacePayload):
    """Example body: {"symbols": ["BANDHANBNK", "TORNTPHARM"]}"""
    new_watchlist = await watchlist_store.replace_symbols(payload.symbols)
    await watchlist_store.persist_to_file()
    return {"requested": payload.symbols, "watchlist": new_watchlist, "count": len(new_watchlist)}


@router.get("/bollinger/watchlist")
async def get_watchlist():
    return await watchlist_store.snapshot()


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
@router.get("/bollinger/positions")
async def get_positions():
    return await position_store.snapshot()


@router.get("/bollinger/paper-trades")
async def get_paper_trades(day: Optional[str] = None):
    """Paper book (28 Sep 2026): open paper positions plus the closed paper
    trades logged on `day` (YYYY-MM-DD, default today) with running totals.
    pnl_raw = at live quotes; pnl_modeled = with the backtests' slippage
    model charged on entry and exit (the like-for-like number to compare
    against bollinger_research.py)."""
    d = date.fromisoformat(day) if day else date.today()
    path = dated_path(PAPER_TRADES_LOG_NAME, d)
    closed = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    return {
        **paper_book.snapshot(),
        "day": d.isoformat(),
        "closed_trades": closed,
        "closed_count": len(closed),
        "wins": sum(1 for t in closed if t["pnl_modeled"] > 0),
        "total_pnl_raw": round(sum(t["pnl_raw"] for t in closed), 2),
        "total_pnl_modeled": round(sum(t["pnl_modeled"] for t in closed), 2),
    }


@router.get("/bollinger/entry-backlog")
async def get_entry_backlog():
    """Same freshness-priority entry backlog as Swing's own - see
    Swing/swing_main.py's /swing/entry-backlog and entry_backlog.py's
    module docstring. This is Bollinger's own separate instance/state."""
    pending = await _entry_backlog.snapshot()
    return {"count": len(pending), "pending": pending}


@router.get("/bollinger/signals")
async def get_signals():
    """Cache-only signal state for every watchlist symbol - no live fetch.
    Same rollout-safety intent as Swing's own GET /swing/signals: watch
    this before trusting a symbol against real money."""
    out = []
    for symbol in await watchlist_store.symbols():
        state = signals.peek_signal_state(symbol)
        out.append({
            "symbol": symbol,
            "state": None if state is None else {
                "valid_bullish": state.valid_bullish, "valid_bearish": state.valid_bearish,
                "pending_side": state.pending_side, "pending_trigger_price": state.pending_trigger_price,
                "pending_stop_price": state.pending_stop_price,
                "fired": state.fired, "fired_trigger_price": state.fired_trigger_price,
                "fired_stop_price": state.fired_stop_price, "last_close": state.last_close,
                "candle_start": state.candle_start.isoformat() if state.candle_start else None,
            },
        })
    return {"signals": out}


@router.post("/bollinger/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live Bollinger position immediately."""
    from .trading_engine import _square_off_all  # local import to avoid a cycle at module load time
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()
