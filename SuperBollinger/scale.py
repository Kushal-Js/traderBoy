"""
Super Bollinger RECOVERY variant - PAPER ONLY (settings.scale_mode: off |
shadow | paper). Real CE trades and real hedges are never touched; the added
lots live in their own paper books and log (history/<date>_super_bollinger_
scale_paper_trades.log, event log super_bollinger_scale), so the variant's
results are never mixed with the deployed strategy's.

Rules = the user's two scenarios as backtested on the 3 Aug - 29 Sep
walk-forward trades (research_super_bollinger_recovery_scenarios.py, 30 Sep
2026; small samples, chosen in-sample):

  S1  CALL RE-ADD ("the stock recovers to its trigger -> one more call").
      After the hedge trigger has fired for a CE (the dip), when the STOCK is
      back at or above the price the entry triggered at, before the entry
      cutoff, and Supertrend on the last closed 5-min bar is bullish
      (scale_ce_readd_confirm): 1 more lot of the same call. It has its own
      max_loss_rs stop and otherwise exits together with the original CE (at
      the original's exit price). No trailing stop and no shared max loss -
      both tested worse (-20.0k with them vs +44.9k without; 24 re-adds).
      Nothing is re-bought once the original CE has been closed.

  S2  RIDE THE FALL WITH TWO PUT LOTS. Once a supervisor hedge is open and
      Supertrend on the last closed 5-min bar is bearish: the variant takes
      over the PUT side on paper - a copy of the hedge lot (lot A: the hedge's
      own entry and best price) plus 1 added lot B at the current price.
      Exits, checked in this order:
        A at the hedge stop (hedge_stop_rs), or - once one lot has been
          sold - at its own purchase price                  -> A and B out
        B at its own scale_pe_add_stop_rs stop              -> B out
        A + B profit >= scale_pe_target_rs                  -> B sold, A rides
        A's trail (hedge_trail_arm_rs / hedge_trail_giveback) -> A and B out
        square_off_time                                     -> A and B out
      (+22.0k vs +15.7k for the hedge alone over the two months, all of the
      gain in September; 33 second lots, target reached 9 times.)

One re-add per CE trade and one second PUT lot per hedge (keyed by contract +
entry price + day, persisted in data/super_bollinger_scale_state.json, so a
restart never adds twice). Shadow mode only logs SCALE_WOULD_ADD_* decisions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from Bollinger import config as bcfg, signals
from Bollinger import trading_engine as engine
from Bollinger.paper_book import PaperBook
from Bollinger.position_store import Position
from Options.dhan_client import dhan_wrapper
from SuperTrader.strategy import supertrend
from trade_history import REAL_TRADES_NAME, dated_path

from . import settings
from .state import PAPER_TRADES_LOG, STRATEGY, paper_book, position_store

logger = logging.getLogger("super_bollinger_scale")

SCALE_STRATEGY = "SuperBollingerScale"
SCALE_TRADES_LOG = "super_bollinger_scale_paper_trades"
SCALE_EVENTS_LOG = "super_bollinger_scale"
STATE_FILE = Path("data/super_bollinger_scale_state.json")
NO_TRAILING = 1e12
ST_RECHECK_SECONDS = 10
_ENTRY_TOL = 0.01

ce_add_book = PaperBook("data/super_bollinger_scale_ce_add_positions.json", SCALE_TRADES_LOG,
                        {"strategy": SCALE_STRATEGY, "leg": "CE_ADD"})
pe_copy_book = PaperBook("data/super_bollinger_scale_pe_copy_positions.json", SCALE_TRADES_LOG,
                         {"strategy": SCALE_STRATEGY, "leg": "PE_HEDGE_COPY"})
pe_add_book = PaperBook("data/super_bollinger_scale_pe_add_positions.json", SCALE_TRADES_LOG,
                        {"strategy": SCALE_STRATEGY, "leg": "PE_ADD"})
BOOKS = (ce_add_book, pe_copy_book, pe_add_book)

_state: dict | None = None
_st_cache: dict[str, tuple[float, Optional[int], Optional[float]]] = {}
_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Pure rules (unit-testable)
# --------------------------------------------------------------------------- #
def ce_readd_due(hedge_trigger_seen: bool, spot: Optional[float], entry_spot: Optional[float]) -> bool:
    """S1: the dip happened (the hedge trigger fired) and the stock is back at/above its trigger price."""
    return bool(hedge_trigger_seen) and spot is not None and entry_spot is not None and spot >= entry_spot


def pe_wave_decision(ltp: float, a_mult: float, a_entry: float, a_best: float, b_entry: Optional[float], b_mult: float,
                     booked: bool, hedge_stop_rs: float, add_stop_rs: float, target_rs: float, trail_arm_rs: float,
                     trail_giveback: float) -> tuple[Optional[str], Optional[str]]:
    """S2 -> (what to close: "ALL" | "B" | None, reason). b_entry None = lot B
    is already closed. booked = one lot has been sold at the target. a_best
    must include ltp."""
    a_pnl = (ltp - a_entry) * a_mult
    if booked and ltp <= a_entry:
        return "ALL", "KEPT_LOT_FLOOR"
    if a_pnl <= -hedge_stop_rs:
        return "ALL", "HEDGE_STOP"
    if b_entry is not None:
        b_pnl = (ltp - b_entry) * b_mult
        if b_pnl <= -add_stop_rs:
            return "B", "ADD_OWN_STOP"
        if not booked and a_pnl + b_pnl >= target_rs:
            return "B", "TARGET_BOOKED"
    peak = (a_best - a_entry) * a_mult
    if peak >= trail_arm_rs and a_pnl <= peak * (1 - trail_giveback):
        return "ALL", "HEDGE_TRAIL"
    return None, None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def _today() -> str:
    return engine._now_ist().date().isoformat()


def _key(symbol: str, pos: Position) -> str:
    return f"{symbol}|{pos.trading_symbol}|{pos.entry_price:.2f}"


def _fresh_state() -> dict:
    return {"day": _today(), "ce_done": [], "pe_done": [], "ce_parent": {}, "pairs": {}}


def _st() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else _fresh_state()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s - starting fresh", STATE_FILE)
            _state = _fresh_state()
    if _state.get("day") != _today():
        _state = _fresh_state()
        _save()
    return _state


def _save() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, indent=2))
        os.replace(tmp, STATE_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist %s", STATE_FILE)


def load() -> list[Position]:
    """Startup: restore open variant paper legs (and subscribe their contracts)."""
    out = []
    for book in BOOKS:
        out += book.load()
    for pos in out:
        try:
            dhan_wrapper.subscribe_option_price(pos.trading_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("[scale] could not re-subscribe %s", pos.trading_symbol)
    _st()
    return out


async def _log(event: str, symbol: str, **detail) -> None:
    await engine._record_bollinger_event(event, symbol, {"component": "scale", **detail}, SCALE_EVENTS_LOG)


def _paper_leg(symbol: str, like: Position, option_type: str, qty: int, entry: float, best: Optional[float] = None,
               opened_at=None) -> Position:
    pos = Position(
        underlying_symbol=symbol, trading_symbol=like.trading_symbol, resolved_option_type=option_type,
        instrument_side="LONG", exchange_segment="NSE_FNO", product_type=like.product_type, quantity=qty,
        lot_size=like.lot_size, entry_price=entry, best_price=max(entry, best or entry), stop_pct=0.0,
        hard_stop_loss=0.05, trailing_stop_dist=NO_TRAILING, trailing_step=NO_TRAILING, pnl_multiplier=qty,
        order_id="PAPER")
    if opened_at is not None:
        pos.opened_at = opened_at
    return pos


def _one_lot(pos: Position) -> int:
    if pos.lot_size:
        return int(pos.lot_size)
    return max(1, int(pos.quantity // max(1, settings.get("quantity_lots"))))


def _after(hhmm: str) -> bool:
    return engine._now_ist().strftime("%H:%M") >= hhmm


# --------------------------------------------------------------------------- #
# Supertrend on the stock's last closed 5-min bar
# --------------------------------------------------------------------------- #
async def supertrend_now(symbol: str) -> tuple[Optional[int], Optional[float]]:
    """(direction +1/-1, that bar's close epoch). Same continuous REST base +
    completed WS bars the signal engine uses (cached per bar - at most one
    REST call per symbol per 5-min bar); rechecked every ST_RECHECK_SECONDS."""
    now = time.monotonic()
    cached = _st_cache.get(symbol)
    if cached and now - cached[0] < ST_RECHECK_SECONDS:
        return cached[1], cached[2]
    period, mult = settings.get("scale_supertrend_period"), settings.get("scale_supertrend_mult")

    def work():
        sid, seg, inst = signals._underlying_reference(symbol)
        data = signals._get_intraday_series(symbol, sid, seg, inst)
        closes = data.get("close") or []
        if len(closes) < period * 3:
            return None, None
        d = supertrend(data["high"], data["low"], closes, period, mult)
        return d[-1], float(data["timestamp"][-1]) + bcfg.SIGNAL_INTERVAL_MINUTES * 60

    try:
        direction, bar_close = await asyncio.get_running_loop().run_in_executor(None, work)
    except Exception:  # noqa: BLE001
        logger.exception("[scale] %s: Supertrend check failed", symbol)
        direction, bar_close = None, None
    _st_cache[symbol] = (now, direction, bar_close)
    return direction, bar_close


# --------------------------------------------------------------------------- #
# CE side
# --------------------------------------------------------------------------- #
def _live_ce(symbol: str) -> Optional[Position]:
    return position_store.live_positions.get(symbol) or paper_book.positions.get(symbol)


def _matches(pos: Optional[Position], ts: str, entry: float) -> bool:
    return pos is not None and pos.trading_symbol == ts and abs(pos.entry_price - entry) <= _ENTRY_TOL * entry


def _logged_closed_ces(symbol: str) -> list[dict]:
    """Today's closed Super Bollinger CE trades for `symbol` from the durable
    logs (real trade log + paper trade log) - survives a restart, unlike
    position_store.closed_positions_today. Each: ts, entry, exit, mult, reason."""
    out = []
    for name in (REAL_TRADES_NAME, PAPER_TRADES_LOG):
        path = dated_path(name)
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if t.get("strategy") != STRATEGY or t.get("underlying_symbol") != symbol or t.get("exit_price") is None:
                continue
            mult = t.get("pnl_multiplier") or t.get("quantity")
            if not mult:
                continue
            out.append({"ts": t.get("option_trading_symbol") or t.get("trading_symbol"),
                        "entry": float(t["entry_price"]), "exit": float(t["exit_price"]), "mult": float(mult),
                        "reason": t.get("exit_reason") or "CLOSED", "closed_at": t.get("closed_at") or ""})
    return sorted(out, key=lambda x: x["closed_at"])


def _closed_ce(symbol: str, ts: str, entry: float) -> Optional[tuple[float, str]]:
    """(exit price, reason) of a closed real or paper Super Bollinger CE."""
    for p in reversed(position_store.closed_positions_today):
        if p.underlying_symbol == symbol and _matches(p, ts, entry) and p.exit_price is not None:
            return p.exit_price, p.exit_reason or "CLOSED"
    for t in reversed(_logged_closed_ces(symbol)):
        if t["ts"] == ts and abs(t["entry"] - entry) <= _ENTRY_TOL * entry:
            return t["exit"], t["reason"]
    return None


async def on_ce_price(symbol: str, pos: Position, ltp: float, ce_is_real: bool, track: Optional[dict] = None,
                      spot: Optional[float] = None) -> None:
    """Called by the supervisor for every open CE price (tick + 2s loop).
    `track` is the supervisor's per-trade memory (hedged flag, entry spot)."""
    mode = settings.get("scale_mode")
    if mode == "off" or pos.pending_exit_order_id or not track:
        return
    st = _st()
    key = _key(symbol, pos)
    if (key in st["ce_done"] or _after(settings.get("entry_cutoff_time"))
            or not ce_readd_due(track.get("hedged"), spot, track.get("entry_spot"))):
        return
    if settings.get("scale_ce_readd_confirm"):
        direction, _bar = await supertrend_now(symbol)
        if direction != 1:
            return
    async with _lock:
        if key in st["ce_done"]:
            return
        st["ce_done"].append(key)
        _save()
    qty = _one_lot(pos)
    detail = {"ce": pos.trading_symbol, "ce_entry": pos.entry_price, "ce_ltp": ltp, "spot": spot,
              "entry_spot": track.get("entry_spot"), "ce_pnl": round((ltp - pos.entry_price) * pos.pnl_multiplier),
              "add_qty": qty, "real_ce": ce_is_real, "mode": mode}
    if mode == "shadow":
        await _log("SCALE_WOULD_ADD_CE", symbol, **detail)
        return
    if await ce_add_book.open(_paper_leg(symbol, pos, "CE", qty, ltp)):
        st["ce_parent"][symbol] = {"ts": pos.trading_symbol, "entry": pos.entry_price, "real": ce_is_real}
        _save()
        await _log("SCALE_CE_ADDED", symbol, add_entry=ltp, **detail)


async def _manage_ce_add(symbol: str, ltp: float) -> None:
    pos = ce_add_book.positions.get(symbol)
    if pos is None:
        return
    await ce_add_book.update(symbol, ltp)
    parent = _st()["ce_parent"].get(symbol)
    reason, px = None, ltp
    if parent and not _matches(_live_ce(symbol), parent["ts"], parent["entry"]):
        closed = _closed_ce(symbol, parent["ts"], parent["entry"])
        reason = f"WITH_ORIGINAL_{closed[1]}" if closed else "WITH_ORIGINAL_CLOSED"
        px = closed[0] if closed else ltp
    elif parent is None:
        reason = "ORIGINAL_UNKNOWN"
    elif (ltp - pos.entry_price) * pos.pnl_multiplier <= -settings.get("max_loss_rs"):
        reason = "ADD_MAX_LOSS"
    elif _after(settings.get("square_off_time")):
        reason = "DAILY_SQUARE_OFF"
    if reason:
        record = await ce_add_book.close(symbol, px, reason)
        if record:
            await _log("SCALE_CE_ADD_CLOSED", symbol, **record)


# --------------------------------------------------------------------------- #
# PE side
# --------------------------------------------------------------------------- #
async def on_hedge_price(symbol: str, hedge: Position, ltp: float, hedge_is_real: bool) -> None:
    """Called by the supervisor for every open hedge price (tick + 2s loop)."""
    mode = settings.get("scale_mode")
    if mode == "off" or hedge.pending_exit_order_id:
        return
    st = _st()
    key = _key(symbol, hedge)
    if key in st["pe_done"]:
        return
    direction, bar_close = await supertrend_now(symbol)
    if direction != -1:
        return
    async with _lock:
        if key in st["pe_done"]:
            return
        st["pe_done"].append(key)
        _save()
    detail = {"pe": hedge.trading_symbol, "hedge_entry": hedge.entry_price, "hedge_best": hedge.best_price,
              "pe_ltp": ltp, "real_hedge": hedge_is_real, "supertrend_bar_close": bar_close, "mode": mode,
              "target_rs": settings.get("scale_pe_target_rs")}
    if mode == "shadow":
        await _log("SCALE_WOULD_ADD_PE", symbol, **detail)
        return
    qty = _one_lot(hedge)
    a = _paper_leg(symbol, hedge, "PE", hedge.pnl_multiplier, hedge.entry_price, hedge.best_price, hedge.opened_at)
    b = _paper_leg(symbol, hedge, "PE", qty, ltp)
    if not await pe_copy_book.open(a):
        return
    await pe_add_book.open(b)
    st["pairs"][symbol] = {"booked": False}
    _save()
    try:
        await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.subscribe_option_price, hedge.trading_symbol)
    except Exception:  # noqa: BLE001
        pass
    await _log("SCALE_PE_ADDED", symbol, add_entry=ltp, add_qty=qty, **detail)


async def _close_pe(symbol: str, which: str, ltp: float, reason: str) -> None:
    books = (pe_add_book,) if which == "B" else (pe_copy_book, pe_add_book)
    for book in books:
        record = await book.close(symbol, ltp, reason)
        if record:
            await _log("SCALE_PE_CLOSED", symbol, **record)
    if reason == "TARGET_BOOKED":
        _st()["pairs"].setdefault(symbol, {})["booked"] = True
        _save()


async def _manage_pe(symbol: str, ltp: float) -> None:
    a, b = pe_copy_book.positions.get(symbol), pe_add_book.positions.get(symbol)
    if a is None and b is None:
        return
    if a is None:                      # lot A gone -> B follows
        await _close_pe(symbol, "ALL", ltp, "WITH_HEDGE_COPY")
        return
    if _after(settings.get("square_off_time")):
        await _close_pe(symbol, "ALL", ltp, "DAILY_SQUARE_OFF")
        return
    await pe_copy_book.update(symbol, ltp)
    if b is not None:
        await pe_add_book.update(symbol, ltp)
    pair = _st()["pairs"].get(symbol, {})
    which, reason = pe_wave_decision(
        ltp, a.pnl_multiplier, a.entry_price, a.best_price, b.entry_price if b else None,
        b.pnl_multiplier if b else 0, bool(pair.get("booked")), settings.get("hedge_stop_rs"),
        settings.get("scale_pe_add_stop_rs"), settings.get("scale_pe_target_rs"),
        settings.get("hedge_trail_arm_rs"), settings.get("hedge_trail_giveback"))
    if which:
        await _close_pe(symbol, which, ltp, reason)


# --------------------------------------------------------------------------- #
# Hooks called by the supervisor
# --------------------------------------------------------------------------- #
async def on_option_tick(trading_symbol: str, ltp: float) -> None:
    for sym, pos in list(ce_add_book.positions.items()):
        if pos.trading_symbol == trading_symbol:
            await _manage_ce_add(sym, ltp)
    for sym in set(pe_copy_book.positions) | set(pe_add_book.positions):
        pos = pe_copy_book.positions.get(sym) or pe_add_book.positions.get(sym)
        if pos is not None and pos.trading_symbol == trading_symbol:
            await _manage_pe(sym, ltp)


async def tick() -> None:
    """Every supervisor loop (2s): manage open variant legs even when no
    tick arrives (e.g. the real leg they mirror has closed)."""
    if not any(book.positions for book in BOOKS):
        _st()
        return
    for sym, pos in list(ce_add_book.positions.items()):
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            continue
        await _manage_ce_add(sym, ltp)
    for sym in set(pe_copy_book.positions) | set(pe_add_book.positions):
        pos = pe_copy_book.positions.get(sym) or pe_add_book.positions.get(sym)
        if pos is None:
            continue
        try:
            ltp = await engine._get_ltp(pos)
        except Exception:  # noqa: BLE001
            continue
        await _manage_pe(sym, ltp)


def snapshot() -> dict:
    return {"settings": {k: settings.get(k) for k in settings.FIELD_NAMES if k.startswith("scale_")},
            "open_ce_adds": ce_add_book.snapshot()["open_positions"],
            "open_pe_hedge_copies": pe_copy_book.snapshot()["open_positions"],
            "open_pe_adds": pe_add_book.snapshot()["open_positions"],
            "state": _st()}
