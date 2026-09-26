"""
Runtime MAX_CONCURRENT_TRADES override, shared by Swing and Bollinger -
added 26 Sep 2026, user request: "this max concurrent trade capacity is
a configurable item which should be able to change without a deployment
by simply updating it using an endpoint in the bot." Same pattern as
paper_mode_control.py (that module's own docstring explains the original
"static .env flag read once at startup" problem this solves) - deliberately
NOT merged into that module since capacity is an int, not a bool, and is
consulted from a different call site (position_store's capacity check,
not the paper/real dispatch point).

Each strategy already has its own static .env-configured
MAX_CONCURRENT_TRADES (Swing: SWING_MAX_CONCURRENT_TRADES, Bollinger:
BOLLINGER_MAX_CONCURRENT_TRADES), read once at import time.
get_max_concurrent_trades(strategy) checks an in-memory override first,
falling back to that strategy's own config default only if no override
has ever been set. An override, once set via set_max_concurrent_trades
(called from POST /capacity/max-concurrent-trades in main.py), is
persisted to OVERRIDE_FILE (gitignored, same data/ dir convention as
paper_mode_control.py's own override file) so it SURVIVES a restart,
including the automatic 08:00 IST morning-refresh restart.

Scope: only Swing and Bollinger (the two strategies this was requested
for) - Options/Futures/Luxury keep their own static MAX_CONCURRENT_TRADES-
equivalent caps untouched, out of scope for this change.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger("capacity_control")

STRATEGIES = ("Swing", "Bollinger")

OVERRIDE_FILE = Path("data/capacity_overrides.json")

_overrides: dict[str, int] = {}
_overrides_loaded = False
_lock = asyncio.Lock()


def _env_default(strategy: str) -> int:
    """The .env-configured static default for `strategy`, read fresh each
    time (not cached) so nothing here goes stale relative to that
    package's own config module - deferred imports (function-local),
    matching paper_mode_control._env_default's own reasoning, since this
    module is imported very early and must never risk a circular import
    at module load time."""
    if strategy == "Swing":
        from Swing import config as cfg
        return int(cfg.MAX_CONCURRENT_TRADES)
    if strategy == "Bollinger":
        from Bollinger import config as cfg
        return int(cfg.MAX_CONCURRENT_TRADES)
    raise ValueError(f"unknown strategy {strategy!r} - must be one of {STRATEGIES}")


def _load_overrides() -> None:
    global _overrides_loaded
    if _overrides_loaded:
        return
    _overrides_loaded = True
    if OVERRIDE_FILE.exists():
        try:
            _overrides.update(json.loads(OVERRIDE_FILE.read_text()))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not read %s - starting with no runtime capacity overrides "
                "(every strategy falls back to its own .env default)",
                OVERRIDE_FILE,
            )


def get_max_concurrent_trades(strategy: str) -> int:
    """The one thing each strategy's own position_store should call
    instead of reading config.MAX_CONCURRENT_TRADES directly."""
    _load_overrides()
    if strategy in _overrides:
        return _overrides[strategy]
    return _env_default(strategy)


def capacity_source(strategy: str) -> str:
    _load_overrides()
    return "runtime_override" if strategy in _overrides else "env_default"


async def set_max_concurrent_trades(strategy: str, value: int) -> None:
    if value < 0:
        raise ValueError("max concurrent trades cannot be negative")
    _load_overrides()
    async with _lock:
        _overrides[strategy] = value
        OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_FILE.write_text(json.dumps(_overrides, indent=2))
    logger.info("%s: max concurrent trades set to %d via runtime override (persisted to %s)",
                strategy, value, OVERRIDE_FILE)
