"""
Market chop gate (Unified Momentum, 1 Oct 2026). No NEW entries - engine A or B - while NIFTY's own efficiency
ratio over its last 24 CLOSED 5-min candles (2 h) is below settings.market_chop_gate_er (default 0.10; 0 = off).
ER = |close now - close 24 candles ago| / sum of the 24 candle-to-candle moves: near 0 = the index went nowhere
however much it moved. Backtest (research_unified_super_strategy_followup.py): a gate at 0.10-0.12 raised profit and
cut the drawdown; stricter gates cost profit monotonically.

The entry checks call is_open() on every tick, so it only reads a cached reading; refresh() (awaited by the monitor
loop) recomputes it in a worker thread at most every REFRESH_SECONDS from the same REST-base + websocket NIFTY series
Swing's signals read (Swing/signals._get_intraday_series - cached per instrument, no extra Dhan calls in normal
operation). No reading yet, or a failed one -> the gate is OPEN (never blocks for missing data).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from typing import Optional

from Options.dhan_client import IST, dhan_wrapper

from . import settings

logger = logging.getLogger("unified_momentum_market_gate")

REFRESH_SECONDS = 30
ER_BARS = 24
SESSION_FIRST_BAR, SESSION_END = dtime(9, 15), dtime(15, 30)
_state: dict = {"er": None, "at": 0.0, "candle": None, "error": None, "running": False}


def _read() -> tuple[Optional[float], Optional[str]]:
    """Blocking. NIFTY's 2 h efficiency ratio from closed 5-min candles -> (er, last candle start ISO)."""
    from Swing import signals
    from Swing.regime import efficiency_ratio
    sid, seg, inst = signals._underlying_reference("NIFTY")
    bars = signals._get_intraday_series("NIFTY", sid, seg, inst, 5, min_bars=ER_BARS + 1)
    ts, closes = bars.get("timestamp") or [], bars.get("close") or []
    cut = time.time() - 5 * 60
    # Closed bars of the NSE regular session only (2 Oct 2026): the index feed used to leave flat after-hours bars
    # (1 Oct: 33 NIFTY bars 15:30-18:10 at the closing price) that read as ER 0.0 = "choppy" - the backtest's ER
    # uses 09:15-15:25 bars only, continuous across days.
    keep = [i for i, t in enumerate(ts)
            if t <= cut and SESSION_FIRST_BAR <= datetime.fromtimestamp(t, IST).time() < SESSION_END]
    closes = [closes[i] for i in keep]
    if len(closes) < ER_BARS + 1:
        return None, None
    return efficiency_ratio(closes, ER_BARS), datetime.fromtimestamp(ts[keep[-1]], IST).isoformat()


async def refresh(force: bool = False) -> None:
    """Market hours only: after the close NIFTY's REST base is always "behind" and would be refetched every
    minute all night (the DH-904 overnight-polling incident class) - the last reading is kept instead."""
    if _state["running"] or (not force and time.monotonic() - _state["at"] < REFRESH_SECONDS):
        return
    if not force and not dhan_wrapper.is_market_open():
        return
    _state["running"] = True
    try:
        er, candle = await asyncio.get_running_loop().run_in_executor(None, _read)
        _state.update(er=er, candle=candle, error=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[UnifiedMomentum] market gate: could not read NIFTY's series - gate stays open")
        _state.update(er=None, error=str(exc))
    finally:
        _state["at"] = time.monotonic()
        _state["running"] = False


def is_open() -> bool:
    threshold = settings.get("market_chop_gate_er")
    er = _state["er"]
    return threshold <= 0 or er is None or er >= threshold


def snapshot() -> dict:
    return {"open": is_open(), "nifty_er_2h": None if _state["er"] is None else round(_state["er"], 3),
            "threshold": settings.get("market_chop_gate_er"), "last_candle": _state["candle"], "error": _state["error"]}
