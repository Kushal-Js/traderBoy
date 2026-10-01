"""
Scalper (1 Oct 2026) - `lifespan` and `router`, composed into the shared app by main.py after Super
Bollinger's (it reuses Bollinger's engine functions, Options' authenticated Dhan connection and Swing's
WS candle feed). Strategy: Scalper/trading_engine.py; settings: Scalper/settings.py.

Endpoints:
  GET  /scalper/config          every setting + paper mode, and where each value comes from
  POST /scalper/config          change any of them at runtime (no restart), e.g. {"paper_mode_enabled": true}
                                or {"daily_loss_limit_rs": 3000, "strategy_enabled": false}
  GET  /scalper/positions       open real + paper positions, today's orders, today's P&L, daily-stop state
  GET  /scalper/signal          the last 1-minute candle's signal evaluation per symbol
  GET  /scalper/trades?day=     closed real + paper trades for a day, with totals
  POST /scalper/square-off-now  manual kill switch - exits every open real AND paper position
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException

import capacity_control
import paper_mode_control
from trade_history import REAL_TRADES_NAME, dated_path
from Options.dhan_client import dhan_wrapper
from Swing import candle_feed

from . import settings, signals
from .state import PAPER_TRADES_LOG, STRATEGY, paper_book, position_store
from .trading_engine import (_last_eval, _consumed, day_pnl, daily_stop_hit, is_paper, monitor_loop, on_price_tick,
                             reconcile_broker_positions, square_off_all, square_off_paper)

logger = logging.getLogger("scalper_main")
router = APIRouter()
_monitor_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reconciliation and the monitor loop always start (even with strategy_enabled=false) so an
    already-open real position is always managed to its exit."""
    global _monitor_task
    try:
        reconciled = await reconcile_broker_positions()
        if reconciled:
            await position_store.reconcile_from_broker(reconciled)
            logger.info("[%s] reconciled %d open REAL position(s): %s", STRATEGY, len(reconciled),
                        [p.trading_symbol for p in reconciled])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not reconcile broker positions at startup - continuing without them", STRATEGY)
    for pos in paper_book.load():
        try:
            dhan_wrapper.subscribe_option_price(pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not re-subscribe %s", STRATEGY, pos.trading_symbol)
    loop = asyncio.get_running_loop()

    def _on_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_tick)
    for symbol in settings.get("symbols"):
        signals.book(symbol)
    candle_feed.add_tick_listener(signals.on_tick)
    _monitor_task = asyncio.create_task(monitor_loop())
    logger.info("[%s] startup complete: mode=%s settings=%s", STRATEGY, "PAPER" if is_paper() else "REAL",
                {k: v["value"] for k, v in settings.snapshot().items()})
    try:
        yield
    finally:
        if _monitor_task:
            _monitor_task.cancel()


def _paper_state() -> dict:
    return {"paper_mode_enabled": is_paper(), "paper_mode_source": paper_mode_control.paper_mode_source(STRATEGY),
            "max_concurrent_trades": capacity_control.get_max_concurrent_trades(STRATEGY),
            "max_concurrent_trades_source": capacity_control.capacity_source(STRATEGY)}


@router.get("/scalper/config")
async def get_config() -> dict:
    return {"strategy": STRATEGY, **_paper_state(), "settings": settings.snapshot()}


@router.post("/scalper/config")
async def post_config(body: dict[str, Any]) -> dict:
    body = dict(body or {})
    paper = body.pop("paper_mode_enabled", None)
    cap = body.pop("max_concurrent_trades", None)
    parsed, errors = settings.validate(body)
    for symbol in parsed.get("symbols", []):       # every new symbol must resolve (NSE index / F&O stock, not MCX)
        try:
            await asyncio.get_running_loop().run_in_executor(None, signals.reference, symbol)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"symbols: {symbol} cannot be traded ({exc})")
    if cap is not None:
        try:
            cap = int(cap)
            if not 1 <= cap <= 10:
                raise ValueError("must be 1-10")
        except (TypeError, ValueError) as exc:
            errors.append(f"max_concurrent_trades: {exc}")
    if paper is not None:
        try:
            paper = settings._parse_bool(paper)
        except ValueError as exc:
            errors.append(f"paper_mode_enabled: {exc}")
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    env_synced = await settings.update(parsed) if parsed else None
    if paper is not None:
        await paper_mode_control.set_paper_mode(STRATEGY, paper)
    if cap is not None:
        await capacity_control.set_max_concurrent_trades(STRATEGY, cap)
    return {"applied": {**parsed, **({"paper_mode_enabled": paper} if paper is not None else {}),
                        **({"max_concurrent_trades": cap} if cap is not None else {})},
            "env_synced": env_synced, **_paper_state(), "settings": settings.snapshot()}


@router.get("/scalper/positions")
async def get_positions() -> dict:
    snap = await position_store.snapshot()
    pnl = await day_pnl()
    return {"strategy": STRATEGY, **_paper_state(), "open_count": len(position_store.live_positions),
            "paper_open_count": len(paper_book.positions), **snap,
            "paper_open_positions": paper_book.snapshot()["open_positions"], "realised_today": pnl,
            "daily_loss_limit_rs": settings.get("daily_loss_limit_rs"),
            "daily_stop_hit": await daily_stop_hit(is_paper())}


@router.get("/scalper/signal")
async def get_signal() -> dict:
    out = {}
    for symbol in settings.get("symbols"):
        b = signals.book(symbol)
        out[symbol] = {"last_evaluation": _last_eval.get(symbol), "consumed_side": _consumed.get(symbol),
                       "live_spot": signals.live_spot(symbol), "ws_bars": len(b.ws), "rest_1m_bars": len(b.rest1),
                       "rest_15m_bars": len(b.rest15)}
    return out


def _read(name: str, day: date) -> list[dict]:
    p = dated_path(name, day)
    rows = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                rows.append(json.loads(line[line.index("{"):]))
            except Exception:  # noqa: BLE001
                continue
    return rows


@router.get("/scalper/trades")
async def get_trades(day: Optional[str] = None) -> dict:
    d = date.fromisoformat(day) if day else date.today()
    real = [r for r in _read(REAL_TRADES_NAME, d) if r.get("strategy") == STRATEGY]
    paper = _read(PAPER_TRADES_LOG, d)
    return {"strategy": STRATEGY, "day": d.isoformat(),
            "real": {"count": len(real), "wins": sum(1 for r in real if (r.get("pnl") or 0) > 0),
                     "total_pnl": round(sum(r.get("pnl") or 0 for r in real), 2), "trades": real},
            "paper": {"count": len(paper), "wins": sum(1 for r in paper if (r.get("pnl_modeled") or 0) > 0),
                      "total_pnl_raw": round(sum(r.get("pnl_raw") or 0 for r in paper), 2),
                      "total_pnl_modeled": round(sum(r.get("pnl_modeled") or 0 for r in paper), 2), "trades": paper}}


@router.post("/scalper/square-off-now")
async def square_off_now() -> dict:
    real_before = sorted(position_store.live_positions)
    paper_before = sorted(paper_book.positions)
    await square_off_all("MANUAL_SQUARE_OFF")
    await square_off_paper("MANUAL_SQUARE_OFF")
    return {"real_exits_attempted": real_before, "paper_closed": paper_before,
            "real_still_open": sorted(position_store.live_positions)}
