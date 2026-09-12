"""
Swing v2 (complete rewrite, 12 Sep 2026 - see Swing/config.py's module
docstring for the full design). No Chartink integration, no dedicated
entry webhook (user request: "No watchlist pruning logic or a separate
webhook endpoint required as of now") - the bot decides when to enter by
continuously evaluating its own signals against a plain, manually-edited
watchlist, not by reacting to an inbound alert.

`lifespan` and `router` are composed into the shared app by the top-level
main.py, the same way every other strategy package is - mounted *inside*
Options' own lifespan (after it), reusing Options' single authenticated
Dhan connection and WebSocket feed.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from Options.dhan_client import dhan_wrapper

from . import config, signals
from .position_store import position_store
from .trading_engine import monitor_loop, on_price_tick, reconcile_broker_positions
from .watchlist import watchlist_store

logger = logging.getLogger("swing_main")

router = APIRouter()

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Does NOT authenticate or start the Dhan feed - reuses Options'
    already-authenticated connection. Reconciliation and the monitor loop
    always run regardless of config.STRATEGY_ENABLED - a real, still-open
    position from before a restart needs to be picked back up whether or
    not NEW entries are currently allowed, and flipping STRATEGY_ENABLED
    should never need a restart to take effect."""
    global _monitor_task

    try:
        await watchlist_store.sync_from_file()
    except Exception:  # noqa: BLE001
        logger.exception("Could not sync watchlist from data/watchlist at startup - continuing without it.")

    try:
        reconciled = await reconcile_broker_positions()
        if reconciled:
            await position_store.reconcile_from_broker(reconciled)
            logger.info("Reconciled %d existing Swing position(s) at startup: %s",
                        len(reconciled), [p.underlying_symbol for p in reconciled])
    except Exception:  # noqa: BLE001
        logger.exception("Could not reconcile broker positions at startup - continuing without them.")

    # Wire the WebSocket tick feed - additive (Options/Futures/Luxury each
    # register their own subscriber the same way; this doesn't replace
    # theirs). Only futures/options positions ever get a match here
    # (equity is never WS-subscribed - see Swing/config.py's own note).
    loop = asyncio.get_running_loop()

    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_price_tick)

    _monitor_task = asyncio.create_task(monitor_loop())
    logger.info(
        "Swing v2 startup complete: monitor loop running (reusing Options' Dhan connection). "
        "strategy_enabled=%s basket_type=%s broker_stop_loss_enabled=%s max_concurrent_trades=%s",
        config.STRATEGY_ENABLED, config.BASKET_TYPE, config.BROKER_STOP_LOSS_ENABLED, config.MAX_CONCURRENT_TRADES,
    )
    yield
    if _monitor_task:
        _monitor_task.cancel()


class WatchlistPayload(BaseModel):
    stocks: str  # comma-separated, same convention as every other payload in this codebase

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]


class WatchlistReplacePayload(BaseModel):
    """A real JSON array, not the comma-separated `stocks` string the
    add/remove endpoints use - added 12 Sep 2026 per explicit request
    ("post a JSON data") for the bulk-replace endpoint below."""
    symbols: list[str]


# --------------------------------------------------------------------------- #
# Watchlist management (user provides stocks directly - requirement #1)
# --------------------------------------------------------------------------- #
@router.post("/swing/watchlist/add")
async def add_to_watchlist(payload: WatchlistPayload):
    stocks = payload.stock_list()
    added = await watchlist_store.add_symbols(stocks)
    return {"requested": stocks, "added": added, "already_on_watchlist": [s for s in stocks if s not in added]}


@router.post("/swing/watchlist/remove")
async def remove_from_watchlist(payload: WatchlistPayload):
    stocks = payload.stock_list()
    removed = [s for s in stocks if await watchlist_store.remove_symbol(s)]
    return {"requested": stocks, "removed": removed}


@router.post("/swing/watchlist/replace")
async def replace_watchlist(payload: WatchlistReplacePayload):
    """Wipes the ENTIRE watchlist and replaces it with exactly the given
    symbols - added 12 Sep 2026, user request: "replace existing
    watchlist contents with this json payload... replace entire content
    of watchlist file any time". Unlike /add or /remove, this is NOT
    additive: any symbol not in this payload stops being watched
    immediately. Also rewrites data/watchlist (backed up first) so the
    replacement survives a restart - see WatchlistStore.persist_to_file's
    own docstring for why skipping that would let old symbols silently
    reappear later. Does NOT touch any already-open position for a
    removed symbol - only stops new entries on it.

    Example body: {"symbols": ["ADANIPORTS", "COALINDIA", "COPPER"]}"""
    new_watchlist = await watchlist_store.replace_symbols(payload.symbols)
    await watchlist_store.persist_to_file()
    return {"requested": payload.symbols, "watchlist": new_watchlist, "count": len(new_watchlist)}


@router.get("/swing/watchlist")
async def get_watchlist():
    return await watchlist_store.snapshot()


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
@router.get("/swing/positions")
async def get_positions():
    return await position_store.snapshot()


@router.get("/swing/signals")
async def get_signals():
    """Cache-only regime + Supertrend state for every watchlist symbol -
    no live fetch (see Swing/signals.py's own peek_* functions). This is
    the main rollout safety tool: watch this for a full trading day with
    STRATEGY_ENABLED=false before trusting it against real money - it
    lets the strategy's own reasoning be observed directly rather than
    inferred from whether a trade happened to fire."""
    out = []
    for symbol in await watchlist_store.symbols():
        regime = signals.peek_regime_state(symbol)
        st = signals.peek_supertrend_state(symbol)
        out.append({
            "symbol": symbol,
            "regime": None if regime is None else {
                "is_bullish": regime.is_bullish, "fast_ema": regime.fast_ema, "slow_ema": regime.slow_ema,
                "fast_candle_start": regime.fast_candle_start.isoformat() if regime.fast_candle_start else None,
                "slow_candle_start": regime.slow_candle_start.isoformat() if regime.slow_candle_start else None,
            },
            "supertrend": None if st is None else {
                "close": st.close, "supertrend": st.supertrend, "is_above": st.is_above,
                "crossed_above": st.crossed_above, "crossed_below": st.crossed_below,
                "candle_start": st.candle_start.isoformat() if st.candle_start else None,
            },
        })
    return {"signals": out}


@router.post("/swing/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live Swing position immediately -
    works regardless of config.STRATEGY_ENABLED (an open position should
    always be closeable, even while new entries are currently disabled)."""
    from .trading_engine import _square_off_all  # local import to avoid a cycle at module load time
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()
