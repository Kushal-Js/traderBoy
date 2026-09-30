"""
Super Bollinger (30 Sep 2026) - `lifespan` and `router`, composed into the
shared app by main.py right after Bollinger's (it reuses Bollinger's signal
cache/watchlist, Options' authenticated Dhan connection and Swing's WS
candle feed). See SuperBollinger/trading_engine.py for the strategy itself
and SuperBollinger/settings.py for the runtime configuration.

Endpoints:
  GET  /super-bollinger/config            every setting, paper mode, capacity, and where each value comes from
  POST /super-bollinger/config            change any of them at runtime (no restart), e.g.
                                          {"paper_mode_enabled": true} or {"breakeven_after_rs": 2000,
                                          "entry_cutoff_time": "13:30", "max_concurrent_trades": 4}
  GET  /super-bollinger/positions         open real + paper positions, today's orders
  GET  /super-bollinger/trades?day=       closed real + paper trades for a day, with PnL totals
  GET  /super-bollinger/symbols           which symbols it trades now (weekly stock list + permanent index symbols)
  GET  /super-bollinger/watchlist         its own HYBRID-picked watchlist (data/super_bollinger_watchlist)
  GET  /super-bollinger/watchlist/shadow  the shadow list (recorded + scored weekly, never traded) vs the live list
  POST /super-bollinger/watchlist/replace replace it by hand, e.g. {"symbols": ["LAURUSLABS", "ZYDUSLIFE"]}
  POST /super-bollinger/square-off-now    manual kill switch - exits every open real position
  GET  /super-bollinger/supervisor        supervisor status: hedge mode, open hedges, today's hedge trades, brake
  POST /super-bollinger/supervisor/square-off-hedges   exits every open REAL hedge (CE trades untouched)
  GET  /super-bollinger/scale?day=        scale-in PAPER variant: open added lots, closed ones, PnL vs the real hedge
  GET  /super-bollinger/restart-report    what the last startup restored from the state file / broker, and what needs a look
  GET  /super-bollinger/live-state        the live state that is persisted for restarts
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

from . import best_price_memory, live_state, scale, settings, shadow_list
from . import supervisor
from . import watchlist as super_watchlist
from .state import (HEDGE_STRATEGY, STRATEGY, SUPERVISOR_LOG, halted, hedge_paper_book, hedge_store, paper_book,
                    position_store)
from .trading_engine import (INDEX_STRATEGY, eligible_symbols, index_symbols, install_tick_entries, monitor_loop,
                             on_price_tick, open_count, reconcile_broker_positions, square_off_all)

logger = logging.getLogger("super_bollinger_main")
router = APIRouter()
_monitor_task: Optional[asyncio.Task] = None
_supervisor_task: Optional[asyncio.Task] = None


async def _legacy_reconcile() -> None:
    """The pre-state-file startup rebuild (broker positions + best-price
    memory) - only used if live_state.restore() itself fails."""
    try:
        reconciled = await reconcile_broker_positions()
        if reconciled:
            best_price_memory.restore(STRATEGY, reconciled)
            await position_store.reconcile_from_broker(reconciled)
            logger.info("[%s] reconciled %d open position(s) at startup: %s", STRATEGY, len(reconciled),
                        [p.underlying_symbol for p in reconciled])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not reconcile broker positions at startup - continuing without them", STRATEGY)
    try:
        hedges = await supervisor.reconcile_hedges()
        if hedges:
            best_price_memory.restore(HEDGE_STRATEGY, hedges)
            await hedge_store.reconcile_from_broker(hedges)
            logger.info("[%s] reconciled %d open REAL hedge(s): %s", STRATEGY, len(hedges), [p.underlying_symbol for p in hedges])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not reconcile hedge positions at startup", STRATEGY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reconciliation and the monitor loop always start (even with
    strategy_enabled=false) so an already-open real position is always
    managed to its exit."""
    global _monitor_task, _supervisor_task
    try:
        # State file + broker + open orders (SuperBollinger/live_state.py): restores every real CE and
        # hedge with its full management state, today's closed trades, the brake, used signals and the
        # supervisor's per-trade memory; resolves orders that were in flight; warms up the data feeds.
        report = await live_state.restore()
        logger.info("[%s] restart reconcile done: %d CE %s, %d hedge(s) %s", STRATEGY, len(report["positions"]),
                    [p["trading_symbol"] for p in report["positions"]], len(report["hedges"]),
                    [p["trading_symbol"] for p in report["hedges"]])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] state-file reconcile failed - falling back to the broker-only rebuild", STRATEGY)
        await _legacy_reconcile()
    try:
        scale.load()
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not restore the scale-in variant's paper legs", STRATEGY)
    for pos in paper_book.load() + hedge_paper_book.load():
        try:
            dhan_wrapper.subscribe_option_price(pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not re-subscribe %s for a restored paper position", STRATEGY,
                             pos.trading_symbol)
    try:
        shadow_list.load()
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not restore the shadow list's paper positions", STRATEGY)

    loop = asyncio.get_running_loop()

    def _on_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    def _on_tick_supervisor(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(supervisor.on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_tick)
    dhan_wrapper.add_price_tick_subscriber(_on_tick_supervisor)
    _supervisor_task = asyncio.create_task(supervisor.supervisor_loop())
    install_tick_entries(loop)  # tick-driven entries off the underlying WS feed
    _monitor_task = asyncio.create_task(monitor_loop())

    real = not paper_mode_control.is_paper_mode_enabled(STRATEGY)
    logger.info("[%s] startup complete: mode=%s max_concurrent_trades=%s settings=%s", STRATEGY,
                "REAL" if real else "PAPER", capacity_control.get_max_concurrent_trades(STRATEGY),
                {k: v["value"] for k, v in settings.snapshot().items()})
    if real:
        logger.warning("[%s] REAL TRADING ON - real orders will be placed for the stocks on data/super_bollinger_watchlist.",
                       STRATEGY)
    idx = index_symbols()
    if idx:
        logger.warning("[%s] index symbols %s are traded (permanent, outside the weekly watchlist) - mode=%s", STRATEGY,
                       idx, "PAPER" if paper_mode_control.is_paper_mode_enabled(INDEX_STRATEGY) else "REAL")
    yield
    if _monitor_task:
        _monitor_task.cancel()
    if _supervisor_task:
        _supervisor_task.cancel()


def _config_view() -> dict:
    return {
        "strategy": STRATEGY,
        "paper_mode_enabled": {"value": paper_mode_control.is_paper_mode_enabled(STRATEGY),
                               "source": paper_mode_control.paper_mode_source(STRATEGY)},
        "index_paper_mode_enabled": {"value": paper_mode_control.is_paper_mode_enabled(INDEX_STRATEGY),
                                     "source": paper_mode_control.paper_mode_source(INDEX_STRATEGY)},
        "max_concurrent_trades": {"value": capacity_control.get_max_concurrent_trades(STRATEGY),
                                  "source": capacity_control.capacity_source(STRATEGY)},
        **settings.snapshot(),
        "index_symbols_traded_now": index_symbols(),
        "always_excluded": "MCX commodities (index symbols are traded only when index_enabled)",
    }


@router.get("/super-bollinger/config")
async def get_config():
    return _config_view()


@router.post("/super-bollinger/config")
async def update_config(payload: dict[str, Any]):
    """Partial update - send only what changes. Validated as a whole before
    anything is applied. Takes effect on the next monitor tick (<= 5s), is
    persisted, and is written to .env too. Changes never touch an already-
    open position's entry, only how it is managed from now on (e.g. a new
    max_loss_rs applies to open trades immediately; the broker-side
    backstop order keeps the level it was placed at)."""
    changes = dict(payload)
    paper = changes.pop("paper_mode_enabled", None)
    index_paper = changes.pop("index_paper_mode_enabled", None)
    capacity = changes.pop("max_concurrent_trades", None)
    parsed, errors = settings.validate(changes)
    if paper is not None and not isinstance(paper, bool):
        errors.append("paper_mode_enabled: must be true or false")
    if index_paper is not None and not isinstance(index_paper, bool):
        errors.append("index_paper_mode_enabled: must be true or false")
    if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0):
        errors.append("max_concurrent_trades: must be an integer >= 0")
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    warnings = []
    if "quantity_lots" in parsed and "max_loss_rs" not in parsed and parsed["quantity_lots"] != settings.get("quantity_lots"):
        warnings.append("quantity_lots changed but max_loss_rs did not - the rupee max-loss cap does not scale "
                        "with lot size; consider scaling max_loss_rs (and breakeven_after_rs) too")
    env_synced = await settings.update(parsed) if parsed else True
    if paper is not None:
        await paper_mode_control.set_paper_mode(STRATEGY, paper)
    if index_paper is not None:
        await paper_mode_control.set_paper_mode(INDEX_STRATEGY, index_paper)
    if capacity is not None:
        await capacity_control.set_max_concurrent_trades(STRATEGY, capacity)
    return {"applied": {**parsed, **({"paper_mode_enabled": paper} if paper is not None else {}),
                        **({"index_paper_mode_enabled": index_paper} if index_paper is not None else {}),
                        **({"max_concurrent_trades": capacity} if capacity is not None else {})},
            "env_synced": env_synced, "warnings": warnings, "config": _config_view()}


@router.get("/super-bollinger/positions")
async def get_positions():
    """open_count = REAL slots in use (paper positions have their own limit since 1 Oct 2026)."""
    return {"open_count": open_count(), "paper_open_count": len(paper_book.positions),
            **await position_store.snapshot(), **paper_book.snapshot()}


@router.get("/super-bollinger/shadow-trades")
async def get_shadow_trades(day: Optional[str] = None):
    """The shadow watchlist traded on PAPER (SuperBollinger/shadow_list.py): the list in use, open paper
    positions and the closed ones of `day` (YYYY-MM-DD, default today), next to the real book's closed
    trades of the same day for comparison."""
    d = date.fromisoformat(day) if day else date.today()
    shadow = _read_log(shadow_list.shadow_book.log_name, d)
    real = [t for t in _read_log(REAL_TRADES_NAME, d) if t.get("strategy") == STRATEGY]
    symbols, as_of = shadow_list.shadow_symbols()
    return {"mode": settings.get("shadow_list_mode"), "list_as_of": as_of, "symbols": symbols, "day": d.isoformat(),
            "shadow": {"count": len(shadow), "wins": sum(1 for t in shadow if t["pnl_modeled"] > 0),
                       "pnl_modeled": round(sum(t["pnl_modeled"] for t in shadow), 2), "trades": shadow,
                       **shadow_list.shadow_book.snapshot()},
            "real_same_day": {"count": len(real), "pnl": round(sum(t.get("pnl") or 0 for t in real), 2)},
            "note": "paper, calls only (no hedge, no scale-in legs); real list = GET /super-bollinger/trades"}


def _read_log(name: str, d: date) -> list[dict]:
    path = dated_path(name, d)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


@router.get("/super-bollinger/trades")
async def get_trades(day: Optional[str] = None):
    """Closed trades for `day` (YYYY-MM-DD, default today): real ones (from
    the shared real-trade log, tagged SuperBollinger) and paper ones (with
    pnl_modeled = the backtests' slippage model)."""
    d = date.fromisoformat(day) if day else date.today()
    real = [t for t in _read_log(REAL_TRADES_NAME, d) if t.get("strategy") == STRATEGY]
    paper = _read_log(paper_book.log_name, d)
    hedges_real = [t for t in _read_log(REAL_TRADES_NAME, d) if t.get("strategy") == HEDGE_STRATEGY]
    hedges_paper = _read_log(hedge_paper_book.log_name, d)
    return {
        "hedges": {"real_count": len(hedges_real), "real_pnl": round(sum(t.get("pnl") or 0 for t in hedges_real), 2),
                   "paper_count": len(hedges_paper),
                   "paper_pnl_modeled": round(sum(t["pnl_modeled"] for t in hedges_paper), 2),
                   "real": hedges_real, "paper": hedges_paper},
        "strategy": STRATEGY, "day": d.isoformat(),
        "real": {"count": len(real), "wins": sum(1 for t in real if (t.get("pnl") or 0) > 0),
                 "total_pnl": round(sum(t.get("pnl") or 0 for t in real), 2), "trades": real},
        "paper": {"count": len(paper), "wins": sum(1 for t in paper if t["pnl_modeled"] > 0),
                  "total_pnl_raw": round(sum(t["pnl_raw"] for t in paper), 2),
                  "total_pnl_modeled": round(sum(t["pnl_modeled"] for t in paper), 2), "trades": paper},
    }


@router.get("/super-bollinger/symbols")
async def get_symbols():
    _all, source = await super_watchlist.symbols()
    symbols = await eligible_symbols()
    idx = index_symbols()
    return {"count": len(symbols), "source": source, "symbols": symbols,
            "permanent_index_symbols": idx,
            "index_mode": ("PAPER" if paper_mode_control.is_paper_mode_enabled(INDEX_STRATEGY) else "REAL") if idx else "off"}


@router.get("/super-bollinger/watchlist")
async def get_watchlist():
    symbols, source = await super_watchlist.symbols()
    return {"file": str(super_watchlist.WATCHLIST_FILE), "source": source, "count": len(symbols), "symbols": symbols,
            "permanent_index_symbols": index_symbols(),
            "note": "index symbols are not stored in this file - the weekly refresh never touches them"}


@router.get("/super-bollinger/watchlist/shadow")
async def get_shadow_watchlist():
    """The shadow list (40-session fit with the 1-hour filter) next to the
    live list, and how both did week by week. Recorded by the Friday
    refresh; never traded."""
    import stock_selection
    return await asyncio.get_running_loop().run_in_executor(None, stock_selection.shadow_status)


@router.post("/super-bollinger/watchlist/replace")
async def replace_watchlist(payload: dict[str, Any]):
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols) or not symbols:
        raise HTTPException(status_code=422, detail="body must be {\"symbols\": [\"SYM1\", ...]} (non-empty)")
    new = await asyncio.get_running_loop().run_in_executor(None, super_watchlist.replace, symbols)
    return {"count": len(new), "symbols": new}


@router.post("/super-bollinger/square-off-now")
async def manual_square_off():
    await square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()


SUPERVISOR_KEYS = ("hedge_mode", "hedge_trigger_rs", "hedge_atr_mult", "hedge_trail_arm_rs", "hedge_trail_giveback",
                   "hedge_stop_rs", "hedge_cutoff_time", "disaster_brake_rs", "shadow_stop_reenter_rs")


@router.get("/super-bollinger/supervisor")
async def get_supervisor(day: Optional[str] = None):
    """Supervisor status (change any setting via POST /super-bollinger/config)."""
    d = date.fromisoformat(day) if day else date.today()
    events = _read_log(SUPERVISOR_LOG, d)
    counts: dict = {}
    for e in events:
        counts[e["event"]] = counts.get(e["event"], 0) + 1
    hedges_real = [x for x in _read_log(REAL_TRADES_NAME, d) if x.get("strategy") == HEDGE_STRATEGY]
    hedges_paper = _read_log(hedge_paper_book.log_name, d)
    return {
        "settings": {k: settings.get(k) for k in SUPERVISOR_KEYS},
        "halted_today": halted["day"] == date.today(), "halt_reason": halted["reason"],
        "open_hedges_real": (await hedge_store.snapshot())["live_positions"],
        "open_hedges_paper": hedge_paper_book.snapshot()["open_positions"],
        "hedges_closed_today": {"real": hedges_real, "real_pnl": round(sum(x.get("pnl") or 0 for x in hedges_real), 2),
                                "paper": hedges_paper,
                                "paper_pnl_modeled": round(sum(x["pnl_modeled"] for x in hedges_paper), 2)},
        "event_counts": counts, "last_events": events[-30:],
    }


@router.get("/super-bollinger/restart-report")
async def get_restart_report():
    """What the last startup restored (SuperBollinger/live_state.py) and whether anything needs a look."""
    return live_state.last_report() or {"note": "no restart report in this process yet"}


@router.get("/super-bollinger/live-state")
async def get_live_state():
    """The state file's content as it would be written right now."""
    return live_state.collect()


@router.get("/super-bollinger/scale")
async def get_scale(day: Optional[str] = None):
    """Scale-in PAPER variant (SuperBollinger/scale.py). variant_vs_real_hedge =
    the variant's PE legs (hedge copy + added lot) minus the real/paper
    hedge's PnL for the same symbols, i.e. what the PE-add rule changed."""
    d = date.fromisoformat(day) if day else date.today()
    closed = _read_log(scale.SCALE_TRADES_LOG, d)
    events = _read_log(scale.SCALE_EVENTS_LOG, d)
    by_leg: dict = {}
    for t in closed:
        leg = by_leg.setdefault(t.get("leg"), {"count": 0, "pnl_raw": 0.0, "pnl_modeled": 0.0})
        leg["count"] += 1
        leg["pnl_raw"] = round(leg["pnl_raw"] + t["pnl_raw"], 2)
        leg["pnl_modeled"] = round(leg["pnl_modeled"] + t["pnl_modeled"], 2)
    pe_syms = {t["underlying_symbol"] for t in closed if t.get("leg") in ("PE_HEDGE_COPY", "PE_ADD")}
    hedges = [x for x in _read_log(REAL_TRADES_NAME, d) if x.get("strategy") == HEDGE_STRATEGY] + \
        _read_log(hedge_paper_book.log_name, d)
    hedge_pnl = sum((x.get("pnl") if x.get("pnl") is not None else x.get("pnl_raw")) or 0
                    for x in hedges if x.get("underlying_symbol") in pe_syms)
    variant_pe = sum(t["pnl_raw"] for t in closed if t.get("leg") in ("PE_HEDGE_COPY", "PE_ADD"))
    return {**scale.snapshot(), "day": d.isoformat(), "closed_by_leg": by_leg, "closed": closed,
            "pe_variant_vs_hedge_raw": {"variant_pe_legs": round(variant_pe, 2), "hedge_same_symbols": round(hedge_pnl, 2),
                                        "difference": round(variant_pe - hedge_pnl, 2),
                                        "note": "only symbols where the variant's PE legs have closed"},
            "events": events[-50:]}


@router.post("/super-bollinger/supervisor/square-off-hedges")
async def square_off_hedges():
    from Bollinger import trading_engine as engine
    for sym, pos in list(hedge_store.live_positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            ltp = pos.entry_price
        if await hedge_store.try_start_exit(sym):
            await engine._exit_position(sym, pos, ltp, "MANUAL_SQUARE_OFF", hedge_store)
    return await hedge_store.snapshot()
