"""
Paper-trading twin of the live options strategy, for evaluating a NEW
Chartink scan before trusting it with real money - PAPER ONLY.

SAFETY INVARIANT: this module must NEVER call `dhan_wrapper.place_market_order`
(the only real order-placement entry point in Options/dhan_client.py) or
`dhan_wrapper.client.order_placement` directly, and does not import either.
Every Dhan/Tradehull call here is read-only: `get_atm_option`
(ATM_Strike_Selection - a lookup, not an order), `get_day_change_pct` (via
rank_and_pick_top_stocks), `get_option_ltp`/`get_cached_option_ltp`,
`refresh_supertrend_signal`. A "PAPER ENTRY"/"PAPER EXIT" log line and an
on-disk trade record are the only side effects.

POST /chartink/webhook-papertrade - point a second, separate Chartink scan
at this endpoint (bullish/CE only, matching /chartink/webhook's convention -
see NOTES.md's design-decision entry for why PE wasn't built here too).
Reuses the exact same ranking (rank_and_pick_top_stocks), ATM resolution,
and exit logic (Position / _exit_reason_for / _supertrend_signal_for, all
imported from trading_engine.py - not reimplemented) as the real strategy,
so the only variable being tested is the new scan's stock-picking quality,
not a different set of exit rules. config.PAPERTRADE_TOP_N_STOCKS /
PAPERTRADE_MAX_POSITIONS gate ranking/capacity independently of the real
strategy's own MAX_LIVE_POSITIONS_CE, so a burst of alerts here can't
starve real-money capacity or vice versa - entirely separate position pool.

Unlike CopperOptions' paper engine (see NOTES.md bug #26), the open
position here IS persisted to disk on every change, not just completed
trades - a restart won't silently lose an in-flight paper position's
outcome.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel, field_validator

from trade_history import record_webhook_alert

from . import config
from .dhan_client import dhan_wrapper
from .position_store import Position
from .trading_engine import (
    _capture_supertrend_entry_candle,
    _exit_reason_for,
    _get_ltp,
    _parse_hhmm_today,
    _supertrend_signal_for,
    is_past_square_off_time,
    rank_and_pick_top_stocks,
)

PAPER_TRADING_ONLY = True  # hard safety invariant, not just a label - see module docstring

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("paper_webhook")

router = APIRouter()

OPEN_STATE_PATH = "papertrade_open.json"
COMPLETED_LOG_PATH = "papertrade_completed.jsonl"


class ChartinkWebhookPayload(BaseModel):
    stocks: str
    trigger_prices: str
    triggered_at: str
    scan_name: str
    scan_url: str
    alert_name: str
    webhook_url: Optional[str] = None

    @field_validator("stocks")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("stocks must not be empty")
        return v

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]


class PaperStore:
    """Mirrors position_store.py's reservation/capacity pattern, but for
    an entirely separate paper-only position pool - a symbol open here has
    no effect on the real strategy's capacity or dedup, and vice versa."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.open_positions: dict[str, Position] = {}
        self.completed: list[dict] = []
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            with open(OPEN_STATE_PATH) as f:
                raw = json.load(f)
            for symbol, fields in raw.items():
                for dt_field in ("opened_at", "closed_at", "supertrend_entry_candle_start", "next_exit_retry_at"):
                    if fields.get(dt_field):
                        fields[dt_field] = datetime.fromisoformat(fields[dt_field])
                self.open_positions[symbol] = Position(**fields)
            if self.open_positions:
                logger.info("Recovered %d open paper position(s) from disk: %s",
                             len(self.open_positions), list(self.open_positions))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Could not load persisted paper-trade open state - starting fresh.")

        try:
            with open(COMPLETED_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.completed.append(json.loads(line))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Could not load paper-trade completed log - starting fresh in memory.")

    def _persist_open(self) -> None:
        try:
            raw = {sym: vars(pos) for sym, pos in self.open_positions.items()}
            with open(OPEN_STATE_PATH, "w") as f:
                json.dump(raw, f, default=str)
        except Exception:  # noqa: BLE001
            logger.exception("Could not persist open paper-trade state to disk.")

    async def reserve_and_open(self, position: Position) -> None:
        async with self._lock:
            self.open_positions[position.underlying_symbol] = position
            self._persist_open()
            logger.info(
                "PAPER ENTRY (no real order placed) %s %s @ %.2f target=%.2f sl=%.2f",
                position.option_type, position.option_trading_symbol,
                position.entry_price, position.target_price, position.hard_stop_loss,
            )

    async def has_capacity(self) -> bool:
        async with self._lock:
            return len(self.open_positions) < config.PAPERTRADE_MAX_POSITIONS

    async def is_reserved(self, symbol: str) -> bool:
        async with self._lock:
            return symbol in self.open_positions

    async def close(self, symbol: str, exit_price: float, reason: str) -> None:
        async with self._lock:
            pos = self.open_positions.pop(symbol, None)
            if pos is None:
                return
            self._persist_open()
            pnl = (exit_price - pos.entry_price) * pos.quantity
            trade = {
                "date": pos.opened_at.date().isoformat(), "underlying_symbol": symbol,
                "option_type": pos.option_type, "trading_symbol": pos.option_trading_symbol,
                "opened_at": pos.opened_at.isoformat(), "entry_price": pos.entry_price,
                "closed_at": datetime.now(IST).isoformat(), "exit_price": exit_price,
                "exit_reason": reason, "quantity": pos.quantity, "pnl": pnl,
            }
            self.completed.append(trade)
            try:
                with open(COMPLETED_LOG_PATH, "a") as f:
                    f.write(json.dumps(trade) + "\n")
            except Exception:  # noqa: BLE001
                logger.exception("Could not persist completed paper trade to disk.")
            logger.info(
                "PAPER EXIT (no real order placed) %s %s reason=%s pnl=%.2f",
                pos.option_type, pos.option_trading_symbol, reason, pnl,
            )

    def snapshot(self, limit: int = 50) -> dict:
        recent = list(reversed(self.completed))[:limit]
        gross_total = sum(t["pnl"] for t in self.completed)
        wins = sum(1 for t in self.completed if t["pnl"] > 0)
        return {
            "paper_trading_only": PAPER_TRADING_ONLY,
            "open_positions": {sym: vars(pos) for sym, pos in self.open_positions.items()},
            "total_completed_trades": len(self.completed),
            "pnl_total": gross_total,
            "win_rate": (wins / len(self.completed)) if self.completed else None,
            "recent_trades": recent,
        }


paper_store = PaperStore()


@router.post("/chartink/webhook-papertrade")
async def chartink_webhook_papertrade(payload: ChartinkWebhookPayload):
    """Bullish scan, paper-only - see module docstring. Same shape/response
    style as /chartink/webhook, but never places a real order."""
    stocks = payload.stock_list()

    def _log_alert(status: str, reason: Optional[str] = None) -> None:
        """Fire-and-forget - see trade_history.py's own docstring. Lower
        stakes here than the real webhooks (this endpoint never places a
        real order), but same discipline for consistency."""
        asyncio.create_task(record_webhook_alert(
            "Options-PaperTrade", payload.scan_name, payload.alert_name, stocks, status, reason,
        ))

    if is_past_square_off_time():
        logger.info("Ignoring paper-trade alert - past today's %s square-off time.", config.SQUARE_OFF_TIME)
        _log_alert("ignored", "past_square_off_time")
        return {"status": "ignored", "reason": "past_square_off_time"}

    logger.info("Paper-trade webhook received: scan=%s alert=%s stocks=%s",
                payload.scan_name, payload.alert_name, stocks)

    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(
        None, rank_and_pick_top_stocks, stocks, config.PAPERTRADE_TOP_N_STOCKS, True
    )
    if not ranked:
        _log_alert("no_action", "could_not_rank_any_stock")
        return {"status": "no_action", "reason": "could_not_rank_any_stock"}

    results = []
    for symbol, pct_change in ranked:
        if await paper_store.is_reserved(symbol):
            results.append({"symbol": symbol, "status": "skipped", "reason": "already_open"})
            continue
        if not await paper_store.has_capacity():
            results.append({"symbol": symbol, "status": "skipped", "reason": "capacity_full"})
            continue
        try:
            atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "CE")
            entry_price = await _get_ltp(atm.trading_symbol)
            if not entry_price:
                entry_price = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
            quantity = atm.lot_size * config.QUANTITY_LOTS
            entry_candle_start = await _capture_supertrend_entry_candle(loop, symbol)

            position = Position(
                underlying_symbol=symbol,
                option_trading_symbol=atm.trading_symbol,
                option_type="CE",
                quantity=quantity,
                lot_size=atm.lot_size,
                entry_price=entry_price,
                highest_price=entry_price,
                target_price=entry_price * (1 + config.TARGET_PCT),
                hard_stop_loss=entry_price * (1 - config.STOP_LOSS_PCT),
                order_id="",
                product_type="PAPER",
                supertrend_entry_candle_start=entry_candle_start,
            )
            await paper_store.reserve_and_open(position)
            results.append({"symbol": symbol, "status": "paper_entered", "entry_price": entry_price})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Paper entry failed for %s", symbol)
            results.append({"symbol": symbol, "status": "error", "reason": str(exc)})

    _log_alert("processed")
    return {"status": "processed", "ranked_by_day_change_pct": ranked, "entries": results}


@router.get("/papertrade/trades")
async def get_papertrade_trades():
    return paper_store.snapshot()


async def poll_loop() -> None:
    """Mirrors trading_engine.monitor_loop's exit logic exactly (reusing
    _exit_reason_for/_supertrend_signal_for directly), applied to the
    separate paper position pool. Also applies the same one-time EOD
    square-off as the real strategy."""
    logger.info("Paper-trade poll loop started (PAPER ONLY - no real orders will be placed).")
    squared_off_today_for: set = set()
    while True:
        try:
            loop = asyncio.get_running_loop()
            now = datetime.now(IST)
            square_off_at = _parse_hhmm_today(config.SQUARE_OFF_TIME)
            today_key = now.date()

            positions = dict(paper_store.open_positions)

            if now >= square_off_at and today_key not in squared_off_today_for:
                for symbol, position in positions.items():
                    try:
                        ltp = await _get_ltp(position.option_trading_symbol)
                        if not ltp:
                            ltp = position.highest_price
                        await paper_store.close(symbol, ltp, "EOD_SQUARE_OFF")
                    except Exception:  # noqa: BLE001
                        logger.exception("Paper EOD square-off failed for %s", symbol)
                squared_off_today_for.add(today_key)
            elif now < square_off_at:
                for symbol, position in positions.items():
                    try:
                        ltp = await _get_ltp(position.option_trading_symbol)
                        if ltp is None:
                            continue
                        if ltp > position.highest_price:
                            position.highest_price = ltp
                        await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, symbol)
                        supertrend_against = _supertrend_signal_for(position)
                        reason = _exit_reason_for(position, ltp, supertrend_against)
                        if reason:
                            await paper_store.close(symbol, ltp, reason)
                    except Exception:  # noqa: BLE001
                        logger.exception("Paper poll tick failed for %s", symbol)
        except Exception:  # noqa: BLE001
            logger.exception("Paper-trade poll loop iteration failed - continuing.")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
