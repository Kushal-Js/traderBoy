"""
Best-price memory for REAL Super Bollinger positions (30 Sep 2026, user
request: "take log and snapshot of running trades so that bot can reconcile
after restart").

Restart reconciliation rebuilds a real position from the broker's net
position, which has no memory of the best price the trade reached - so
after a restart the CE's breakeven stop (armed once the trade has been
Rs breakeven_after_rs in profit) and the hedge PE's profit trail (armed once
the PE has been Rs hedge_trail_arm_rs in profit) were silently disarmed.
Found live 30 Sep: the APLAPOLLO hedge had an armed trail (best 61.70 vs
entry 53.10) that a restart would have reset to 53.10.

The supervisor loop records every real position's best price here
(data/super_bollinger_best_prices.json, only written when a best price
rises); startup reconciliation restores it for a position that matches the
same contract, the same day and (within 1%) the same entry price. The same
file format is used to seed the memory from a pre-restart snapshot.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("super_bollinger_best_price_memory")

FILE = Path("data/super_bollinger_best_prices.json")
ENTRY_TOLERANCE = 0.01
_mem: dict | None = None


def _today() -> str:
    from Bollinger.trading_engine import _now_ist
    return _now_ist().date().isoformat()


def _key(strategy: str, trading_symbol: str) -> str:
    return f"{strategy}|{trading_symbol}"


def _load() -> dict:
    global _mem
    if _mem is None:
        try:
            _mem = json.loads(FILE.read_text()) if FILE.exists() else {}
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s - starting with an empty best-price memory", FILE)
            _mem = {}
    return _mem


def _save(mem: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(mem, indent=2))
        os.replace(tmp, FILE)
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist %s", FILE)


def record(strategy: str, positions: Iterable) -> None:
    """Remember each position's best price if it is new or higher. Entries
    from earlier days are dropped on the next write."""
    mem, today, changed = _load(), _today(), False
    for pos in positions:
        k = _key(strategy, pos.trading_symbol)
        old = mem.get(k)
        same = (old and old.get("day") == today
                and abs(float(old.get("entry", 0)) - pos.entry_price) <= ENTRY_TOLERANCE * pos.entry_price)
        if same and float(old.get("best", 0)) >= pos.best_price:
            continue
        mem[k] = {"day": today, "entry": pos.entry_price, "best": pos.best_price}
        changed = True
    if changed:
        for k in [k for k, v in mem.items() if v.get("day") != today]:
            mem.pop(k, None)
        _save(mem)


def restore(strategy: str, positions: Iterable) -> list[str]:
    """Raise each reconciled position's best_price to the remembered one
    (same contract, same day, entry within 1%). Returns what was restored."""
    mem, today, restored = _load(), _today(), []
    for pos in positions:
        m = mem.get(_key(strategy, pos.trading_symbol))
        if not m or m.get("day") != today:
            continue
        if abs(float(m["entry"]) - pos.entry_price) > ENTRY_TOLERANCE * pos.entry_price:
            logger.warning("[%s] %s: remembered best price ignored - entry %.2f vs broker avg %.2f", strategy,
                           pos.trading_symbol, float(m["entry"]), pos.entry_price)
            continue
        if float(m["best"]) > pos.best_price:
            logger.info("[%s] %s: best price restored after restart %.2f -> %.2f (entry %.2f)", strategy,
                        pos.trading_symbol, pos.best_price, float(m["best"]), pos.entry_price)
            pos.best_price = float(m["best"])
            restored.append(f"{pos.trading_symbol} best={pos.best_price}")
    return restored


def seed(strategy: str, trading_symbol: str, entry: float, best: float, day: date | None = None) -> None:
    """Pre-restart snapshot helper (also usable from a shell)."""
    mem = _load()
    mem[_key(strategy, trading_symbol)] = {"day": (day.isoformat() if day else _today()), "entry": entry, "best": best}
    _save(mem)
