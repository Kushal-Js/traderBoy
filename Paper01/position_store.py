"""
Paper01's own position pool - entirely independent of Options'
position_store.py (a symbol open here has zero effect on Options' real
capacity/dedup, and vice versa, per the plan's explicit design decision:
the cross-package "same stock already being traded elsewhere" lock
doesn't apply to a paper strategy).

Reuses Options.position_store.Position directly rather than redefining
an equivalent dataclass - its current_trailing_sl property already reads
Options.config's dynamic/trailing-SL settings, so a Paper01 Position gets
byte-identical stop-loss math for free, with zero duplication. Every
Paper01 Position is built with order_id="", product_type="PAPER",
stop_loss_order_id=None (no real broker order ever exists for it).

Persists open positions to disk on every change (same approach as
Options/paper_webhook.py's PaperStore) so a mid-day restart doesn't
silently lose an in-flight paper position's outcome - only completed
trades are the durable source of truth for P&L, but losing track of what
was open is still worth avoiding.

Completed trades are appended to Paper01's OWN dated JSONL log
(config.PAPER_TRADES_LOG_NAME) via trade_history.append_jsonl/dated_path
- deliberately never trade_history.record_closed_trade/REAL_TRADES_NAME,
so paper trades can never be mixed into the real ledger.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Dict, List, Optional

from trade_history import append_jsonl, dated_path, read_all_jsonl

from . import config
from Options.position_store import Position

logger = logging.getLogger("paper01_position_store")


def _cap_for(option_type: str) -> int:
    return config.MAX_LIVE_POSITIONS_CE if option_type == "CE" else config.MAX_LIVE_POSITIONS_PE


class PaperPositionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.live_positions: Dict[str, Position] = {}   # keyed by underlying_symbol
        self.reserved_symbols: Dict[str, str] = {}       # underlying_symbol -> option_type
        self.closed_positions_today: List[Position] = []
        self._trading_day: date = date.today()
        self._load_open_from_disk()

    def _load_open_from_disk(self) -> None:
        try:
            with open(config.OPEN_STATE_PATH) as f:
                raw = json.load(f)
            for symbol, fields in raw.items():
                for dt_field in ("opened_at", "closed_at", "supertrend_entry_candle_start", "next_exit_retry_at"):
                    if fields.get(dt_field):
                        fields[dt_field] = datetime.fromisoformat(fields[dt_field])
                pos = Position(**fields)
                self.live_positions[symbol] = pos
                self.reserved_symbols[symbol] = pos.option_type
            if self.live_positions:
                logger.info("Recovered %d open Paper01 position(s) from disk: %s",
                             len(self.live_positions), list(self.live_positions))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Could not load persisted Paper01 open state - starting fresh.")

    def _persist_open(self) -> None:
        try:
            raw = {sym: asdict(pos) for sym, pos in self.live_positions.items()}
            with open(config.OPEN_STATE_PATH, "w") as f:
                json.dump(raw, f, default=str)
        except Exception:  # noqa: BLE001
            logger.exception("Could not persist Paper01 open state to disk.")

    async def maybe_reset_for_new_day(self) -> None:
        """Mirrors Options.position_store's own daily reset, always in the
        ENABLE_SQUARE_OFF=True mode (paper positions are always squared
        off at end of day by paper_monitor_loop - see its own docstring -
        so there's never a real-money reason to carry one across a day
        boundary the way Options' NRML mode can)."""
        async with self._lock:
            today = date.today()
            if today != self._trading_day:
                logger.info("New trading day detected (%s) - resetting Paper01 daily state.", today)
                self.closed_positions_today.clear()
                self._trading_day = today

    async def reserve_symbol(self, underlying_symbol: str, option_type: str) -> bool:
        """Same atomic dedup+capacity pattern as Options.position_store.reserve_symbol,
        gated on Paper01's own config.MAX_LIVE_POSITIONS_CE/_PE."""
        async with self._lock:
            if underlying_symbol in self.reserved_symbols or underlying_symbol in self.live_positions:
                return False
            current = sum(1 for ot in self.reserved_symbols.values() if ot == option_type)
            if current >= _cap_for(option_type):
                return False
            self.reserved_symbols[underlying_symbol] = option_type
            return True

    async def release_symbol(self, underlying_symbol: str) -> None:
        async with self._lock:
            if underlying_symbol not in self.live_positions:
                self.reserved_symbols.pop(underlying_symbol, None)

    async def remaining_capacity(self, option_type: str) -> int:
        async with self._lock:
            current = sum(1 for ot in self.reserved_symbols.values() if ot == option_type)
            return max(0, _cap_for(option_type) - current)

    async def add_position(self, pos: Position) -> None:
        async with self._lock:
            self.live_positions[pos.underlying_symbol] = pos
            self.reserved_symbols[pos.underlying_symbol] = pos.option_type
            self._persist_open()
            append_jsonl(config.PAPER_OPENED_LOG_NAME, {
                "underlying_symbol": pos.underlying_symbol, "option_type": pos.option_type,
                "option_trading_symbol": pos.option_trading_symbol, "opened_at": pos.opened_at.isoformat(),
            })
            logger.info(
                "PAPER ENTRY (no real order placed): %s %s (%s) entry=%.2f target=%.2f sl=%.2f qty=%s",
                pos.option_type, pos.underlying_symbol, pos.option_trading_symbol,
                pos.entry_price, pos.target_price, pos.hard_stop_loss, pos.quantity,
            )

    async def update_highest_price(self, underlying_symbol: str, current_price: float) -> None:
        async with self._lock:
            pos = self.live_positions.get(underlying_symbol)
            if pos and current_price > pos.highest_price:
                pos.highest_price = current_price

    async def close_position(self, underlying_symbol: str, exit_price: float, reason: str) -> Optional[Position]:
        async with self._lock:
            pos = self.live_positions.pop(underlying_symbol, None)
            if pos is None:
                return None
            pos.status = "CLOSED"
            pos.exit_reason = reason
            pos.exit_price = exit_price
            pos.closed_at = datetime.now()
            self.closed_positions_today.append(pos)
            self.reserved_symbols.pop(underlying_symbol, None)
            self._persist_open()
            pnl = (exit_price - pos.entry_price) * pos.quantity
            append_jsonl(config.PAPER_TRADES_LOG_NAME, {
                "underlying_symbol": pos.underlying_symbol, "option_type": pos.option_type,
                "option_trading_symbol": pos.option_trading_symbol, "quantity": pos.quantity,
                "entry_price": pos.entry_price, "opened_at": pos.opened_at.isoformat(),
                "exit_price": exit_price, "exit_reason": reason,
                "closed_at": pos.closed_at.isoformat(), "pnl": pnl,
            })
            logger.info(
                "PAPER EXIT (no real order placed): %s %s (%s) reason=%s exit=%.2f pnl=%.2f",
                pos.option_type, pos.underlying_symbol, pos.option_trading_symbol, reason, exit_price, pnl,
            )
            return pos

    async def count_opened_today(self, underlying_symbol: str) -> int:
        """Mirrors trade_history.count_opened_today's exact semantics
        (today's dated file only, fail-open to 0), pointed at Paper01's
        own PAPER_OPENED_LOG_NAME instead of the real OPENED_POSITIONS_NAME -
        that real function is hardcoded to the real log name, not
        parameterizable, so this small local copy is the only option."""
        path = dated_path(config.PAPER_OPENED_LOG_NAME)
        if not path.exists():
            return 0
        count = 0
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("underlying_symbol") == underlying_symbol:
                        count += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not read today's %s log for %s - treating as 0 (fail open).",
                              config.PAPER_OPENED_LOG_NAME, underlying_symbol)
            return 0
        return count

    async def loss_exit_count_today(self, underlying_symbol: str, exit_reasons: tuple) -> int:
        """Mirrors trade_history.loss_exit_count_today's exact semantics,
        pointed at Paper01's own PAPER_TRADES_LOG_NAME - same fail-open-to-0
        rationale as count_opened_today above."""
        path = dated_path(config.PAPER_TRADES_LOG_NAME)
        if not path.exists():
            return 0
        count = 0
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (record.get("underlying_symbol") == underlying_symbol
                            and record.get("exit_reason") in exit_reasons):
                        count += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not read today's %s log for %s - treating as 0 (fail open).",
                              config.PAPER_TRADES_LOG_NAME, underlying_symbol)
            return 0
        return count

    async def snapshot_open(self) -> dict:
        async with self._lock:
            return {
                "live_positions": [dict(asdict(p), current_trailing_sl=p.current_trailing_sl)
                                    for p in self.live_positions.values()],
                "reserved_symbols_by_type": dict(self.reserved_symbols),
            }

    def all_completed_trades(self) -> list[dict]:
        """Full history across every day - reads every dated Paper01
        trades log file, not just today's (see trade_history.read_all_jsonl)."""
        return read_all_jsonl(config.PAPER_TRADES_LOG_NAME)


paper_position_store = PaperPositionStore()
