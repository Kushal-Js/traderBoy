"""
Restart memory for the REAL positions of Swing and Bollinger (30 Sep 2026,
user: "extend the restart-safe state file to Swing and Bollinger - they
still lose trailing state on restart").

Both packages rebuild a real position after a restart from the broker's net
position. The broker knows the contract, the quantity and the average price
- not what the bot had learned since entry. So a restart (there is one every
morning at 08:00 IST, and Swing/Bollinger positions are carried overnight)
reset:
  Swing      best_price (the trailing memory), the entry regime, the real
             opened_at (the Supertrend-exit guard compares against it), the
             target/stop computed at entry;
  Bollinger  best_price, the per-trade stop_pct and everything derived from
             it (hard stop, trailing distance/step), trailing_armed and the
             trailing stop price - reconciliation fell back to a flat
             MIN_STOP_PCT with the trail not armed.

This module keeps those fields on disk, one file per strategy
(data/<strategy>_position_memory.json):
  record()   from the package's monitor tick - writes only when something
             changed; atomic replace.
  restore()  at startup, right after the broker reconciliation succeeded -
             puts the remembered fields back on a reconciled position when
             it is the SAME position: same contract, same quantity, same
             side, and the broker's average price within 1% of the
             remembered entry. Anything else is left exactly as the
             reconciliation built it, and said so in the log.

Safety rules:
  - the broker stays the source of truth for what is open (contract,
    quantity, side, average price); this file only adds what the broker
    cannot know. A remembered position that the broker no longer has is
    never re-created.
  - a position is forgotten when it is no longer live - but only after a
    successful restore() in this process. If the startup reconciliation
    failed (broker unreachable), nothing is pruned, so the next restart can
    still restore.
  - best_price only ever moves in the position's favour on restore.
Paper positions are not handled here (Bollinger's paper book already
persists itself; see Bollinger/paper_book.py).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("position_memory")

DATA_DIR = Path(__file__).resolve().parent / "data"
ENTRY_TOLERANCE = 0.01
DATETIME_FIELDS = {"opened_at", "entry_candle_start", "supertrend_entry_candle_start"}

# What each package remembers on top of the identity fields below.
SWING_FIELDS = ("best_price", "regime", "opened_at", "supertrend_entry_candle_start", "target_price", "hard_stop_loss")
BOLLINGER_FIELDS = ("best_price", "stop_pct", "hard_stop_loss", "trailing_stop_dist", "trailing_step", "trailing_armed",
                    "trailing_stop_price", "opened_at", "entry_candle_start")
IDENTITY = ("underlying_symbol", "trading_symbol", "quantity", "instrument_side", "entry_price")

_last_written: dict[str, str] = {}      # strategy -> the JSON text on disk
_ready: set[str] = set()                # strategies whose startup restore() ran (pruning allowed)
_last_report: dict[str, dict] = {}


def _file(strategy: str) -> Path:
    return DATA_DIR / f"{strategy.lower()}_position_memory.json"


def _dump(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _parse(name: str, value):
    if name in DATETIME_FIELDS and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _read(strategy: str) -> dict:
    """The remembered rows (from the file the first time, then from what this process last wrote)."""
    if strategy in _last_written:
        return json.loads(_last_written[strategy])
    try:
        f = _file(strategy)
        text = f.read_text() if f.exists() else "{}"
        rows = json.loads(text)
        _last_written[strategy] = json.dumps(rows, indent=1, sort_keys=True)
        return rows
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not read %s - starting with an empty position memory", strategy, _file(strategy))
        return {}


def _write(strategy: str, rows: dict) -> None:
    text = json.dumps(rows, indent=1, sort_keys=True)
    if _last_written.get(strategy) == text:
        return
    try:
        f = _file(strategy)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, f)
        _last_written[strategy] = text
    except Exception:  # noqa: BLE001
        logger.exception("[%s] could not persist %s", strategy, _file(strategy))


def record(strategy: str, positions: Iterable, fields: tuple[str, ...]) -> None:
    """Remember every live real position's restart-critical fields. Blocking
    (a small file write, only when something changed) - call it from an
    executor or accept the few hundred microseconds."""
    live = {p.trading_symbol: p for p in positions}
    old = _read(strategy)
    rows = {k: v for k, v in old.items() if k in live} if strategy in _ready else dict(old)   # closed -> forgotten
    for sym, p in live.items():
        rows[sym] = {**{k: _dump(getattr(p, k)) for k in IDENTITY}, **{k: _dump(getattr(p, k, None)) for k in fields}}
    _write(strategy, rows)


def restore(strategy: str, positions: Iterable, fields: tuple[str, ...]) -> dict:
    """Put the remembered fields back on the positions the broker
    reconciliation just built. Call ONLY when that reconciliation succeeded
    (also when it found nothing). Returns a small report, kept for
    last_report()."""
    rows = _read(strategy)
    report = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "restored": [], "not_restored": [],
              "remembered_but_not_at_broker": []}
    seen = set()
    for pos in positions:
        seen.add(pos.trading_symbol)
        row = rows.get(pos.trading_symbol)
        if row is None:
            report["not_restored"].append({"trading_symbol": pos.trading_symbol, "why": "nothing remembered"})
            continue
        why = _mismatch(pos, row)
        if why:
            logger.warning("[%s] %s: remembered state NOT restored - %s", strategy, pos.trading_symbol, why)
            report["not_restored"].append({"trading_symbol": pos.trading_symbol, "why": why})
            continue
        changed = {}
        for k in fields:
            if k not in row or not hasattr(pos, k):
                continue
            new = _parse(k, row[k])
            if k == "best_price":
                if new is None:
                    continue
                better = max if pos.instrument_side == "LONG" else min
                new = better(float(new), pos.best_price)
            if new is None and k in DATETIME_FIELDS:
                continue
            if getattr(pos, k) != new:
                changed[k] = [_dump(getattr(pos, k)), _dump(new)]
                setattr(pos, k, new)
        logger.info("[%s] %s: state restored after restart: %s", strategy, pos.trading_symbol, changed or "nothing differed")
        report["restored"].append({"trading_symbol": pos.trading_symbol, "changed": changed})
    for sym in rows:
        if sym not in seen:
            logger.warning("[%s] %s was remembered as open but the broker reconciliation did not return it - "
                           "treated as closed while the bot was down (not re-created)", strategy, sym)
            report["remembered_but_not_at_broker"].append(sym)
    _ready.add(strategy)
    _last_report[strategy] = report
    return report


def _mismatch(pos, row: dict) -> Optional[str]:
    if int(row.get("quantity", -1)) != int(pos.quantity):
        return f"quantity {row.get('quantity')} remembered vs {pos.quantity} at the broker"
    if row.get("instrument_side") != pos.instrument_side:
        return f"side {row.get('instrument_side')} remembered vs {pos.instrument_side} at the broker"
    entry = float(row.get("entry_price") or 0)
    if entry <= 0 or abs(entry - pos.entry_price) > ENTRY_TOLERANCE * pos.entry_price:
        return f"entry {entry} remembered vs broker average {pos.entry_price} (more than 1% apart - a different position)"
    return None


def last_report(strategy: str) -> dict:
    return _last_report.get(strategy) or {"at": None, "restored": [], "not_restored": [], "remembered_but_not_at_broker": [],
                                          "note": "no restore has run in this process"}


def remembered(strategy: str) -> dict:
    return _read(strategy)
