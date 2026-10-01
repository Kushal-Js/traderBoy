"""
Scalper runtime settings (1 Oct 2026). Same mechanics as SuperBollinger/settings.py: every value has a
.env default (SCALPER_*), can be changed at runtime via POST /scalper/config, is applied immediately,
persisted to OVERRIDE_FILE (survives restarts) and written into .env too.

Paper/real lives in the shared paper_mode_control ("Scalper", env SCALPER_PAPER_MODE_ENABLED) like every
other strategy's, so POST /paper-mode works too; the config endpoint forwards paper_mode_enabled to it.

Defaults = Swing's live exit ladder for options (Swing/config.py + .env on 1 Oct 2026) and the user's
daily loss stop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("scalper_settings")

OVERRIDE_FILE = Path("data/scalper_settings.json")
ENV_FILE = Path(".env")

# Not in _FIELDS - owned by paper_mode_control (see docstring). Code default: paper.
PAPER_MODE_ENABLED_DEFAULT = os.getenv("SCALPER_PAPER_MODE_ENABLED", "true").lower() == "true"


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
    if isinstance(v, (list, tuple)):
        return ",".join(v)
    return str(v)


_positive = lambda v: None if v > 0 else "must be > 0"            # noqa: E731
_fraction = lambda v: None if 0 < v < 1 else "must be between 0 and 1"  # noqa: E731

# name: (parser, env var, default, validator)
_FIELDS = {
    "strategy_enabled": (_parse_bool, "SCALPER_STRATEGY_ENABLED", "true", None),
    # Any NSE index or F&O stock (checked against the instrument master by POST /scalper/config); MCX refused.
    "symbols": (_parse_symbols, "SCALPER_SYMBOLS", "BANKNIFTY", lambda v: None if v else "at least one symbol"),
    # Skip an option cheaper than this (spread/slippage would dominate - matters for stock options).
    "min_premium_rs": (float, "SCALPER_MIN_PREMIUM_RS", "5", lambda v: None if v >= 0 else "must be >= 0"),
    "quantity_lots": (int, "SCALPER_QUANTITY_LOTS", "1", lambda v: None if 1 <= v <= 5 else "must be 1-5"),
    # Swing's options exit ladder (MAX_LOSS -> TARGET -> PROFIT_PROTECTION -> STOP_LOSS -> SUPERTREND_REVERSAL)
    "max_loss_rs": (float, "SCALPER_MAX_LOSS_RS", "4500", _positive),
    "target_pct": (float, "SCALPER_TARGET_PCT", "0.35", _fraction),
    "hard_stop_pct": (float, "SCALPER_HARD_STOP_PCT", "0.20", _fraction),
    "profit_protection_rs": (float, "SCALPER_PROFIT_PROTECTION_RS", "3000", _positive),
    "profit_protection_giveback_pct": (float, "SCALPER_PROFIT_PROTECTION_GIVEBACK_PCT", "0.02", _fraction),
    "supertrend_exit_enabled": (_parse_bool, "SCALPER_SUPERTREND_EXIT_ENABLED", "true", None),
    # User, 1 Oct 2026: "Stop at 3000 max loss for rest of the day with no new entries in this strategy."
    "daily_loss_limit_rs": (float, "SCALPER_DAILY_LOSS_LIMIT_RS", "3000", _positive),
    "square_off_time": (_parse_hhmm, "SCALPER_SQUARE_OFF_TIME", "15:25", None),
    "funds_check_enabled": (_parse_bool, "SCALPER_FUNDS_CHECK_ENABLED", "true", None),
    "broker_stop_enabled": (_parse_bool, "SCALPER_BROKER_STOP_ENABLED", "true", None),
    # Signal timeframes (live Swing: 5 / 15; the Scalper runs the same rules on 1-minute candles)
    "fast_interval_minutes": (int, "SCALPER_FAST_INTERVAL_MINUTES", "1", lambda v: None if v == 1 else "only 1 is built"),
    "slow_interval_minutes": (int, "SCALPER_SLOW_INTERVAL_MINUTES", "15", lambda v: None if v == 15 else "only 15 is built"),
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
        logger.exception("Could not read %s - using .env defaults for every Scalper setting", OVERRIDE_FILE)


def get(name: str):
    _load_overrides()
    return _overrides[name] if name in _overrides else _defaults[name]


def snapshot() -> dict:
    _load_overrides()
    return {name: {"value": get(name), "source": "runtime_override" if name in _overrides else "env_default"}
            for name in _FIELDS}


def validate(changes: dict) -> tuple[dict, list[str]]:
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
    return parsed, errors


def _sync_env(values: dict) -> bool:
    try:
        if not ENV_FILE.exists():
            logger.warning("%s not found - Scalper settings apply and persist in %s, .env NOT updated", ENV_FILE,
                           OVERRIDE_FILE)
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
        logger.exception("Could not update .env with Scalper settings %s", sorted(values))
        return False


async def update(parsed: dict) -> bool:
    _load_overrides()
    async with _lock:
        _overrides.update(parsed)
        OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE_FILE.write_text(json.dumps(_overrides, indent=2))
        env_synced = _sync_env(parsed)
    logger.info("Scalper settings updated: %s (.env %s)", parsed, "updated" if env_synced else "NOT updated")
    return env_synced
