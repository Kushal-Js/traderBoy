"""
Swing strategy: user request 31 Aug 2026 - buys 1 lot of a stock's
futures contract hedged with 1 lot of its ATM PE option, as an all-or-
nothing "basket" (see trading_engine.py's own module docstring for the
compensating-rollback design, since Dhan has no native basket-order API -
see the separate trading-skills repo's `basket-order-feasibility.md` for
the investigation this is based on).

Entry/exit CONDITION logic is deliberately deferred to the user (see
config.py's own docstring) - what's live today is the MECHANICS: a
continuously-monitored watchlist and two webhooks:
   - POST /chartink/webhook-swing-enter      - directly enters a basket
     for the stock(s) in the payload. A manual/explicit trigger, not a
     scan-ranked selection like Options/Futures/Luxury - the "business
     logic" for WHEN to call this lives outside the bot for now (the
     user's own words: "based on business logic defined" - defined
     later, not embedded here).
   - POST /chartink/webhook-swing-watchlist  - adds stock(s) to the
     watchlist trading_engine.monitor_loop() continuously polls.

DEPLOYED DISABLED (config.STRATEGY_ENABLED=false) - see config.py's own
docstring for why and how the flag works without needing a restart later
to flip it, once entry/exit logic is actually defined.

`lifespan` and `router` are composed into the shared app by the top-level
main.py, the same way every other strategy package is. Reuses the
Options package's single authenticated Dhan connection (dhan_client.py
here just re-exports it, plus the new get_futures_contract) - main.py
must mount this package's lifespan *inside* Options' own (after it), the
same pattern Futures/Luxury/CopperOptions/IndexScalping already use.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, field_validator

from trade_history import fire_and_forget, record_webhook_alert

from . import config
from .paper_engine import paper_basket_store, paper_poll_loop, sequential_paper_store
from .position_store import basket_store, sequential_store
from .trading_engine import (
    _enter_futures_for_stock,
    enter_basket_for_stock,
    monitor_loop,
    reconcile_broker_positions,
    reconcile_sequential_positions,
)
from .watchlist import watchlist_store

logger = logging.getLogger("swing_main")

router = APIRouter()

_monitor_task: Optional[asyncio.Task] = None
_paper_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Swing strategy's own startup/shutdown. Does NOT authenticate or
    start the Dhan feed - reuses Options' already-authenticated
    connection. Reconciliation always runs regardless of
    config.STRATEGY_ENABLED - a real, still-open basket from before a
    restart needs to be picked back up whether or not NEW entries are
    currently allowed. The monitor loop always starts too - see
    config.py's own docstring for why."""
    global _monitor_task, _paper_task

    # Best-effort load of whatever's already in data/watchlist (user
    # request 31 Aug 2026) - so a restart doesn't lose stocks that were
    # only ever added by hand-editing the file, not through the webhook.
    # Runs regardless of config.STRATEGY_ENABLED - populating the
    # watchlist is inert on its own; only the entry SIGNAL evaluation
    # (gated by the flag) can actually act on it. See watchlist.py's own
    # docstring.
    try:
        await watchlist_store.sync_from_file()
    except Exception:  # noqa: BLE001
        logger.exception("Could not sync watchlist from data/watchlist at startup - continuing without it.")

    # Mode-aware (added 1 Sep 2026) - a lone Swing-attributed broker leg
    # means something different in each mode (an anomaly in basket mode,
    # the NORMAL shape in sequential mode), so each mode runs its own
    # reconciliation function against its own store. See
    # reconcile_sequential_positions's own docstring.
    if config.STRATEGY_MODE == "basket":
        try:
            reconciled = await reconcile_broker_positions()
            if reconciled:
                await basket_store.reconcile_from_broker(reconciled)
                logger.info(
                    "Reconciled %d existing Swing basket(s) at startup: %s",
                    len(reconciled), [b.underlying_symbol for b in reconciled],
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not reconcile broker baskets at startup - continuing without them.")
    else:
        try:
            reconciled_legs = await reconcile_sequential_positions()
            for leg in reconciled_legs:
                await sequential_store.reconcile_leg(leg)
            if reconciled_legs:
                logger.info(
                    "Reconciled %d existing Swing sequential leg(s) at startup: %s",
                    len(reconciled_legs), [(l.underlying_symbol, l.option_type) for l in reconciled_legs],
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not reconcile broker sequential legs at startup - continuing without them.")

    _monitor_task = asyncio.create_task(monitor_loop())
    # Own independent loop, own independent flag (config.PAPER_TRADING_ENABLED)
    # - see paper_engine.py's own module docstring for why this always
    # starts alongside the real monitor loop regardless of either flag.
    _paper_task = asyncio.create_task(paper_poll_loop())
    logger.info(
        "Swing strategy startup complete: monitor loop running (reusing Options' Dhan connection). "
        "strategy_enabled=%s strategy_mode=%s paper_trading_enabled=%s",
        config.STRATEGY_ENABLED, config.STRATEGY_MODE, config.PAPER_TRADING_ENABLED,
    )
    yield
    if _monitor_task:
        _monitor_task.cancel()
    if _paper_task:
        _paper_task.cancel()


# --------------------------------------------------------------------------- #
# Webhook payload schemas (same shape as every other Chartink-style
# payload in this codebase, for consistency - scan_name/alert_name etc.
# are optional here since these two webhooks are meant to be called
# directly/manually, not necessarily from an actual Chartink scan)
# --------------------------------------------------------------------------- #
class SwingWebhookPayload(BaseModel):
    stocks: str
    trigger_prices: Optional[str] = None
    triggered_at: Optional[str] = None
    scan_name: Optional[str] = None
    scan_url: Optional[str] = None
    alert_name: Optional[str] = None
    webhook_url: Optional[str] = None

    @field_validator("stocks")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("stocks must not be empty")
        return v

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]


# --------------------------------------------------------------------------- #
# Webhook endpoints
# --------------------------------------------------------------------------- #
@router.post("/chartink/webhook-swing-enter")
async def chartink_webhook_swing_enter(payload: SwingWebhookPayload):
    """Directly enters a basket (futures + ATM PE, all-or-nothing) for
    each stock in the payload, one at a time (deliberately sequential,
    not concurrent like Options/Futures/Luxury's own multi-stock ranking -
    each basket entry is already a multi-step, two-leg operation; keeping
    multiple basket entries from interleaving keeps the all-or-nothing
    rollback easy to reason about while this is new). See this module's
    own docstring for what "based on business logic defined" means here -
    the decision of WHICH stock to send and WHEN lives outside the bot
    for now; this endpoint just executes the entry mechanics.

    Mode-aware (added 1 Sep 2026): under config.STRATEGY_MODE ==
    "sequential", this drives the SAME NONE -> FUTURES transition
    monitor_loop's own entry-signal path uses (futures only, no PE leg
    yet - the PE only ever comes in as the hedge once the futures leg
    later exits) rather than the basket entry, and checks
    sequential_store's own capacity instead of basket_store's."""
    stocks = payload.stock_list()

    def _log_alert(status: str, reason: Optional[str] = None) -> None:
        fire_and_forget(record_webhook_alert(
            "Swing", payload.scan_name or "manual", payload.alert_name or "swing-enter", stocks, status, reason,
        ))

    if not config.STRATEGY_ENABLED:
        logger.info("Swing enter webhook received but strategy is disabled - ignoring: stocks=%s", stocks)
        _log_alert("ignored", "strategy_disabled")
        return {"status": "ignored", "reason": "strategy_disabled", "stocks": stocks}

    logger.info("Swing enter webhook received: stocks=%s mode=%s", stocks, config.STRATEGY_MODE)
    store = basket_store if config.STRATEGY_MODE == "basket" else sequential_store
    remaining = await store.remaining_capacity()
    if remaining == 0:
        logger.info("No capacity left (%s live/in-flight already) - ignoring alert.", config.MAX_LIVE_BASKETS)
        _log_alert("ignored", "max_live_baskets_reached")
        return {
            "status": "ignored", "reason": "max_live_baskets_reached",
            "max_live_baskets": config.MAX_LIVE_BASKETS,
        }

    entry_fn = enter_basket_for_stock if config.STRATEGY_MODE == "basket" else _enter_futures_for_stock
    results = [await entry_fn(symbol) for symbol in stocks]
    _log_alert("processed")
    return {"status": "processed", "mode": config.STRATEGY_MODE, "entries": results}


@router.post("/chartink/webhook-swing-watchlist")
async def chartink_webhook_swing_watchlist(payload: SwingWebhookPayload):
    """Adds stock(s) to the watchlist trading_engine.monitor_loop()
    continuously polls - see this module's own docstring."""
    stocks = payload.stock_list()

    def _log_alert(status: str, reason: Optional[str] = None) -> None:
        fire_and_forget(record_webhook_alert(
            "Swing-Watchlist", payload.scan_name or "manual", payload.alert_name or "swing-watchlist",
            stocks, status, reason,
        ))

    if not config.STRATEGY_ENABLED:
        logger.info("Swing watchlist webhook received but strategy is disabled - ignoring: stocks=%s", stocks)
        _log_alert("ignored", "strategy_disabled")
        return {"status": "ignored", "reason": "strategy_disabled", "stocks": stocks}

    added = await watchlist_store.add_symbols(stocks)
    logger.info("Swing watchlist webhook received: stocks=%s added=%s", stocks, added)
    _log_alert("processed")
    return {
        "status": "processed", "requested": stocks, "added": added,
        "already_on_watchlist": [s for s in stocks if s not in added],
    }


# --------------------------------------------------------------------------- #
# Observability endpoints
# --------------------------------------------------------------------------- #
@router.get("/swing/positions")
async def get_positions():
    """Basket-mode positions - live_baskets/reserved_symbols read empty
    while config.STRATEGY_MODE == "sequential" (that store is simply
    never written to under sequential mode) - see GET /swing/sequential-
    positions for the currently-active mode's own view."""
    return {"strategy_mode": config.STRATEGY_MODE} | await basket_store.snapshot()


@router.get("/swing/sequential-positions")
async def get_sequential_positions():
    """Sequential-mode positions (added 1 Sep 2026) - live_legs/
    reserved_symbols read empty while config.STRATEGY_MODE == "basket"."""
    return {"strategy_mode": config.STRATEGY_MODE} | await sequential_store.snapshot()


@router.get("/swing/watchlist")
async def get_watchlist():
    return await watchlist_store.snapshot()


@router.get("/swing/paper-trades")
async def get_paper_trades():
    """Completed + live PAPER baskets, and aggregate pnl/win-rate - see
    paper_engine.py's own docstring. Always reachable (no
    strategy_enabled/paper_trading_enabled gate on the GET itself) so past
    results stay visible even after paper trading is later turned off.
    Basket-mode only - reads empty while config.STRATEGY_MODE ==
    "sequential" (paper trading mirrors whichever mode is active - see
    GET /swing/sequential-paper-trades for that one)."""
    return {"strategy_mode": config.STRATEGY_MODE} | await paper_basket_store.snapshot()


@router.get("/swing/sequential-paper-trades")
async def get_sequential_paper_trades():
    """Sequential-mode PAPER trades (added 1 Sep 2026) - reads empty
    while config.STRATEGY_MODE == "basket"."""
    return {"strategy_mode": config.STRATEGY_MODE} | await sequential_paper_store.snapshot()


@router.post("/swing/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live Swing position immediately -
    both legs of every basket in basket mode, or the single held leg of
    every symbol in sequential mode (returning each straight to plain
    watching, not continuing the loop into a hedge) - works regardless of
    config.STRATEGY_ENABLED (an open position should always be closeable,
    even while new entries are currently disabled). See trading_engine.
    _square_off_all's own docstring for the mode dispatch."""
    from .trading_engine import _square_off_all  # local import to avoid cycles at module load
    await _square_off_all("MANUAL_SQUARE_OFF")
    if config.STRATEGY_MODE == "basket":
        return {"strategy_mode": config.STRATEGY_MODE} | await basket_store.snapshot()
    return {"strategy_mode": config.STRATEGY_MODE} | await sequential_store.snapshot()
