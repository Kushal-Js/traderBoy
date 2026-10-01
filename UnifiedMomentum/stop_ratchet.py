"""
Ratcheted broker stop for Unified Momentum's REAL positions (1 Oct 2026, user:
"PUT hedges: add the ratchet and keep the 30% trail. Calls: move Dhan's order to
the entry price once a call reaches +Rs 1,500. Limits: a Rs 250 minimum step and
one change every few seconds at most").

Every real position gets a stop-loss LIMIT order at Dhan when it opens, at its
loss cap (call: -max_loss_rs, hedge PUT: -hedge_stop_rs). Until now the PROFIT
side (the call's breakeven stop, the hedge's 30% trail) was enforced only by the
bot watching prices - nothing protected it while the bot restarted, froze or
lost its price feed. This module moves that broker order UP to the level at
which the bot itself would sell, so Dhan enforces it too:

  hedge PUT  once the trail is armed (peak profit >= hedge_trail_arm_rs), the
             trigger follows the trail: entry + peak x (1 - hedge_trail_giveback)
             / quantity - the same price the bot sells at (30% giveback).
  call (CE)  once the call has been +breakeven_after_rs in profit, the trigger
             goes to the entry price (the breakeven rule). One move - calls have
             no profit trail (backtests: every call trail lowered profit).

The broker trigger sits BOT_FIRST_GAP (two ticks) under the bot's own level, so
the bot still exits first in normal running and the broker order is the
backstop. Moves only up. Sent only when the new level is at least stop_ratchet_min_step_rs
above what the order holds (on the whole quantity), at most once every
stop_ratchet_min_interval_seconds per order, and never at or above the live
price (the exchange rejects a sell stop above the market - the bot's own exit
acts on that tick anyway). The limit sits below the trigger by the same gap as
the original order. Runs as a background task: never delays a tick.

If a move fails (rejected, network), it is logged (STOP_RATCHET_FAILED) and
nothing else changes - the bot's own exits keep working exactly as before;
after MAX_FAILURES failures for an order it stops trying (STOP_RATCHET_GAVE_UP).
When the broker order fills, the existing broker-stop-filled check closes the
position; when the bot exits first, its exit cancels the order (unchanged).

State (the level each order holds): data/unified_momentum_stop_ratchet.json, so a
restart continues from the moved level. Events: hedge -> the supervisor log,
call -> the events log. Setting stop_ratchet_mode: off | shadow (log the moves
it would make) | on. GET /unified-momentum/stop-ratchet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from Bollinger import config as bcfg
from Bollinger import trading_engine as engine
from Options.dhan_client import IST, OrderStatus, dhan_wrapper
from Swing.position_store import broker_stop_trigger_and_limit

from . import settings
from .state import EVENTS_LOG, SUPERVISOR_LOG

logger = logging.getLogger("unified_momentum_stop_ratchet")

STATE_FILE = Path("data/unified_momentum_stop_ratchet.json")
MAX_FAILURES = 3
VERIFY_AFTER_SECONDS = 2.0     # read the order back this long after Dhan accepts a move (1 Oct 2026)
PRICE_BUFFER = 0.05            # the moved trigger stays at least one tick below the live price
BOT_FIRST_GAP = 0.10           # broker trigger two ticks under the bot's own exit level: normally the bot sells
                               # first (its exit cancels this order, checks the broker quantity, then sells), and
                               # the broker order is the backstop when the bot cannot act - not a race on every exit

_state: Optional[dict] = None
_inflight: set[str] = set()
_last_attempt: dict[str, float] = {}
_verify_tasks: set = set()


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _st() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        except Exception:  # noqa: BLE001
            logger.exception("could not read %s - starting empty", STATE_FILE)
            _state = {}
        _state = {k: v for k, v in _state.items() if v.get("day") == _today()}
    return _state


def _save() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, indent=1))
        os.replace(tmp, STATE_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("could not persist %s", STATE_FILE)


def _cap(kind: str) -> float:
    return settings.get("hedge_stop_rs") if kind == "hedge" else settings.get("max_loss_rs")


def desired_trigger(kind: str, entry: float, best: float, qty: float) -> Optional[float]:
    """Pure. The price the broker stop should sit at now, or None (not yet)."""
    peak = (best - entry) * qty
    if kind == "hedge":
        if peak >= settings.get("hedge_trail_arm_rs"):
            return entry + peak * (1 - settings.get("hedge_trail_giveback")) / qty
        return None
    if peak >= settings.get("breakeven_after_rs"):
        return entry
    return None


def original_trigger_and_gap(kind: str, entry: float, qty: int) -> tuple[float, float]:
    """The trigger the order was PLACED at (same formula as the entry/hedge code) and its limit gap."""
    trig, limit = broker_stop_trigger_and_limit("LONG", entry, qty, _cap(kind), bcfg.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE,
                                                hard_stop_pct=0.95)
    return trig, trig - limit


def _key(kind: str, pos) -> str:
    return f"{kind}|{pos.underlying_symbol}|{pos.trading_symbol}|{pos.stop_loss_order_id}"


def decide(kind: str, pos, ltp: float, now: Optional[float] = None) -> Optional[dict]:
    """Pure-ish (reads the state): the move to make now, or None. Updates nothing."""
    if settings.get("stop_ratchet_mode") == "off" or not pos.stop_loss_order_id or pos.pending_exit_order_id:
        return None
    qty = int(pos.pnl_multiplier or pos.quantity)
    want = desired_trigger(kind, pos.entry_price, max(pos.best_price, ltp), qty)
    if want is None:
        return None
    key = _key(kind, pos)
    row = _st().get(key, {})
    if row.get("failures", 0) >= MAX_FAILURES or key in _inflight:
        return None
    trigger = want - BOT_FIRST_GAP
    orig, gap = original_trigger_and_gap(kind, pos.entry_price, qty)
    current = row.get("trigger", orig)
    if (trigger - current) * qty < settings.get("stop_ratchet_min_step_rs"):
        return None
    if trigger > ltp - PRICE_BUFFER:
        return None
    now = time.monotonic() if now is None else now
    if now - _last_attempt.get(key, -1e9) < settings.get("stop_ratchet_min_interval_seconds"):
        return None
    return {"key": key, "trigger": trigger, "limit": max(trigger - gap, 0.05), "from_trigger": current, "qty": qty,
            "bot_level": want, "locks_rs": round((trigger - pos.entry_price) * qty)}


async def maybe_ratchet(kind: str, symbol: str, pos, ltp: float) -> None:
    """Called on every price of a REAL position (no exit decided). Starts a background move when due."""
    try:
        move = decide(kind, pos, ltp)
    except Exception:  # noqa: BLE001
        logger.exception("[stop ratchet] %s: decision failed", symbol)
        return
    if move is None:
        return
    _inflight.add(move["key"])
    _last_attempt[move["key"]] = time.monotonic()
    asyncio.create_task(_move(kind, symbol, pos, ltp, move))


async def _move(kind: str, symbol: str, pos, ltp: float, move: dict) -> None:
    log_name = SUPERVISOR_LOG if kind == "hedge" else EVENTS_LOG
    detail = {"component": "stop_ratchet", "kind": kind, "trading_symbol": pos.trading_symbol,
              "order_id": pos.stop_loss_order_id, "entry": pos.entry_price, "best": pos.best_price, "ltp": ltp,
              "from_trigger": round(move["from_trigger"], 2), "trigger": round(move["trigger"], 2),
              "limit": round(move["limit"], 2), "locks_rs": move["locks_rs"]}
    st = _st()
    try:
        if settings.get("stop_ratchet_mode") == "shadow":
            st[move["key"]] = {**st.get(move["key"], {}), "day": _today(), "trigger": move["trigger"], "shadow": True}
            _save()
            await engine._record_bollinger_event("STOP_RATCHET_WOULD_MOVE", symbol, detail, log_name)
            return
        # The order's own quantity (what it was placed with), not the P&L multiplier - a modify with a
        # different quantity would resize the stop. An exit that starts meanwhile waits for this request
        # to finish before cancelling (engine.orders_being_modified).
        engine.orders_being_modified.add(str(pos.stop_loss_order_id))
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, dhan_wrapper.modify_stop_loss_limit_order, pos.stop_loss_order_id, pos.trading_symbol,
                int(pos.quantity), move["trigger"], move["limit"])
        finally:
            engine.orders_being_modified.discard(str(pos.stop_loss_order_id))
        st[move["key"]] = {"day": _today(), "trigger": resp.get("trigger_price", move["trigger"]),
                           "limit": resp.get("limit_price", move["limit"]), "moved_at": datetime.now(IST).isoformat(),
                           "failures": 0}
        _save()
        logger.info("[stop ratchet] %s: broker stop %s moved %.2f -> %.2f (locks %+d)", symbol, pos.stop_loss_order_id,
                    move["from_trigger"], move["trigger"], move["locks_rs"])
        await engine._record_bollinger_event("STOP_RATCHET_MOVED", symbol, detail, log_name)
        task = asyncio.create_task(_verify(symbol, pos.stop_loss_order_id, move["key"],
                                           resp.get("trigger_price", move["trigger"]), detail, log_name))
        _verify_tasks.add(task)                      # keep a reference until it finishes
        task.add_done_callback(_verify_tasks.discard)
    except Exception as exc:  # noqa: BLE001
        row = st.setdefault(move["key"], {"day": _today()})
        row["failures"] = row.get("failures", 0) + 1
        _save()
        logger.warning("[stop ratchet] %s: could not move broker stop %s (%r) - the bot's own exit still applies",
                       symbol, pos.stop_loss_order_id, exc)
        await engine._record_bollinger_event("STOP_RATCHET_FAILED", symbol, {**detail, "error": repr(exc)[:300],
                                                                             "failures": row["failures"]}, log_name)
        if row["failures"] >= MAX_FAILURES:
            await engine._record_bollinger_event("STOP_RATCHET_GAVE_UP", symbol, detail, log_name)
    finally:
        _inflight.discard(move["key"])


async def _verify(symbol: str, order_id: str, key: str, expected_trigger: float, detail: dict, log_name: str) -> None:
    """Read the order back VERIFY_AFTER_SECONDS after Dhan accepted the move (1 Oct 2026: the modify call had
    never run against Dhan). Read-only; changes no order.
      trigger matches, order still open -> STOP_RATCHET_VERIFIED
      order filled                      -> nothing (the broker-stop-filled check closes the position)
      order cancelled/rejected/expired  -> STOP_RATCHET_STOP_GONE (ERROR): the position has no broker stop now;
                                           the bot's own exits still apply
      trigger not the one sent          -> STOP_RATCHET_NOT_APPLIED: the state goes back to the broker's real
                                           trigger and it counts as a failure (3 -> the ratchet stops for it)
      read failed                       -> STOP_RATCHET_VERIFY_FAILED (state unchanged)"""
    await asyncio.sleep(VERIFY_AFTER_SECONDS)
    try:
        got = await asyncio.get_running_loop().run_in_executor(None, dhan_wrapper.get_order_prices, order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[stop ratchet] %s: could not read broker stop %s back (%r)", symbol, order_id, exc)
        await engine._record_bollinger_event("STOP_RATCHET_VERIFY_FAILED", symbol,
                                             {**detail, "error": repr(exc)[:300]}, log_name)
        return
    info = {**detail, "broker_status": got["status"], "broker_trigger": got["trigger_price"],
            "broker_limit": got["limit_price"], "broker_quantity": got["quantity"], "broker_remark": got["remark"]}
    if got["status"] == OrderStatus.TRADED or str(order_id) in engine._orders_confirmed_gone:
        return    # filled, or the bot's own exit cancelled it meanwhile - both expected
    if got["status"] in OrderStatus.TERMINAL_STATUSES:
        logger.error("[stop ratchet] %s: broker stop %s is %s after the move - the position has NO broker stop "
                     "now; the bot's own exits still apply", symbol, order_id, got["status"])
        await engine._record_bollinger_event("STOP_RATCHET_STOP_GONE", symbol, info, log_name)
        return
    if abs(got["trigger_price"] - float(expected_trigger)) > 0.001:
        row = _st().setdefault(key, {"day": _today()})
        if got["trigger_price"] > 0:
            row["trigger"], row["limit"] = got["trigger_price"], got["limit_price"]
        row["failures"] = row.get("failures", 0) + 1
        _save()
        logger.warning("[stop ratchet] %s: broker stop %s holds trigger %.2f, not the %.2f sent", symbol, order_id,
                       got["trigger_price"], float(expected_trigger))
        await engine._record_bollinger_event("STOP_RATCHET_NOT_APPLIED", symbol, {**info, "failures": row["failures"]},
                                             log_name)
        return
    await engine._record_bollinger_event("STOP_RATCHET_VERIFIED", symbol, info, log_name)


def snapshot() -> dict:
    return {"mode": settings.get("stop_ratchet_mode"), "min_step_rs": settings.get("stop_ratchet_min_step_rs"),
            "min_interval_seconds": settings.get("stop_ratchet_min_interval_seconds"), "orders": _st(),
            "in_flight": sorted(_inflight)}
