"""
Runtime paper-mode ON/OFF switch, shared across every strategy that has
its own paper-mode kill switch (Options/Luxury/Swing/Bollinger) - added 24
Sep 2026, user request: "turn paper trading on or off... without
deployment just by calling an endpoint", extended same day to cover
Swing too.

Each strategy already had its OWN static .env-configured flag (Options/
Luxury's BREAKOUT_PAPER_MODE_ENABLED, added 22 Sep 2026; Swing's
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
  - Options/option_main.py, Luxury/luxury_main.py (_resolve_entry):
    reroutes to breakout_paper_engine.process_paper_entry
    instead of that package's own trading_engine._process_one_entry.
  - Swing/trading_engine.py (_monitor_tick): reroutes to
    swing_paper_engine.process_paper_entry instead of
    enter_position_for_stock. NIFTY/BANKNIFTY specifically read the
    "SwingIndex" pseudo-strategy below instead of "Swing" - fully
    independent of the rest of the watchlist's paper/real state (added
    28 Sep 2026 - previously a separate, narrower, restart-only
    INDEX_PAPER_MODE_ENABLED flag ORed with the global one; folded into
    this module so it gets the same runtime-toggle + .env-sync guarantee
    every other strategy here already has). MCX symbols (COPPER/
    NATURALGAS) likewise read their own "SwingMCX" pseudo-strategy
    (added 29 Sep 2026, same reasoning - previously the restart-only
    MCX_PAPER_MODE_ENABLED flag).

Same "replaces real trading, doesn't add a shadow copy" semantic in
every case, and never touches an already-open position - flipping a
strategy INTO paper mode leaves its current real position(s) to be
managed for real through to their own close; flipping OUT of paper mode
leaves any currently-open PAPER position simulated to its own close
too. Only entries from the toggle point on are affected.

**`.env` sync, added 27 Sep 2026 (real incident):** the runtime-override
design above has a real gap - an override can silently diverge from
`.env`'s own default for days with nothing surfacing the disagreement,
which is exactly what happened to Swing (its `.env` said paper-mode-on,
but an undated runtime override had it real-trading instead, discovered
by chance while investigating an unrelated finding). `set_paper_mode`
now also rewrites `.env`'s own line for that strategy to match, so a
FUTURE restart no longer depends on the override file at all to come up
correct. The override itself is deliberately KEPT, not dropped, after a
successful sync - `_env_default` reads each package's config module,
which cached `os.getenv(...)` once at process START, so rewriting the
.env file on disk does NOT change what `_env_default` returns for the
CURRENT process; dropping the override would make an already-running
process silently fall back to whatever .env said at ITS OWN startup,
which is wrong the instant a requested value differs from that (caught
by this module's own test suite, 27 Sep 2026, before this ever shipped
with the bug live: the first version of this fix dropped the override
and only "worked" in the one real case tried because the requested
value happened to already match .env). `GET /paper-mode` correctly
keeps reporting `source: "runtime_override"` for the life of the
process after any change - that's accurate, not a regression to the
original gap, since .env and the override are now written together on
every change and can never diverge again."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("paper_mode_control")

STRATEGIES = ("Options", "Luxury", "Swing", "Bollinger", "SwingIndex", "SwingMCX", "SuperBollinger", "BollingerIndex")

OVERRIDE_FILE = Path("data/paper_mode_overrides.json")
ENV_FILE = Path(".env")

_ENV_VAR_NAMES = {
    "Options": "OPTIONS_BREAKOUT_PAPER_MODE_ENABLED",
    "Luxury": "LUXURY_BREAKOUT_PAPER_MODE_ENABLED",
    "Swing": "SWING_PAPER_MODE_ENABLED",
    "Bollinger": "BOLLINGER_PAPER_MODE_ENABLED",
    # "SwingIndex" (added 28 Sep 2026, user request: "Make this Index
    # trading paper mode toggle also the one which won't require any
    # restart in future") - a SEPARATE pseudo-strategy scoped to just
    # NIFTY/BANKNIFTY within Swing, independent of the "Swing" entry
    # above (which governs the other 18 watchlist symbols). Reuses the
    # pre-existing SWING_INDEX_PAPER_MODE_ENABLED env var name rather
    # than introducing a new one - that flag already meant exactly "is
    # NIFTY/BANKNIFTY paper", just previously only as an ADD-on OR'd with
    # the global Swing flag and only readable via a restart. Folding it
    # into this module gives it the same runtime-toggle + .env-sync
    # guarantees every other strategy here already has - see Swing/
    # trading_engine.py's _should_paper_trade for where this is checked.
    "SwingIndex": "SWING_INDEX_PAPER_MODE_ENABLED",
    # "SwingMCX" (added 29 Sep 2026, user request: "add the MCX runtime
    # toggle also and keep it on paper only for MCX") - Swing's MCX
    # commodities (COPPER/NATURALGAS), same pattern as SwingIndex: reuses
    # the pre-existing SWING_MCX_PAPER_MODE_ENABLED env var, previously
    # restart-only, now the startup default for this runtime toggle.
    "SwingMCX": "SWING_MCX_PAPER_MODE_ENABLED",
    # "SuperBollinger" (added 30 Sep 2026) - SuperBollinger/, its own
    # strategy with its own paper/real switch.
    "SuperBollinger": "SUPER_BOLLINGER_PAPER_MODE_ENABLED",
    # "BollingerIndex" (added 30 Sep 2026) - Bollinger's NIFTY/BANKNIFTY
    # only, same split as SwingIndex; "Bollinger" above then covers every
    # other Bollinger symbol (the stocks).
    "BollingerIndex": "BOLLINGER_INDEX_PAPER_MODE_ENABLED",
}

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
    if strategy == "Luxury":
        from Luxury import config as cfg
        return bool(cfg.BREAKOUT_PAPER_MODE_ENABLED)
    if strategy == "Swing":
        from Swing import config as cfg
        return bool(cfg.PAPER_MODE_ENABLED)
    if strategy == "Bollinger":
        from Bollinger import config as cfg
        return bool(cfg.PAPER_MODE_ENABLED)
    if strategy == "SwingIndex":
        from Swing import config as cfg
        return bool(cfg.INDEX_PAPER_MODE_ENABLED)
    if strategy == "SwingMCX":
        from Swing import config as cfg
        return bool(cfg.MCX_PAPER_MODE_ENABLED)
    if strategy == "SuperBollinger":
        from SuperBollinger import settings as cfg
        return bool(cfg.PAPER_MODE_ENABLED_DEFAULT)
    if strategy == "BollingerIndex":
        from Bollinger import config as cfg
        return bool(cfg.INDEX_PAPER_MODE_ENABLED)
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


def _sync_env_file(strategy: str, enabled: bool) -> bool:
    """Rewrites `strategy`'s own paper-mode line in ENV_FILE to match
    `enabled` - or appends it if the line doesn't exist yet. Returns
    True on success. Best-effort by design: a failure here (e.g. no
    .env in this working directory) must never block the runtime change
    itself, which has already taken effect via the in-memory override
    regardless of whether this succeeds - only the "survives a restart
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
        new_value = "true" if enabled else "false"
        lines = ENV_FILE.read_text().splitlines(keepends=True)
        pattern = re.compile(rf"^{re.escape(var_name)}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{var_name}={new_value}\n"
                ENV_FILE.write_text("".join(lines))
                logger.info("%s: .env's own %s rewritten to %s", strategy, var_name, new_value)
                return True
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{var_name}={new_value}\n")
        ENV_FILE.write_text("".join(lines))
        logger.info("%s: .env had no %s line - appended %s=%s", strategy, var_name, var_name, new_value)
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not update .env's own %s - the runtime override still fully applies "
            "for this process, but a restart before this is fixed manually would revert to "
            "whatever .env currently says",
            strategy, var_name,
        )
        return False


async def set_paper_mode(strategy: str, enabled: bool) -> None:
    _load_overrides()
    async with _lock:
        _overrides[strategy] = enabled
        OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_FILE.write_text(json.dumps(_overrides, indent=2))
        env_synced = _sync_env_file(strategy, enabled)
    logger.info(
        "%s: paper mode set to %s (override persisted to %s AND %s)", strategy, enabled, OVERRIDE_FILE,
        ".env's own default updated to match" if env_synced
        else ".env NOT updated - see warning above, a restart before this is fixed would revert to .env's old default",
    )
