"""
Runtime paper-mode ON/OFF switch, shared across every strategy that has
its own paper-mode kill switch (Options/Futures/Luxury/Swing) - added 24
Sep 2026, user request: "turn paper trading on or off... without
deployment just by calling an endpoint", extended same day to cover
Swing too.

Each strategy already had its OWN static .env-configured flag (Options/
Futures/Luxury's BREAKOUT_PAPER_MODE_ENABLED, added 22 Sep 2026; Swing's
PAPER_MODE_ENABLED, added 23 Sep 2026) - read once at process startup,
requiring a redeploy+restart to change. This module makes that a runtime
override instead: `is_paper_mode_enabled(strategy)` checks an in-memory
override first, falling back to that strategy's own .env default only
when no override has ever been set. An override, once set via
`set_paper_mode` (called from POST /paper-mode in main.py), is persisted
to OVERRIDE_FILE (gitignored, same data/ dir convention as
Swing/watchlist.py's own runtime-editable file) so it SURVIVES a restart
- including the automatic 08:00 IST morning-refresh restart - instead of
silently reverting to whatever .env says.

Deliberately generic, not living inside breakout_paper_engine.py (which
is genuinely Options/Futures/Luxury-specific - its own STRATEGY dispatch
table, its own paper-position bookkeeping) or swing_paper_engine.py
(Swing's own, structurally different paper engine) - this module knows
nothing about EITHER engine's internals, only "given a strategy name,
what's its current paper-mode state." Each strategy's own dispatch point
still decides what "paper mode" actually means for it:
  - Options/option_main.py, Futures/futures_main.py, Luxury/luxury_main.py
    (_resolve_entry): reroutes to breakout_paper_engine.process_paper_entry
    instead of that package's own trading_engine._process_one_entry.
  - Swing/trading_engine.py (_monitor_tick): reroutes to
    swing_paper_engine.process_paper_entry instead of
    enter_position_for_stock - ORed with Swing's own separate, narrower
    INDEX_SYMBOLS-only INDEX_PAPER_MODE_ENABLED flag, which this module
    does NOT touch (out of scope - a different, more targeted switch,
    not "real trading on/off for the whole strategy").

Same "replaces real trading, doesn't add a shadow copy" semantic in
every case, and never touches an already-open position - flipping a
strategy INTO paper mode leaves its current real position(s) to be
managed for real through to their own close; flipping OUT of paper mode
leaves any currently-open PAPER position simulated to its own close
too. Only entries from the toggle point on are affected.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger("paper_mode_control")

STRATEGIES = ("Options", "Futures", "Luxury", "Swing")

OVERRIDE_FILE = Path("data/paper_mode_overrides.json")

_overrides: dict[str, bool] = {}
_overrides_loaded = False
_lock = asyncio.Lock()


def _env_default(strategy: str) -> bool:
    """The .env-configured static default for `strategy`, read fresh each
    time (not cached) so nothing here goes stale relative to that
    package's own config module - deferred imports (function-local),
    matching breakout_paper_engine._build_hooks' own reasoning, since
    this module is imported very early (main.py, before every package's
    own *_main.py in some import orders) and must never risk a circular
    import at module load time."""
    if strategy == "Options":
        from Options import config as cfg
        return bool(cfg.BREAKOUT_PAPER_MODE_ENABLED)
    if strategy == "Futures":
        from Futures import config as cfg
        return bool(cfg.BREAKOUT_PAPER_MODE_ENABLED)
    if strategy == "Luxury":
        from Luxury import config as cfg
        return bool(cfg.BREAKOUT_PAPER_MODE_ENABLED)
    if strategy == "Swing":
        from Swing import config as cfg
        return bool(cfg.PAPER_MODE_ENABLED)
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
                "Could not read %s - starting with no runtime paper-mode overrides "
                "(every strategy falls back to its own .env default)",
                OVERRIDE_FILE,
            )


def is_paper_mode_enabled(strategy: str) -> bool:
    """The one thing each strategy's own real-vs-paper dispatch point
    should call instead of reading its config module's static flag
    directly - everything else about paper-mode dispatch is unchanged."""
    _load_overrides()
    if strategy in _overrides:
        return _overrides[strategy]
    return _env_default(strategy)


def paper_mode_source(strategy: str) -> str:
    _load_overrides()
    return "runtime_override" if strategy in _overrides else "env_default"


async def set_paper_mode(strategy: str, enabled: bool) -> None:
    _load_overrides()
    async with _lock:
        _overrides[strategy] = enabled
        OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_FILE.write_text(json.dumps(_overrides, indent=2))
    logger.info("%s: paper mode set to %s via runtime override (persisted to %s)",
                strategy, enabled, OVERRIDE_FILE)
