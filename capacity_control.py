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
for) - Options/Luxury keep their own static MAX_CONCURRENT_TRADES-
equivalent caps untouched, out of scope for this change.

**`.env` sync, added 27 Sep 2026** - same fix, same reasoning, as
paper_mode_control.py's own (see that module's docstring for the real
incident that motivated it, and for why the override is KEPT rather
than dropped after a successful sync - dropping it would make an
already-running process silently fall back to whatever .env said at
ITS OWN startup, which is wrong the instant a requested value differs
from that; rewriting the .env FILE on disk does not change what the
already-imported config module's cached value is for THIS process).
Standing user instruction from that fix: whenever an explicit
configuration change is requested, it must be durably written to BOTH
the runtime override AND .env, so a restart can never silently revert
an explicit ask - set_max_concurrent_trades now does both, every time.
If .env can't be written, the override alone still keeps the in-process
value correct (logged clearly) even though restart-survival doesn't.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("capacity_control")

STRATEGIES = ("Swing", "Bollinger", "SuperBollinger")

OVERRIDE_FILE = Path("data/capacity_overrides.json")
ENV_FILE = Path(".env")

_ENV_VAR_NAMES = {
    "Swing": "SWING_MAX_CONCURRENT_TRADES",
    "Bollinger": "BOLLINGER_MAX_CONCURRENT_TRADES",
    "SuperBollinger": "SUPER_BOLLINGER_MAX_CONCURRENT_TRADES",  # added 30 Sep 2026
}

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
    if strategy == "SuperBollinger":
        from SuperBollinger import settings as cfg
        return int(cfg.MAX_CONCURRENT_TRADES_DEFAULT)
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


def _sync_env_file(strategy: str, value: int) -> bool:
    """Rewrites `strategy`'s own MAX_CONCURRENT_TRADES line in ENV_FILE
    to match `value` - or appends it if the line doesn't exist yet.
    Returns True on success. Best-effort by design, identical contract
    to paper_mode_control._sync_env_file: a failure here must never
    block the runtime change itself, which has already taken effect via
    the in-memory override regardless - only the "survives a restart
    without needing the override" guarantee depends on it."""
    var_name = _ENV_VAR_NAMES.get(strategy)
    if var_name is None:
        return False
    try:
        if not ENV_FILE.exists():
            logger.warning(
                "%s: %s not found (cwd=%s) - the runtime override still fully applies for "
                "this process, but .env's own default was NOT updated to match, so a future "
                "restart would revert to whatever .env currently says",
                strategy, ENV_FILE, Path.cwd(),
            )
            return False
        lines = ENV_FILE.read_text().splitlines(keepends=True)
        pattern = re.compile(rf"^{re.escape(var_name)}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{var_name}={value}\n"
                ENV_FILE.write_text("".join(lines))
                logger.info("%s: .env's own %s rewritten to %d", strategy, var_name, value)
                return True
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{var_name}={value}\n")
        ENV_FILE.write_text("".join(lines))
        logger.info("%s: .env had no %s line - appended %s=%d", strategy, var_name, var_name, value)
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not update .env's own %s - the runtime override still fully applies "
            "for this process, but a restart before this is fixed manually would revert to "
            "whatever .env currently says",
            strategy, var_name,
        )
        return False


async def set_max_concurrent_trades(strategy: str, value: int) -> None:
    if value < 0:
        raise ValueError("max concurrent trades cannot be negative")
    _load_overrides()
    async with _lock:
        _overrides[strategy] = value
        OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_FILE.write_text(json.dumps(_overrides, indent=2))
        env_synced = _sync_env_file(strategy, value)
    logger.info(
        "%s: max concurrent trades set to %d (override persisted to %s AND %s)", strategy, value, OVERRIDE_FILE,
        ".env's own default updated to match" if env_synced
        else ".env NOT updated - see warning above, a restart before this is fixed would revert to .env's old default",
    )
