"""
Paper book for the Bollinger strategy (added 28 Sep 2026).

WHY THIS EXISTS: before this, Bollinger's paper mode only logged
"ENTRY_SKIPPED_PAPER_MODE" and simulated nothing, so switching it to paper
produced no evidence about whether the strategy works. This module lets
paper mode run the SAME entry and exit logic as real mode, on live option
prices, without placing any order.

HOW IT WORKS:
  - Entry (Bollinger/trading_engine.py `_enter_paper`) resolves the same ATM
    option real mode would, applies the same minimum-premium gate, and
    "fills" at the option's live price at that moment.
  - Exits use the identical maths as real positions: the shared trailing
    ratchet (position_store.apply_price_to_trailing) and the shared exit
    rule (trading_engine._exit_reason_for: MAX_LOSS -> trailing/hard stop),
    plus the same Friday/index square-off times.
  - Open paper positions are saved to config.PAPER_POSITIONS_FILE after
    every change, so a restart (including the daily 08:00 refresh) picks
    them back up instead of silently dropping them.
  - Every closed paper trade is appended to
    history/<date>_bollinger_paper_trades.log with two P&L numbers:
      pnl_raw      - exit price minus entry price, at the live quotes;
      pnl_modeled  - the same trade with the backtests' slippage model
                     charged on BOTH entry and exit, so paper results can be
                     compared like-for-like with bollinger_research.py.

DELIBERATELY SEPARATE from position_store: paper positions never touch the
real position store, the capacity counter, or trade_history's real-trade
records (which restart reconciliation uses to attribute real broker
positions to a strategy - a paper record there could mis-attribute one).
Paper mode is also not capped by MAX_CONCURRENT_TRADES, matching the
research backtests (which had no capacity limit); the log keeps every trade
so a capacity cap can be applied afterwards when analysing it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Optional

from trade_history import append_jsonl
from Options.dhan_client import IST
from . import config
from .position_store import Position, apply_price_to_trailing

logger = logging.getLogger("bollinger_paper_book")

PAPER_TRADES_LOG_NAME = "bollinger_paper_trades"
_POSITION_FIELDS = {f.name for f in fields(Position)}
_DATETIME_FIELDS = {"opened_at", "closed_at", "next_exit_retry_at", "entry_candle_start"}


def modeled_slippage_pct(premium: float) -> float:
    """Same inverse-to-premium slippage model every Bollinger backtest uses:
    a fixed ~2-tick spread (Rs 0.10) expressed as a % of the premium,
    floored at 0.5% and capped at 10%. ~10% at Rs 1, 2% at Rs 5, 0.5% at
    Rs 20+."""
    if premium <= 0:
        return 0.10
    return min(0.10, max(0.005, 0.10 / premium))


def _to_json(pos: Position) -> dict:
    d = asdict(pos)
    for k in _DATETIME_FIELDS:
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def _from_json(d: dict) -> Position:
    d = {k: v for k, v in d.items() if k in _POSITION_FIELDS}
    for k in _DATETIME_FIELDS:
        if d.get(k):
            d[k] = datetime.fromisoformat(d[k])
    return Position(**d)


class PaperBook:
    """One strategy's paper positions + trade log. `labels` (strategy name and
    rule settings) are stamped on every closed-trade record so a log line
    always says which strategy produced it."""
    def __init__(self, path: str, log_name: str, labels: dict) -> None:
        self._path = Path(path)
        self.log_name = log_name
        self.labels = labels
        self._lock = asyncio.Lock()
        self.positions: dict[str, Position] = {}

    def load(self) -> list[Position]:
        """Called once at startup. A corrupt file is logged and ignored rather
        than blocking startup - paper state must never stop the real bot."""
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text())
            self.positions = {d["underlying_symbol"]: _from_json(d) for d in raw}
        except Exception:  # noqa: BLE001
            logger.exception("Could not load paper positions from %s - starting with none", self._path)
            self.positions = {}
        if self.positions:
            logger.info("[%s] Restored %d open PAPER position(s): %s", self.labels["strategy"],
                        len(self.positions), sorted(self.positions))
        return list(self.positions.values())

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps([_to_json(p) for p in self.positions.values()], indent=2))
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            logger.exception("Could not persist paper positions to %s", self._path)

    async def open(self, pos: Position) -> bool:
        async with self._lock:
            if pos.underlying_symbol in self.positions:
                return False
            self.positions[pos.underlying_symbol] = pos
            self._save()
        logger.info("[%s] PAPER position OPENED: %s (%s) entry=%.2f hard_stop=%.2f stop_pct=%.2f%% qty=%s",
                    self.labels["strategy"], pos.underlying_symbol, pos.trading_symbol, pos.entry_price, pos.hard_stop_loss,
                    pos.stop_pct * 100, pos.pnl_multiplier)
        return True

    async def update(self, symbol: str, price: float) -> Optional[Position]:
        async with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return None
            before = (pos.best_price, pos.trailing_armed, pos.trailing_stop_price)
            apply_price_to_trailing(pos, price)
            if (pos.best_price, pos.trailing_armed, pos.trailing_stop_price) != before:
                self._save()
            return pos

    async def close(self, symbol: str, exit_price: float, reason: str) -> Optional[dict]:
        """Returns the closed-trade record, or None if already closed (e.g.
        the tick path and the poll path raced for the same exit)."""
        async with self._lock:
            pos = self.positions.pop(symbol, None)
            if pos is None:
                return None
            self._save()
        entry_filled = pos.entry_price * (1 + modeled_slippage_pct(pos.entry_price))
        exit_filled = exit_price * (1 - modeled_slippage_pct(exit_price))
        now = datetime.now(IST)
        record = {
            "underlying_symbol": symbol, "trading_symbol": pos.trading_symbol, "option_type": pos.resolved_option_type,
            **self.labels, "opened_at": pos.opened_at.isoformat(), "closed_at": now.isoformat(),
            "hold_minutes": round((now - pos.opened_at).total_seconds() / 60, 1),
            "entry_price": pos.entry_price, "exit_price": exit_price, "best_price": pos.best_price,
            "exit_reason": reason, "stop_pct": pos.stop_pct, "trailing_armed": pos.trailing_armed,
            "pnl_multiplier": pos.pnl_multiplier,
            "pnl_raw": round((exit_price - pos.entry_price) * pos.pnl_multiplier, 2),
            "pnl_modeled": round((exit_filled - entry_filled) * pos.pnl_multiplier, 2),
        }
        await asyncio.get_running_loop().run_in_executor(None, append_jsonl, self.log_name, record)
        logger.info("[%s] PAPER position CLOSED: %s (%s) reason=%s entry=%.2f exit=%.2f pnl_raw=%.2f pnl_modeled=%.2f",
                    self.labels["strategy"], symbol, pos.trading_symbol, reason, pos.entry_price, exit_price,
                    record["pnl_raw"], record["pnl_modeled"])
        return record

    def snapshot(self) -> dict:
        return {"open_positions": [_to_json(p) for p in self.positions.values()]}


HOLD_LONG_PAPER_TRADES_LOG_NAME = "bollinger_hold_long_paper_trades"

# The deployed Bollinger strategy's paper book.
paper_book = PaperBook(config.PAPER_POSITIONS_FILE, PAPER_TRADES_LOG_NAME, {
    "strategy": "Bollinger", "entry_mode": config.ENTRY_MODE, "sides": config.SIDES,
    "exit_mode": config.EXIT_MODE, "roll_days": config.ROLL_EXPIRY_WITHIN_TRADING_DAYS})

# The separate Bollinger Hold-Long paper strategy's book (see config.HOLD_LONG_*).
hold_long_paper_book = PaperBook(config.HOLD_LONG_PAPER_POSITIONS_FILE, HOLD_LONG_PAPER_TRADES_LOG_NAME, {
    "strategy": "BollingerHoldLong", "entry_mode": "resting", "sides": "long",
    "exit_mode": "hold_to_close", "roll_days": config.HOLD_LONG_ROLL_EXPIRY_WITHIN_TRADING_DAYS})
