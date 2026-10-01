"""SwingMomentum endpoints (paper only) - see SwingMomentum/engine.py."""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from fastapi import APIRouter

from Swing import config as swing_config
from trade_history import dated_path
from . import engine

router = APIRouter()


@router.get("/swing-momentum/paper-trades")
async def get_paper_trades(day: Optional[str] = None):
    """Closed paper trades of a day (default today) with totals, plus the open positions."""
    d = date.fromisoformat(day) if day else engine.swing_te._now_ist().date()
    path = dated_path(engine.TRADES_LOG, d)
    trades = []
    if path.exists():
        for line in path.read_text().splitlines():
            if "{" in line:
                try:
                    trades.append(json.loads(line[line.index("{"):]))
                except ValueError:
                    continue
    raw = sum(t.get("pnl") or 0 for t in trades)
    mod = sum(t.get("pnl_modeled") or 0 for t in trades)
    return {"strategy": engine.STRATEGY, "mode": "paper", "day": d.isoformat(), "count": len(trades),
            "wins": sum(1 for t in trades if (t.get("pnl_modeled") or 0) > 0), "total_pnl_raw": round(raw, 2),
            "total_pnl_modeled": round(mod, 2), "trades": trades, "open_positions": await engine.snapshot()}


@router.get("/swing-momentum/status")
async def get_status():
    return {"strategy": engine.STRATEGY, "mode": "paper (no order path exists)", "enabled": engine.ENABLED,
            "max_positions": engine.MAX_POSITIONS, "open_positions": await engine.snapshot(),
            "same_side_blocked": {s: v for s, v in engine._consumed.items() if v is not None},
            "regime_rule": {"momentum": f"2h ER >= {swing_config.REGIME_ER_MIN} and today's ER >= "
                                        f"{swing_config.REGIME_TODAY_ER_MIN}",
                            "er_bars_5min": swing_config.REGIME_ER_BARS},
            "logs": {"trades": f"history/<date>_{engine.TRADES_LOG}.log", "events": f"history/<date>_{engine.EVENTS_LOG}.log"}}
