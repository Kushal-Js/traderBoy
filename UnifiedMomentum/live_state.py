"""
Unified Momentum LIVE STATE (30 Sep 2026, user idea: "rather than only relying
on memory, keep a live-trade JSON so that after a restart we can track and
reconcile based on it").

Before this, a restart rebuilt real positions from the broker's net position
alone, and everything the broker does not know was lost or reset. Found live
on 30 Sep (five mid-session restarts):
  - best price -> breakeven stop / hedge trail disarmed (APLAPOLLO hedge);
  - candle feed + price history for a HELD stock -> the hedge could not
    evaluate its ATR condition (GLENMARK hedge fired 7 minutes late);
  - the supervisor's per-trade memory: "this CE was already hedged" (a second
    hedge could fire), the entry spot, shadow-rule flags;
  - today's closed trades and the "halted" flag -> the disaster brake only
    counted PnL since the last restart and forgot a triggered halt;
  - which signals were already used (a trigger could be traded twice);
  - an order sent but not yet confirmed (an orphan, like SONACOMS).

What this module does:
  1. STATE FILE data/unified_momentum_live_state.json - every open real CE and
     hedge with its full management state, today's closed trades, the
     supervisor's per-trade memory, used signals, the brake/halt flag and
     open order intents. Written atomically by the supervisor loop whenever
     anything changed (and immediately around every order).
  2. WRITE-AHEAD ORDER INTENTS - recorded BEFORE an entry/hedge order is
     sent, updated with the order id, removed once the outcome is handled.
     An intent still on file at startup is looked up at the broker: open ->
     cancelled; filled -> the position is adopted (backstop stop-loss placed,
     then managed normally).
  3. THREE-WAY STARTUP RECONCILE (restore()) - state file vs the broker's
     positions vs open orders:
       in both            -> restored exactly (opened_at, best price, ids...)
       file only          -> it closed while the bot was down: recorded as
                             closed (exit price from its stop-loss order if
                             that traded)
       broker only        -> rebuilt from the broker as before, flagged
       broker unreachable -> the file is trusted and every position in it
                             is managed (never dropped on a failed API call)
  4. WARM-UP - every restored position's underlying is re-subscribed and its
     candle series loaded before the first supervisor check.
  5. RESTART REPORT - one RESTART_RECONCILE_REPORT event (supervisor log) and
     GET /unified-momentum/restart-report: what was restored and what needs a
     human look.
A missing, stale (another day) or unreadable file falls back to the broker-
only rebuild, so a restart can never be worse than before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from Bollinger import config as bcfg
from Bollinger import trading_engine as engine
from Bollinger.paper_book import _from_json, _to_json
from Bollinger.position_store import OrderRecord, Position
from Options.dhan_client import OrderStatus, dhan_wrapper
from Swing.position_store import broker_stop_trigger_and_limit
from trade_history import record_closed_trade

from . import best_price_memory, settings
from .state import HEDGE_STRATEGY, PUT_STRATEGY, STRATEGY, SUPERVISOR_LOG, halted, hedge_store, position_store, put_store

logger = logging.getLogger("unified_momentum_live_state")

FILE = Path("data/unified_momentum_live_state.json")
ENTRY_TOLERANCE = 0.01
_intents: dict = {}
_last_digest: Optional[str] = None
_report: dict = {}


def _today() -> str:
    return engine._now_ist().date().isoformat()


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #
def collect() -> dict:
    from . import engine_b, supervisor            # function-local: supervisor/trading_engine import this module
    from .trading_engine import PROFILE
    return {
        "day": _today(),
        "positions": [_to_json(p) for p in position_store.live_positions.values()],
        "hedges": [_to_json(p) for p in hedge_store.live_positions.values()],
        "puts": [_to_json(p) for p in put_store.live_positions.values()],
        "closed": {STRATEGY: [_to_json(p) for p in position_store.closed_positions_today],
                   HEDGE_STRATEGY: [_to_json(p) for p in hedge_store.closed_positions_today],
                   PUT_STRATEGY: [_to_json(p) for p in put_store.closed_positions_today]},
        "engine_b": engine_b.state_snapshot(),
        "track": {f"{k[0]}|{k[1]}": v for k, v in supervisor._track.items()},
        "consumed": {s: (c.isoformat() if hasattr(c, "isoformat") else c) for s, c in PROFILE.consumed.items()},
        "halted": {"day": halted["day"].isoformat() if halted["day"] else None, "reason": halted["reason"]},
        "intents": _intents,
    }


def save(force: bool = False) -> bool:
    """Write the state file if anything changed. Never raises."""
    global _last_digest
    try:
        data = collect()
        digest = json.dumps(data, sort_keys=True, default=str)
        if not force and digest == _last_digest:
            return False
        data["saved_at"] = engine._now_ist().isoformat()
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        os.replace(tmp, FILE)
        _last_digest = digest
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Could not write %s - the bot keeps running on its in-memory state", FILE)
        return False


def load() -> Optional[dict]:
    if not FILE.exists():
        return None
    try:
        data = json.loads(FILE.read_text())
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s - falling back to the broker-only rebuild", FILE)
        return None
    if data.get("day") != _today():
        logger.info("%s is from %s, not today - ignored", FILE, data.get("day"))
        return None
    return data


# --------------------------------------------------------------------------- #
# Write-ahead order intents
# --------------------------------------------------------------------------- #
def intent_begin(kind: str, symbol: str, leg: dict, quantity: int, **extra) -> str:
    """kind: "entry" (engine A CE), "hedge" (PE) or "put_entry" (engine B PE). Call BEFORE sending the order."""
    iid = uuid.uuid4().hex[:12]
    _intents[iid] = {"kind": kind, "symbol": symbol, "trading_symbol": leg["trading_symbol"], "quantity": quantity,
                     "lot_size": leg.get("lot_size"), "product_type": leg.get("product_type"),
                     "order_id": None, "status": "PLACING", "at": engine._now_ist().isoformat(), **extra}
    save(force=True)
    return iid


def intent_order(iid: str, order_id: str) -> None:
    if iid in _intents:
        _intents[iid].update(order_id=order_id, status="SENT")
        save(force=True)


def intent_done(iid: Optional[str]) -> None:
    if iid and _intents.pop(iid, None) is not None:
        save(force=True)


async def intent_finish(iid: Optional[str], outcome_known: bool) -> None:
    """Call in the `finally` of an order path. outcome_known=True: the order's
    result was handled (position added, or confirmed not filled) - drop the
    intent. False: something failed between sending the order and handling
    its result - look the order up NOW (cancel it, or adopt the fill) instead
    of leaving it for the next restart. If even that fails the intent stays
    on file."""
    if not iid or iid not in _intents:
        return
    if outcome_known:
        intent_done(iid)
        return
    try:
        tracked = {p.trading_symbol for p in list(position_store.live_positions.values())
                   + list(hedge_store.live_positions.values()) + list(put_store.live_positions.values())}
        out = await _resolve_intent(iid, _intents[iid], tracked, None)
        logger.warning("[%s] order intent %s resolved after an error: %s", STRATEGY, iid, out)
        await engine._record_bollinger_event("ORDER_INTENT_RESOLVED", _intents[iid]["symbol"],
                                             {"component": "live_state", **out}, SUPERVISOR_LOG)
        if str(out.get("outcome", "")).startswith("not_filled_") and out.get("needs_review"):
            return   # the order is STILL open at the broker - keep the intent so it is looked at again
        intent_done(iid)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not resolve order intent %s - kept on file for the next restart", STRATEGY, iid)


# --------------------------------------------------------------------------- #
# Startup reconcile
# --------------------------------------------------------------------------- #
def _match(pos: Position, saved: Position) -> bool:
    return (pos.trading_symbol == saved.trading_symbol
            and abs(pos.entry_price - saved.entry_price) <= ENTRY_TOLERANCE * saved.entry_price)


def _overlay(pos: Position, saved: Position) -> list[str]:
    """Copy what only the bot knew onto a position rebuilt from the broker.
    Entry price, quantity and the resting stop order stay the broker's."""
    restored = []
    if saved.opened_at and saved.opened_at != pos.opened_at:
        pos.opened_at = saved.opened_at
        restored.append("opened_at")
    if saved.order_id and not pos.order_id:
        pos.order_id = saved.order_id
        restored.append("order_id")
    if saved.entry_candle_start and not pos.entry_candle_start:
        pos.entry_candle_start = saved.entry_candle_start
        restored.append("entry_candle_start")
    if saved.best_price > pos.best_price:
        pos.best_price = saved.best_price
        restored.append(f"best_price={saved.best_price}")
    return restored


async def _closed_while_down(store, strategy: str, saved: Position, note: list) -> None:
    loop = asyncio.get_running_loop()
    exit_price, reason = None, "CLOSED_WHILE_BOT_WAS_DOWN"
    if saved.stop_loss_order_id:
        try:
            res = await loop.run_in_executor(None, dhan_wrapper.refresh_order_status, saved.stop_loss_order_id)
            if res.status == OrderStatus.TRADED and res.fill_price:
                exit_price, reason = res.fill_price, "STOP_LOSS_HIT"
        except Exception:  # noqa: BLE001
            logger.exception("[%s] %s: could not read stop-loss order %s", strategy, saved.trading_symbol,
                             saved.stop_loss_order_id)
    saved.status, saved.exit_reason, saved.exit_price, saved.closed_at = "CLOSED", reason, exit_price, engine._now_ist()
    store.closed_positions_today.append(saved)
    try:
        await record_closed_trade(strategy, saved)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s: could not write the closed-trade record", strategy, saved.trading_symbol)
    note.append({"trading_symbol": saved.trading_symbol, "exit_reason": reason, "exit_price": exit_price,
                 "needs_review": exit_price is None})


async def _reconcile_book(store, strategy: str, reconciled: list[Position], saved_rows: list, broker_symbols: Optional[set],
                          report: dict, key: str) -> list[Position]:
    """-> the positions to manage for this book (CE book or hedge book)."""
    saved = []
    for row in saved_rows:
        try:
            saved.append(_from_json(row))
        except Exception:  # noqa: BLE001
            logger.exception("[%s] unreadable position row in %s: %s", strategy, FILE, row)
    out, used = [], set()
    for pos in reconciled:
        s = next((x for i, x in enumerate(saved) if i not in used and _match(pos, x)), None)
        if s is None:
            report[key].append({"trading_symbol": pos.trading_symbol, "source": "broker_only",
                                "restored": best_price_memory.restore(strategy, [pos])})
        else:
            used.add(saved.index(s))
            restored = _overlay(pos, s) + best_price_memory.restore(strategy, [pos])
            report[key].append({"trading_symbol": pos.trading_symbol, "source": "state_file+broker", "restored": restored})
        out.append(pos)
    for i, s in enumerate(saved):
        if i in used:
            continue
        if broker_symbols is None or s.trading_symbol in broker_symbols:
            # The broker could not be asked, or still shows the contract although our trade history did
            # not attribute it: keep managing what we know rather than dropping a real position.
            s.reconciled = True
            out.append(s)
            report[key].append({"trading_symbol": s.trading_symbol, "needs_review": True,
                                "source": "state_file_only_broker_unreachable" if broker_symbols is None
                                else "state_file_only_unattributed_at_broker", "restored": ["everything"]})
        else:
            await _closed_while_down(store, strategy, s, report["closed_while_down"])
    return out


async def _place_backstop(symbol: str, trading_symbol: str, quantity: int, fill: float, loss_rs: float,
                          product_type: str, store) -> Optional[str]:
    if not bcfg.BROKER_STOP_LOSS_ENABLED:
        return None
    loop = asyncio.get_running_loop()
    trig, limit = broker_stop_trigger_and_limit("LONG", fill, quantity, loss_rs, bcfg.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE,
                                                hard_stop_pct=0.95)
    try:
        sl = await loop.run_in_executor(None, dhan_wrapper.place_stop_loss_limit_order, trading_symbol, quantity, "SELL",
                                        trig, limit, engine._gen_tag("SL", symbol), product_type)
        await store.record_order(OrderRecord(order_id=sl["order_id"], underlying_symbol=symbol, trading_symbol=trading_symbol,
                                             transaction_type="SELL", quantity=quantity, status="PENDING", is_amo=False))
        return sl["order_id"]
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not place the backstop stop-loss for an adopted position %s", symbol, trading_symbol)
        return None


async def _resolve_intent(iid: str, it: dict, tracked: set, broker_symbols: Optional[set]) -> dict:
    """An order intent left on file = the bot stopped between sending an order
    and handling its result. Make sure nothing is left unmanaged."""
    from . import supervisor
    from .trading_engine import _new_position, settle_unfilled_order
    loop = asyncio.get_running_loop()
    symbol, ts, qty, kind = it["symbol"], it["trading_symbol"], int(it["quantity"]), it["kind"]
    out = {"intent": iid, "kind": kind, "trading_symbol": ts, "order_id": it.get("order_id")}
    oid = it.get("order_id")
    if not oid:
        try:
            oid = await loop.run_in_executor(None, dhan_wrapper.get_pending_order_id, ts, "BUY", "NSE")
        except Exception:  # noqa: BLE001
            oid = None
        if not oid:
            untracked = broker_symbols is not None and ts in broker_symbols and ts not in tracked
            out.update(outcome="no_order_found", needs_review=untracked,
                       note="broker shows a position in this contract that the bot does not track" if untracked else None)
            return out
        out["order_id"] = oid
    try:
        res = await loop.run_in_executor(None, dhan_wrapper.refresh_order_status, oid)
    except Exception as exc:  # noqa: BLE001
        out.update(outcome="status_unknown", needs_review=True, note=repr(exc))
        return out
    if res.status in OrderStatus.OPEN_STATUSES:
        res = await settle_unfilled_order(symbol, ts, oid, res, False, f"{kind} (restart)")
    if res.status != OrderStatus.TRADED:
        out.update(outcome=f"not_filled_{res.status}", needs_review=res.status in OrderStatus.OPEN_STATUSES)
        return out
    if ts in tracked:
        out.update(outcome="filled_already_tracked")
        return out
    fill = res.fill_price or 0.0
    if fill <= 0:
        out.update(outcome="filled_no_price", needs_review=True)
        return out
    leg = {"trading_symbol": ts, "product_type": it.get("product_type") or bcfg.OPTIONS_PRODUCT, "quantity": qty,
           "lot_size": it.get("lot_size")}
    if kind == "hedge":
        sl_id = await _place_backstop(symbol, ts, qty, fill, settings.get("hedge_stop_rs"), leg["product_type"], hedge_store)
        await hedge_store.add_position(supervisor._hedge_position(symbol, leg, qty, fill, oid, sl_id))
    elif kind == "put_entry":
        sl_id = await _place_backstop(symbol, ts, qty, fill, settings.get("b_max_loss_rs"), leg["product_type"], put_store)
        await put_store.add_position(_new_position(symbol, leg, fill, oid, sl_id, option_type="PE",
                                                   max_loss_rs=settings.get("b_max_loss_rs")))
    else:
        sl_id = await _place_backstop(symbol, ts, qty, fill, settings.get("max_loss_rs"), leg["product_type"], position_store)
        await position_store.add_position(_new_position(symbol, leg, fill, oid, sl_id))
    await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, ts)
    out.update(outcome="filled_adopted", entry_price=fill, stop_loss_order_id=sl_id)
    return out


async def restore() -> dict:
    """The whole startup reconcile for Unified Momentum's real books. Returns
    (and keeps, and logs) the restart report."""
    global _report, _intents
    from . import engine_b, supervisor
    from .trading_engine import PROFILE, reconcile_broker_positions
    loop = asyncio.get_running_loop()
    state = load()
    report = {"at": engine._now_ist().isoformat(), "state_file": bool(state),
              "state_saved_at": (state or {}).get("saved_at"), "positions": [], "hedges": [], "puts": [],
              "closed_while_down": [], "intents": [], "day_state": {}, "warm_up": [], "broker_reachable": True}

    broker_symbols: Optional[set] = None
    try:
        broker = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)
        broker_symbols = {bp["trading_symbol"] for bp in broker if bp.get("quantity", 0) > 0}
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not read broker positions at startup - trusting the state file", STRATEGY)
        report["broker_reachable"] = False
    try:
        ces = await reconcile_broker_positions()
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not reconcile CE positions from the broker", STRATEGY)
        ces, broker_symbols, report["broker_reachable"] = [], None, False
    try:
        hedges = await supervisor.reconcile_hedges()
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not reconcile hedge positions from the broker", STRATEGY)
        hedges, broker_symbols, report["broker_reachable"] = [], None, False
    try:
        puts = await engine_b.reconcile_broker_positions()
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not reconcile engine B put positions from the broker", STRATEGY)
        puts, broker_symbols, report["broker_reachable"] = [], None, False

    ces = await _reconcile_book(position_store, STRATEGY, ces, (state or {}).get("positions", []), broker_symbols,
                                report, "positions")
    hedges = await _reconcile_book(hedge_store, HEDGE_STRATEGY, hedges, (state or {}).get("hedges", []), broker_symbols,
                                   report, "hedges")
    puts = await _reconcile_book(put_store, PUT_STRATEGY, puts, (state or {}).get("puts", []), broker_symbols,
                                 report, "puts")
    if ces:
        await position_store.reconcile_from_broker(ces)
    if hedges:
        await hedge_store.reconcile_from_broker(hedges)
    if puts:
        await put_store.reconcile_from_broker(puts)

    if state:
        # ---- day state the broker cannot give back ----
        for strategy, store in ((STRATEGY, position_store), (HEDGE_STRATEGY, hedge_store), (PUT_STRATEGY, put_store)):
            already = {(p.trading_symbol, p.closed_at) for p in store.closed_positions_today}
            rows = []
            for row in (state.get("closed") or {}).get(strategy, []):
                try:
                    p = _from_json(row)
                except Exception:  # noqa: BLE001
                    continue
                if (p.trading_symbol, p.closed_at) not in already:
                    rows.append(p)
            store.closed_positions_today[:0] = rows
            report["day_state"][f"closed_{strategy}"] = len(rows)
        h = state.get("halted") or {}
        if h.get("day") == _today():
            halted["day"], halted["reason"] = date.fromisoformat(h["day"]), h.get("reason")
        report["day_state"]["halted"] = halted["day"] is not None and halted["day"].isoformat() == _today()
        for sym, c in (state.get("consumed") or {}).items():
            try:
                PROFILE.consumed[sym] = datetime.fromisoformat(c) if isinstance(c, str) else c
            except ValueError:
                pass
        report["day_state"]["consumed_signals"] = len(PROFILE.consumed)
        live_keys = {(p.underlying_symbol, p.opened_at.isoformat()) for p in ces}
        for key, v in (state.get("track") or {}).items():   # today's keys only; the supervisor prunes what is gone
            sym, _, opened = key.partition("|")
            if (sym, opened) in live_keys or opened[:10] == _today():
                supervisor._track[(sym, opened)] = v
        report["day_state"]["supervisor_track"] = len(supervisor._track)
        # engine B's fresh-formation state comes from its own file (engine_b.load_state, at startup) - it is
        # written on every change, so it is never older than this snapshot of it
        report["day_state"]["engine_b_used_stocks"] = sum(1 for v in engine_b._consumed.values() if v is not None)

        # ---- orders that were in flight ----
        tracked = {p.trading_symbol for p in ces + hedges + puts}
        for iid, it in list((state.get("intents") or {}).items()):
            try:
                report["intents"].append(await _resolve_intent(iid, it, tracked, broker_symbols))
            except Exception as exc:  # noqa: BLE001
                logger.exception("[%s] could not resolve order intent %s", STRATEGY, iid)
                report["intents"].append({"intent": iid, "outcome": "error", "needs_review": True, "note": repr(exc)})
    _intents = {}

    # ---- warm-up: feeds + candle history for everything we hold ----
    for sym in sorted({p.underlying_symbol for p in list(position_store.live_positions.values())
                       + list(hedge_store.live_positions.values()) + list(put_store.live_positions.values())}):
        ok = await supervisor.ensure_series(sym, force=True)
        report["warm_up"].append({"symbol": sym, "series_loaded": ok})

    report["needs_review"] = (not report["broker_reachable"]
                              or any(x.get("needs_review") for k in ("positions", "hedges", "puts", "closed_while_down", "intents")
                                     for x in report[k])
                              or any(not w["series_loaded"] for w in report["warm_up"]))
    _report = report
    save(force=True)
    try:
        await engine._record_bollinger_event("RESTART_RECONCILE_REPORT", "*", {"component": "live_state", **report},
                                             SUPERVISOR_LOG)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not log the restart report", STRATEGY)
    log = logger.warning if report["needs_review"] else logger.info
    log("[%s] restart reconcile: %d CE, %d hedge(s), %d engine B put(s) restored; closed while down %d; intents %d; "
        "needs_review=%s", STRATEGY, len(report["positions"]), len(report["hedges"]), len(report["puts"]),
        len(report["closed_while_down"]),
        len(report["intents"]), report["needs_review"])
    return report


def last_report() -> dict:
    return _report
