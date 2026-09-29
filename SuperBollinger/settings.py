"""
Super Bollinger runtime settings (30 Sep 2026, user request: "Keep
configurations configurable and updating them or Paper trading flag on or
off should not require a deployment, run time changes should be allowed
without the need of bot restart").

Every value has a .env default (SUPER_BOLLINGER_*, read once at import) and
can be changed at runtime via POST /super-bollinger/config. A runtime change
is (a) applied immediately - every reader calls get() on each use, nothing
is cached - (b) persisted to OVERRIDE_FILE so it survives a restart, and
(c) written into .env as well, so .env never silently disagrees with what
the bot is actually running (same two-place rule as paper_mode_control.py /
capacity_control.py, see their docstrings for the 27 Sep incident behind it).

Paper/real and max-concurrent-trades are NOT stored here: they live in the
shared paper_mode_control ("SuperBollinger") and capacity_control
("SuperBollinger") modules like every other strategy's, so POST /paper-mode
and POST /capacity/max-concurrent-trades work for Super Bollinger too; the
config endpoint simply forwards those two keys to them.

Defaults = the best-performing rules from the 29 Sep 2026 backtest
(backtest_bollinger_hold_long_exit_variants.py, policy "G'"): buy the ATM CE
on a BULLISH resting trigger, Rs 4,500 max loss, breakeven stop once the
trade has been Rs 1,500 in profit, no new entries from 14:00, square off at
15:15, 5 concurrent trades, NSE stocks only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("super_bollinger_settings")

OVERRIDE_FILE = Path("data/super_bollinger_settings.json")
ENV_FILE = Path(".env")

# Not in _FIELDS - owned by paper_mode_control / capacity_control (see docstring).
PAPER_MODE_ENABLED_DEFAULT = os.getenv("SUPER_BOLLINGER_PAPER_MODE_ENABLED", "true").lower() == "true"
MAX_CONCURRENT_TRADES_DEFAULT = int(os.getenv("SUPER_BOLLINGER_MAX_CONCURRENT_TRADES", "5"))


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if str(v).strip().lower() in ("true", "1", "yes", "on"):
        return True
    if str(v).strip().lower() in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {v!r}")


def _parse_hhmm(v) -> str:
    return datetime.strptime(str(v).strip(), "%H:%M").strftime("%H:%M")


def _parse_symbols(v) -> list[str]:
    items = v if isinstance(v, (list, tuple)) else str(v).split(",")
    return sorted({str(s).strip().upper() for s in items if str(s).strip()})


def _to_env(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ",".join(v)
    return str(v)


# name -> (parser, env var, default, validator(value) -> error message or None)
_FIELDS = {
    "strategy_enabled": (_parse_bool, "SUPER_BOLLINGER_STRATEGY_ENABLED", "true", None),
    "max_loss_rs": (float, "SUPER_BOLLINGER_MAX_LOSS_RS", "4500",
                    lambda v: None if v > 0 else "must be > 0"),
    "breakeven_after_rs": (float, "SUPER_BOLLINGER_BREAKEVEN_AFTER_RS", "1500",
                           lambda v: None if v >= 0 else "must be >= 0 (0 turns the breakeven stop off)"),
    "entry_cutoff_time": (_parse_hhmm, "SUPER_BOLLINGER_ENTRY_CUTOFF_TIME", "14:00", None),
    "square_off_time": (_parse_hhmm, "SUPER_BOLLINGER_SQUARE_OFF_TIME", "15:15", None),
    "min_premium_rs": (float, "SUPER_BOLLINGER_MIN_PREMIUM_RS", "5",
                       lambda v: None if v >= 0 else "must be >= 0"),
    "roll_expiry_within_trading_days": (int, "SUPER_BOLLINGER_ROLL_EXPIRY_WITHIN_TRADING_DAYS", "2",
                                        lambda v: None if 0 <= v <= 10 else "must be 0-10"),
    "quantity_lots": (int, "SUPER_BOLLINGER_QUANTITY_LOTS", "1",
                      lambda v: None if 1 <= v <= 10 else "must be 1-10"),
    "funds_check_enabled": (_parse_bool, "SUPER_BOLLINGER_FUNDS_CHECK_ENABLED", "true", None),
    "excluded_symbols": (_parse_symbols, "SUPER_BOLLINGER_EXCLUDED_SYMBOLS", "", None),
}
FIELD_NAMES = tuple(_FIELDS)

_defaults = {name: parser(os.getenv(env, default)) for name, (parser, env, default, _v) in _FIELDS.items()}
_overrides: dict = {}
_overrides_loaded = False
_lock = asyncio.Lock()


def _load_overrides() -> None:
    global _overrides_loaded
    if _overrides_loaded:
        return
    _overrides_loaded = True
    if not OVERRIDE_FILE.exists():
        return
    try:
        raw = json.loads(OVERRIDE_FILE.read_text())
        for name, value in raw.items():
            if name in _FIELDS:
                _overrides[name] = _FIELDS[name][0](value)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s - using .env defaults for every Super Bollinger setting", OVERRIDE_FILE)


def get(name: str):
    _load_overrides()
    return _overrides[name] if name in _overrides else _defaults[name]


def snapshot() -> dict:
    _load_overrides()
    return {name: {"value": get(name), "source": "runtime_override" if name in _overrides else "env_default"}
            for name in _FIELDS}


def validate(changes: dict) -> tuple[dict, list[str]]:
    """Parses and validates `changes` without applying anything. Returns
    (parsed values, errors). Cross-field rule: entry cutoff must be before
    the square-off time."""
    parsed, errors = {}, []
    for name, raw in changes.items():
        if name not in _FIELDS:
            errors.append(f"{name}: unknown setting (valid: {', '.join(_FIELDS)})")
            continue
        parser, _env, _default, check = _FIELDS[name]
        try:
            value = parser(raw)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: invalid value {raw!r} ({exc})")
            continue
        problem = check(value) if check else None
        if problem:
            errors.append(f"{name}: {problem}")
            continue
        parsed[name] = value
    cutoff = parsed.get("entry_cutoff_time", get("entry_cutoff_time"))
    square = parsed.get("square_off_time", get("square_off_time"))
    if not errors and cutoff > square:
        errors.append(f"entry_cutoff_time ({cutoff}) must not be after square_off_time ({square})")
    return parsed, errors


def _sync_env(values: dict) -> bool:
    try:
        if not ENV_FILE.exists():
            logger.warning("%s not found (cwd=%s) - Super Bollinger runtime settings apply to this process "
                           "and survive restarts via %s, but .env was NOT updated", ENV_FILE, Path.cwd(), OVERRIDE_FILE)
            return False
        lines = ENV_FILE.read_text().splitlines(keepends=True)
        for name, value in values.items():
            var = _FIELDS[name][1]
            pattern = re.compile(rf"^{re.escape(var)}\s*=")
            new_line = f"{var}={_to_env(value)}\n"
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = new_line
                    break
            else:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(new_line)
        ENV_FILE.write_text("".join(lines))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Could not update .env with Super Bollinger settings %s - the runtime change still "
                         "applies (and is persisted in %s)", sorted(values), OVERRIDE_FILE)
        return False


async def update(parsed: dict) -> bool:
    """Applies already-validated values (see validate). Returns whether .env
    was updated too."""
    _load_overrides()
    async with _lock:
        _overrides.update(parsed)
        OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_FILE.write_text(json.dumps(_overrides, indent=2))
        env_synced = _sync_env(parsed)
    logger.info("Super Bollinger settings updated: %s (.env %s)", parsed, "updated" if env_synced else "NOT updated")
    return env_synced
