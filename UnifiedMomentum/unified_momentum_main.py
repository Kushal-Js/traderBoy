"""
Unified Momentum (1 Oct 2026) - `lifespan` and `router`, composed into the shared app by main.py right after Super
Bollinger's (it reuses Bollinger's signal cache, Swing's signals and WS candle feed, and Options' authenticated Dhan
connection). The strategy: engine A = pullback CALLS (UnifiedMomentum/trading_engine.py) with the supervisor's PUT
hedges (supervisor.py), engine B = momentum PUTs (engine_b.py), the market chop gate (market_gate.py), its own weekly
HYBRID watchlist (watchlist.py). Runtime configuration: settings.py.

Endpoints:
  GET  /unified-momentum/config            every setting, paper mode, capacity, and where each value comes from
  POST /unified-momentum/config            change any of them at runtime (no restart), e.g. {"paper_mode_enabled": true}
                                          (the real-money feature flag - both engines and the hedges), {"engine_b_enabled":
                                          false}, {"market_chop_gate_er": 0}, {"max_concurrent_trades": 2} (engine A),
                                          {"b_max_concurrent_trades": 2} (engine B)
  GET  /unified-momentum/positions         open real + paper positions of both engines, today's orders
  GET  /unified-momentum/trades?day=       closed trades of a day (calls, puts, hedges; real + paper) with PnL totals
  GET  /unified-momentum/symbols           the stocks it trades now
  GET  /unified-momentum/watchlist         its own HYBRID-picked watchlist (data/unified_momentum_watchlist)
  POST /unified-momentum/watchlist/replace replace it by hand, e.g. {"symbols": ["LAURUSLABS", "ZYDUSLIFE"]}
  GET  /unified-momentum/market-gate       the market chop gate: NIFTY's 2 h efficiency ratio vs the threshold
  GET  /unified-momentum/engine-b          engine B: settings, open puts, fresh-formation state
  POST /unified-momentum/square-off-now    manual kill switch - exits every open REAL position (calls, puts, hedges)
  GET  /unified-momentum/supervisor        supervisor status: hedge mode, open hedges, today's hedge trades, brake
  POST /unified-momentum/supervisor/square-off-hedges   exits every open REAL hedge (calls and puts untouched)
  GET  /unified-momentum/stop-ratchet      ratcheted broker stops of engine A's calls
  GET  /unified-momentum/restart-report    what the last startup restored from the state file / broker
  GET  /unified-momentum/live-state        the live state that is persisted for restarts
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

from . import best_price_memory, engine_b, live_state, market_gate, settings
from . import supervisor
from . import watchlist as um_watchlist
from .state import (HEDGE_STRATEGY, PUT_STRATEGY, STRATEGY, SUPERVISOR_LOG, halted, hedge_paper_book, hedge_store,
                    paper_book, position_store, put_paper_book, put_store)
from .trading_engine import (eligible_symbols, install_tick_entries, monitor_loop, on_price_tick, open_count,
                             reconcile_broker_positions, square_off_all)

logger = logging.getLogger("unified_momentum_main")
router = APIRouter()
_monitor_task: Optional[asyncio.Task] = None
_supervisor_task: Optional[asyncio.Task] = None
_engine_b_task: Optional[asyncio.Task] = None
_probe_task: Optional[asyncio.Task] = None


async def _legacy_reconcile() -> None:
    """The pre-state-file startup rebuild (broker positions + best-price
    memory) - only used if live_state.restore() itself fails."""
    for name, fetch, store in ((STRATEGY, reconcile_broker_positions, position_store),
                               (HEDGE_STRATEGY, supervisor.reconcile_hedges, hedge_store),
                               (PUT_STRATEGY, engine_b.reconcile_broker_positions, put_store)):
        try:
            found = await fetch()
            if found:
                best_price_memory.restore(name, found)
                await store.reconcile_from_broker(found)
                logger.info("[%s] reconciled %d open %s position(s) at startup: %s", STRATEGY, len(found), name,
                            [p.underlying_symbol for p in found])
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not reconcile %s broker positions at startup - continuing without them",
                             STRATEGY, name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reconciliation and the monitor loop always start (even with
    strategy_enabled=false) so an already-open real position is always
    managed to its exit."""
    global _monitor_task, _supervisor_task, _engine_b_task, _probe_task
    try:
        n = engine_b.load_state()        # engine B's fresh-formation state (kept across days)
        logger.info("[%s] engine B: fresh-formation state restored for %d stock(s)", STRATEGY, n)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not restore engine B's fresh-formation state", STRATEGY)
    try:
        # State file + broker + open orders (UnifiedMomentum/live_state.py): restores every real call, hedge and
        # put with its full management state, today's closed trades, the brake, used signals, engine B's
        # fresh-formation state and the supervisor's per-trade memory; resolves orders that were in flight.
        report = await live_state.restore()
        logger.info("[%s] restart reconcile done: %d call(s) %s, %d hedge(s) %s, %d put(s) %s", STRATEGY,
                    len(report["positions"]), [p["trading_symbol"] for p in report["positions"]],
                    len(report["hedges"]), [p["trading_symbol"] for p in report["hedges"]],
                    len(report.get("puts", [])), [p["trading_symbol"] for p in report.get("puts", [])])
    except Exception:  # noqa: BLE001
        logger.exception("[%s] state-file reconcile failed - falling back to the broker-only rebuild", STRATEGY)
        await _legacy_reconcile()
    for pos in paper_book.load() + hedge_paper_book.load() + put_paper_book.load():
        try:
            dhan_wrapper.subscribe_option_price(pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] could not re-subscribe %s for a restored paper position", STRATEGY,
                             pos.trading_symbol)

    loop = asyncio.get_running_loop()

    def _on_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    def _on_tick_supervisor(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(supervisor.on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_tick)       # engine A, then engine B (trading_engine.on_price_tick)
    dhan_wrapper.add_price_tick_subscriber(_on_tick_supervisor)
    _supervisor_task = asyncio.create_task(supervisor.supervisor_loop())
    install_tick_entries(loop)  # engine A's tick-driven entries off the underlying WS feed
    _monitor_task = asyncio.create_task(monitor_loop())
    _engine_b_task = asyncio.create_task(engine_b.loop())   # momentum PUTs: own loop since 2 Oct 2026
    # How long a blocking call (an order too) waits for an executor worker - GET /feed-stats executor_lag_*.
    _probe_task = asyncio.create_task(dhan_wrapper.executor_lag_probe_forever())

    real = not paper_mode_control.is_paper_mode_enabled(STRATEGY)
    logger.info("[%s] startup complete: mode=%s engine A slots=%s engine B=%s (slots %s) settings=%s", STRATEGY,
                "REAL" if real else "PAPER", capacity_control.get_max_concurrent_trades(STRATEGY),
                "on" if settings.get("engine_b_enabled") else "off", settings.get("b_max_concurrent_trades"),
                {k: v["value"] for k, v in settings.snapshot().items()})
    if real:
        logger.warning("[%s] REAL TRADING ON - real orders will be placed for the stocks on "
                       "data/unified_momentum_watchlist (calls, momentum puts, hedges).", STRATEGY)
    yield
    if _monitor_task:
        _monitor_task.cancel()
    if _engine_b_task:
        _engine_b_task.cancel()
    if _probe_task:
        _probe_task.cancel()
    if _supervisor_task:
        _supervisor_task.cancel()


def _config_view() -> dict:
    return {
        "strategy": STRATEGY,
        "paper_mode_enabled": {"value": paper_mode_control.is_paper_mode_enabled(STRATEGY),
                               "source": paper_mode_control.paper_mode_source(STRATEGY)},
        "max_concurrent_trades": {"value": capacity_control.get_max_concurrent_trades(STRATEGY),
                                  "source": capacity_control.capacity_source(STRATEGY),
                                  "note": "engine A (pullback calls); engine B uses b_max_concurrent_trades"},
        **settings.snapshot(),
        "always_excluded": "index and MCX symbols (NSE stocks only)",
    }


@router.get("/unified-momentum/config")
async def get_config():
    return _config_view()


@router.post("/unified-momentum/config")
async def update_config(payload: dict[str, Any]):
    """Partial update - send only what changes. Validated as a whole before
    anything is applied. Takes effect on the next monitor tick (<= 5s), is
    persisted, and is written to .env too. Changes never touch an already-
    open position's entry, only how it is managed from now on (e.g. a new
    max_loss_rs applies to open trades immediately; the broker-side
    backstop order keeps the level it was placed at)."""
    changes = dict(payload)
    paper = changes.pop("paper_mode_enabled", None)
    capacity = changes.pop("max_concurrent_trades", None)
    parsed, errors = settings.validate(changes)
    if paper is not None and not isinstance(paper, bool):
        errors.append("paper_mode_enabled: must be true or false")
    if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0):
        errors.append("max_concurrent_trades: must be an integer >= 0")
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    warnings = []
    if "quantity_lots" in parsed and parsed["quantity_lots"] != settings.get("quantity_lots") and not (
            "max_loss_rs" in parsed and "b_max_loss_rs" in parsed):
        warnings.append("quantity_lots changed - the rupee caps do not scale with lot size; consider scaling "
                        "max_loss_rs, breakeven_after_rs, b_max_loss_rs and b_profit_protection_rs too")
    env_synced = await settings.update(parsed) if parsed else True
    if paper is not None:
        await paper_mode_control.set_paper_mode(STRATEGY, paper)
    if capacity is not None:
        await capacity_control.set_max_concurrent_trades(STRATEGY, capacity)
    return {"applied": {**parsed, **({"paper_mode_enabled": paper} if paper is not None else {}),
                        **({"max_concurrent_trades": capacity} if capacity is not None else {})},
            "env_synced": env_synced, "warnings": warnings, "config": _config_view()}


@router.get("/unified-momentum/positions")
async def get_positions():
    """Engine A at the top level (open_count = its REAL slots in use), engine B under "engine_b"."""
    return {"open_count": open_count(), "paper_open_count": len(paper_book.positions),
            **await position_store.snapshot(), **paper_book.snapshot(),
            "engine_b": {"open_count": engine_b.open_count(False), "paper_open_count": engine_b.open_count(True),
                         **await put_store.snapshot(), **put_paper_book.snapshot()},
            "market_gate": market_gate.snapshot(), "halted_today": halted["day"] == date.today()}


def _read_log(name: str, d: date) -> list[dict]:
    path = dated_path(name, d)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _book_summary(real: list[dict], paper: list[dict]) -> dict:
    return {"real": {"count": len(real), "wins": sum(1 for t in real if (t.get("pnl") or 0) > 0),
                     "total_pnl": round(sum(t.get("pnl") or 0 for t in real), 2), "trades": real},
            "paper": {"count": len(paper), "wins": sum(1 for t in paper if t["pnl_modeled"] > 0),
                      "total_pnl_raw": round(sum(t["pnl_raw"] for t in paper), 2),
                      "total_pnl_modeled": round(sum(t["pnl_modeled"] for t in paper), 2), "trades": paper}}


@router.get("/unified-momentum/trades")
async def get_trades(day: Optional[str] = None):
    """Closed trades for `day` (YYYY-MM-DD, default today): real ones (the shared real-trade log, by tag) and paper
    ones (pnl_modeled = the backtests' slippage model), for the calls, the momentum puts and the hedges."""
    d = date.fromisoformat(day) if day else date.today()
    real_all = _read_log(REAL_TRADES_NAME, d)
    by = {name: [t for t in real_all if t.get("strategy") == name] for name in (STRATEGY, PUT_STRATEGY, HEDGE_STRATEGY)}
    books = {"calls": _book_summary(by[STRATEGY], _read_log(paper_book.log_name, d)),
             "puts": _book_summary(by[PUT_STRATEGY], _read_log(put_paper_book.log_name, d)),
             "hedges": _book_summary(by[HEDGE_STRATEGY], _read_log(hedge_paper_book.log_name, d))}
    return {"strategy": STRATEGY, "day": d.isoformat(),
            "total": {"real_pnl": round(sum(b["real"]["total_pnl"] for b in books.values()), 2),
                      "paper_pnl_modeled": round(sum(b["paper"]["total_pnl_modeled"] for b in books.values()), 2)},
            **books}


@router.get("/unified-momentum/symbols")
async def get_symbols():
    _all, source = await um_watchlist.symbols()
    symbols = await eligible_symbols()
    return {"count": len(symbols), "source": source, "symbols": symbols}


@router.get("/unified-momentum/watchlist")
async def get_watchlist():
    symbols, source = await um_watchlist.symbols()
    return {"file": str(um_watchlist.WATCHLIST_FILE), "source": source, "count": len(symbols), "symbols": symbols,
            "refreshed": "every Friday 00:00 IST (weekly_watchlist_refresh.py, HYBRID selection)"}


@router.post("/unified-momentum/watchlist/replace")
async def replace_watchlist(payload: dict[str, Any]):
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols) or not symbols:
        raise HTTPException(status_code=422, detail="body must be {\"symbols\": [\"SYM1\", ...]} (non-empty)")
    new = await asyncio.get_running_loop().run_in_executor(None, um_watchlist.replace, symbols)
    return {"count": len(new), "symbols": new}


@router.get("/unified-momentum/market-gate")
async def get_market_gate():
    """Closed = no new entries in either engine (open positions are managed as normal)."""
    await market_gate.refresh()
    return market_gate.snapshot()


@router.get("/unified-momentum/engine-b")
async def get_engine_b():
    keys = ("engine_b_enabled", "b_max_concurrent_trades", "b_entry_cutoff_time", "b_max_loss_rs", "b_target_pct",
            "b_profit_protection_rs", "b_profit_protection_giveback", "b_hard_stop_pct", "b_supertrend_exit",
            "b_volume_floor_ratio")
    return {"settings": {k: settings.get(k) for k in keys},
            "mode": "PAPER" if paper_mode_control.is_paper_mode_enabled(STRATEGY) else "REAL",
            "open_real": (await put_store.snapshot())["live_positions"],
            "open_paper": put_paper_book.snapshot()["open_positions"],
            "fresh_formation_state": engine_b.state_snapshot()["consumed"],
            "market_gate": market_gate.snapshot()}


@router.post("/unified-momentum/square-off-now")
async def manual_square_off():
    await square_off_all("MANUAL_SQUARE_OFF")
    await engine_b.square_off_all("MANUAL_SQUARE_OFF")
    await square_off_hedges()
    return {"calls": await position_store.snapshot(), "puts": await put_store.snapshot(),
            "hedges": await hedge_store.snapshot()}


SUPERVISOR_KEYS = ("hedge_mode", "hedge_trigger_rs", "hedge_atr_mult", "hedge_trail_arm_rs", "hedge_trail_giveback",
                   "hedge_stop_rs", "hedge_cutoff_time", "disaster_brake_rs", "shadow_stop_reenter_rs")


@router.get("/unified-momentum/supervisor")
async def get_supervisor(day: Optional[str] = None):
    """Supervisor status (change any setting via POST /unified-momentum/config)."""
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


@router.post("/unified-momentum/supervisor/square-off-hedges")
async def square_off_hedges():
    from Bollinger import trading_engine as engine
    for sym, pos in list(hedge_store.live_positions.items()):
        if pos.pending_exit_order_id or engine._exit_on_cooldown(pos):
            continue
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            ltp = pos.entry_price
        if await hedge_store.try_start_exit(sym):
            await engine._exit_position(sym, pos, ltp, "MANUAL_SQUARE_OFF", hedge_store)
    return await hedge_store.snapshot()


@router.get("/unified-momentum/stop-ratchet")
async def get_stop_ratchet():
    """Ratcheted broker stops (UnifiedMomentum/stop_ratchet.py): the mode, the limits, and the level each
    real call's broker stop-loss order has been moved to today."""
    from . import stop_ratchet
    return stop_ratchet.snapshot()


@router.get("/unified-momentum/restart-report")
async def get_restart_report():
    """What the last startup restored (UnifiedMomentum/live_state.py) and whether anything needs a look."""
    return live_state.last_report() or {"note": "no restart report in this process yet"}


@router.get("/unified-momentum/live-state")
async def get_live_state():
    """The state file's content as it would be written right now."""
    return live_state.collect()
