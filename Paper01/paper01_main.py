"""
Paper01: real-time, paper-only twin of the Options strategy (user request
15 Sep 2026). Exact same entry/exit rules as Options (see
Paper01/trading_engine.py's own docstring for exactly what's reused vs.
what's independently scoped), but NEVER places a real order - every
"entry"/"exit" is a log line plus a persisted paper-trade record.

Accepts Chartink scanner webhook alerts on two endpoints, mirroring
Options/option_main.py's own two real webhooks exactly:
   - POST /paper01/webhook       (bullish scan -> paper-buys ATM CE)
   - POST /paper01/webhook-sell  (bearish scan -> paper-buys ATM PE)

Depends on Options' lifespan having already authenticated with Dhan and
started the market-data feed (same dependency IndexScalping/Futures/K01/
Luxury/Swing already have) - must be nested inside option_main.lifespan(app)
in main.py.

GET /paper01/positions and GET /paper01/trades are the observability
surface - the latter answers "give me results for all P&L throughout
paper trades taken", with a day-wise breakdown included.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI

from trade_history import fire_and_forget, record_webhook_alert

from Options import config as options_config
from Options.dhan_client import dhan_wrapper
from Options.option_main import ChartinkWebhookPayload
from Options.trading_engine import (
    is_past_allowed_trading_time,
    is_past_square_off_time,
    is_within_trading_windows,
    rank_and_pick_top_stocks,
)

from . import config
from .position_store import paper_position_store as store
from .trading_engine import (
    enter_paper_positions_for_stocks,
    on_paper_price_tick,
    paper_monitor_loop,
)

logger = logging.getLogger("paper01_main")

router = APIRouter()

_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    assert config.PAPER_TRADING_ONLY, "Refusing to start: PAPER_TRADING_ONLY must stay True for Paper01."
    global _monitor_task
    loop = asyncio.get_running_loop()

    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_paper_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_price_tick)

    _monitor_task = asyncio.create_task(paper_monitor_loop())
    logger.info("Paper01 startup complete: PAPER TRADING ONLY - no real orders will ever be placed.")
    yield
    if _monitor_task:
        _monitor_task.cancel()


async def _handle_paper_webhook(payload: ChartinkWebhookPayload, option_type: str, prefer_highest: bool) -> dict:
    await store.maybe_reset_for_new_day()
    stocks = payload.stock_list()

    def _log_alert(status: str, reason: Optional[str] = None) -> None:
        fire_and_forget(record_webhook_alert(
            "Paper01", payload.scan_name, payload.alert_name, stocks, status, reason,
        ))

    if not is_within_trading_windows():
        _log_alert("ignored", "outside_trading_windows")
        return {"status": "ignored", "reason": "outside_trading_windows"}

    if is_past_allowed_trading_time():
        _log_alert("ignored", "past_allowed_trading_time")
        return {"status": "ignored", "reason": "past_allowed_trading_time"}

    if is_past_square_off_time():
        _log_alert("ignored", "past_square_off_time")
        return {"status": "ignored", "reason": "past_square_off_time"}

    logger.info("Paper01 webhook received (%s): scan=%s alert=%s stocks=%s",
                option_type, payload.scan_name, payload.alert_name, stocks)

    if option_type == "CE" and options_config.ENABLE_GAP_DOWN_CE_DELAY and dhan_wrapper.should_delay_ce_entry():
        # Same real-market-condition gate Options/Futures/Luxury apply -
        # kept for entry-rule fidelity (PE is never gated by this, same as
        # the real webhooks).
        _log_alert("ignored", "nifty_gap_down_ce_delay")
        return {"status": "ignored", "reason": "nifty_gap_down_ce_delay"}

    remaining = await store.remaining_capacity(option_type)
    if remaining == 0:
        _log_alert("ignored", "max_live_positions_reached")
        return {"status": "ignored", "reason": "max_live_positions_reached", "option_type": option_type}

    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(
        None, rank_and_pick_top_stocks, stocks, options_config.TOP_N_STOCKS, prefer_highest
    )
    if not ranked:
        _log_alert("no_action", "could_not_rank_any_stock")
        return {"status": "no_action", "reason": "could_not_rank_any_stock"}

    results = await enter_paper_positions_for_stocks(ranked, option_type)
    _log_alert("processed")
    return {"status": "processed", "ranked_by_day_change_pct": ranked, "entries": results}


@router.post("/paper01/webhook")
async def paper01_webhook(payload: ChartinkWebhookPayload):
    """Bullish scan - paper-buys ATM CE. Never places a real order."""
    return await _handle_paper_webhook(payload, option_type="CE", prefer_highest=True)


@router.post("/paper01/webhook-sell")
async def paper01_webhook_sell(payload: ChartinkWebhookPayload):
    """Bearish scan - paper-buys ATM PE. Never places a real order."""
    return await _handle_paper_webhook(payload, option_type="PE", prefer_highest=False)


@router.get("/paper01/positions")
async def paper01_positions():
    return await store.snapshot_open()


@router.get("/paper01/trades")
async def paper01_trades():
    """Full paper-trade history plus a computed summary and day-wise
    breakdown - the answer to "give me results for all P&L throughout
    paper trades taken"."""
    trades = store.all_completed_trades()
    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)

    by_date: dict = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for t in trades:
        d = t["opened_at"][:10]
        by_date[d]["pnl"] += t["pnl"]
        by_date[d]["trades"] += 1
        if t["pnl"] > 0:
            by_date[d]["wins"] += 1
        elif t["pnl"] < 0:
            by_date[d]["losses"] += 1

    return {
        "paper_trading_only": config.PAPER_TRADING_ONLY,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / len(trades)) if trades else None,
        "total_pnl": total_pnl,
        "day_wise": dict(sorted(by_date.items())),
        "trades": trades,
    }
