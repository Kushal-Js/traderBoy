"""
Unified Momentum runtime settings (1 Oct 2026, user request: one strategy taking the best of Super Bollinger,
SwingMomentum and the rest; real trading on by default with a flag to turn it off).

Every value has a .env default (UNIFIED_MOMENTUM_*, read once at import) and can be changed at runtime via
POST /unified-momentum/config: applied immediately (every reader calls get() on each use), persisted to
OVERRIDE_FILE so it survives a restart, and written into .env too (the two-place rule of paper_mode_control /
capacity_control - .env never silently disagrees with what the bot runs).
Paper/real and engine A's slot limit are NOT stored here: they live in the shared paper_mode_control
("UnifiedMomentum") and capacity_control ("UnifiedMomentum") modules like every other strategy's, so POST /paper-mode
and POST /capacity/max-concurrent-trades work for it too; the config endpoint forwards those two keys.

Defaults = the backtested setup (research_unified_super_strategy*.py, 3 Aug - 29 Sep 2026, Rs 1.2L, walk-forward
picks: +1,46,038 with the hedge, max drawdown -13,712): engine A = Super Bollinger's live rules (resting BULLISH
trigger, last 1-hour candle green, ATM call, max loss 4,500, breakeven after +1,500, entries to 14:00, out 15:15,
real PUT hedge, ratcheted broker stop), 2 slots; engine B = SwingMomentum's momentum PUTs (Swing v3 Supertrend cross
down + MOMENTUM state), entries to 14:30, Swing's options exit ladder, out 15:15, 2 slots; both: premium >= Rs 10 and
no new entries while NIFTY's own 2 h efficiency ratio is below market_chop_gate_er (0.10).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("unified_momentum_settings")

OVERRIDE_FILE = Path("data/unified_momentum_settings.json")
ENV_FILE = Path(".env")

# Not in _FIELDS - owned by paper_mode_control / capacity_control (see docstring). REAL by default (user, 1 Oct 2026).
PAPER_MODE_ENABLED_DEFAULT = os.getenv("UNIFIED_MOMENTUM_PAPER_MODE_ENABLED", "false").lower() == "true"
MAX_CONCURRENT_TRADES_DEFAULT = int(os.getenv("UNIFIED_MOMENTUM_MAX_CONCURRENT_TRADES", "2"))


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


def _parse_mode(v) -> str:
    v = str(v).strip().lower()
    if v not in ("off", "shadow", "paper", "real"):
        raise ValueError("must be off, shadow, paper or real")
    return v


def _parse_bypass_mode(v) -> str:
    v = str(v).strip().lower()
    if v not in ("off", "shadow"):
        raise ValueError("must be off or shadow (a trading bypass is not built - the 1 Oct backtests did not support one)")
    return v


def _parse_late_entry_mode(v) -> str:
    v = str(v).strip().lower()
    if v not in ("off", "shadow"):
        raise ValueError("must be off or shadow (a real late entry is not built - collect shadow data first)")
    return v


def _parse_filter_mode(v) -> str:
    v = str(v).strip().lower()
    if v not in ("off", "shadow", "on"):
        raise ValueError("must be off, shadow or on")
    return v


def _to_env(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ",".join(v)
    return str(v)


_pos = lambda v: None if v > 0 else "must be > 0"
_nonneg = lambda v: None if v >= 0 else "must be >= 0"

# name -> (parser, env var, default, validator(value) -> error message or None)
_FIELDS = {
    "strategy_enabled": (_parse_bool, "UNIFIED_MOMENTUM_STRATEGY_ENABLED", "true", None),
    # ---- Engine A: pullback calls (Super Bollinger's rules) ----
    "max_loss_rs": (float, "UNIFIED_MOMENTUM_MAX_LOSS_RS", "4500", _pos),
    "breakeven_after_rs": (float, "UNIFIED_MOMENTUM_BREAKEVEN_AFTER_RS", "1500",
                           lambda v: None if v >= 0 else "must be >= 0 (0 turns the breakeven stop off)"),
    "entry_cutoff_time": (_parse_hhmm, "UNIFIED_MOMENTUM_ENTRY_CUTOFF_TIME", "14:00", None),
    "square_off_time": (_parse_hhmm, "UNIFIED_MOMENTUM_SQUARE_OFF_TIME", "15:15", None),
    # both engines: cheaper options lose their edge to slippage (MOTHERSON on Swing; +42k in the backtest)
    "min_premium_rs": (float, "UNIFIED_MOMENTUM_MIN_PREMIUM_RS", "10", _nonneg),
    "roll_expiry_within_trading_days": (int, "UNIFIED_MOMENTUM_ROLL_EXPIRY_WITHIN_TRADING_DAYS", "2",
                                        lambda v: None if 0 <= v <= 10 else "must be 0-10"),
    "quantity_lots": (int, "UNIFIED_MOMENTUM_QUANTITY_LOTS", "1", lambda v: None if 1 <= v <= 10 else "must be 1-10"),
    "funds_check_enabled": (_parse_bool, "UNIFIED_MOMENTUM_FUNDS_CHECK_ENABLED", "true", None),
    "excluded_symbols": (_parse_symbols, "UNIFIED_MOMENTUM_EXCLUDED_SYMBOLS", "", None),
    # ---- Market chop gate (both engines) ----
    # No NEW entries while NIFTY's own efficiency ratio over its last 24 closed 5-min candles (2 h) is below this
    # (0 = off). 0.10-0.12 improved both profit and drawdown in the backtest; stricter values cost profit.
    "market_chop_gate_er": (float, "UNIFIED_MOMENTUM_MARKET_CHOP_GATE_ER", "0.10",
                            lambda v: None if 0 <= v < 1 else "must be 0 (off) to < 1"),
    # ---- Supervisor: PUT hedge on engine A's calls ----
    "hedge_mode": (_parse_mode, "UNIFIED_MOMENTUM_HEDGE_MODE", "real", None),
    "hedge_trigger_rs": (float, "UNIFIED_MOMENTUM_HEDGE_TRIGGER_RS", "1800", _pos),
    "hedge_atr_mult": (float, "UNIFIED_MOMENTUM_HEDGE_ATR_MULT", "1.0",
                       lambda v: None if v >= 0 else "must be >= 0 (0 = no ATR confirmation)"),
    "hedge_trail_arm_rs": (float, "UNIFIED_MOMENTUM_HEDGE_TRAIL_ARM_RS", "1000", _pos),
    "hedge_trail_giveback": (float, "UNIFIED_MOMENTUM_HEDGE_TRAIL_GIVEBACK", "0.30",
                             lambda v: None if 0 < v < 1 else "must be between 0 and 1"),
    "hedge_stop_rs": (float, "UNIFIED_MOMENTUM_HEDGE_STOP_RS", "1500", _pos),
    "hedge_cutoff_time": (_parse_hhmm, "UNIFIED_MOMENTUM_HEDGE_CUTOFF_TIME", "15:15", None),
    # Disaster brake: the day's realized + open REAL PnL (calls, hedges and engine B's puts) at or below
    # -disaster_brake_rs -> no new entries or hedges today, everything squared off (0 = off). A malfunction guard.
    "disaster_brake_rs": (float, "UNIFIED_MOMENTUM_DISASTER_BRAKE_RS", "20000", _nonneg),
    "shadow_stop_reenter_rs": (float, "UNIFIED_MOMENTUM_SHADOW_STOP_REENTER_RS", "0",
                               lambda v: None if v >= 0 else "must be >= 0 (0 = off)"),
    # ---- Engine A's 1-hour-green entry filter ----
    "entry_filter_1h": (_parse_filter_mode, "UNIFIED_MOMENTUM_ENTRY_FILTER_1H", "on", None),
    "entry_filter_1h_minutes": (int, "UNIFIED_MOMENTUM_ENTRY_FILTER_1H_MINUTES", "60",
                                lambda v: None if 15 <= v <= 120 and v % 5 == 0 else "must be 15-120 in steps of 5"),
    "entry_filter_1h_bypass": (_parse_bypass_mode, "UNIFIED_MOMENTUM_ENTRY_FILTER_1H_BYPASS", "off", None),
    "entry_filter_1h_bypass_day_up_pct": (float, "UNIFIED_MOMENTUM_ENTRY_FILTER_1H_BYPASS_DAY_UP_PCT", "1.5",
                                          lambda v: None if 0 < v <= 10 else "must be > 0 and <= 10"),
    # Engine A triggers the live feed never saw (log only, see Super Bollinger b2cc18e).
    "late_entry_mode": (_parse_late_entry_mode, "UNIFIED_MOMENTUM_LATE_ENTRY_MODE", "shadow", None),
    "late_entry_max_pct": (float, "UNIFIED_MOMENTUM_LATE_ENTRY_MAX_PCT", "0.3",
                           lambda v: None if 0 < v <= 2 else "must be > 0 and <= 2"),
    # ---- Unfilled orders: re-price and retry (both engines and the hedge) ----
    "entry_retry_max": (int, "UNIFIED_MOMENTUM_ENTRY_RETRY_MAX", "2", lambda v: None if 0 <= v <= 5 else "must be 0-5"),
    "entry_retry_wait_seconds": (float, "UNIFIED_MOMENTUM_ENTRY_RETRY_WAIT_SECONDS", "5",
                                 lambda v: None if 1 <= v <= 30 else "must be 1-30"),
    "entry_chase_max_pct": (float, "UNIFIED_MOMENTUM_ENTRY_CHASE_MAX_PCT", "5",
                            lambda v: None if 0 <= v <= 25 else "must be 0-25"),
    "entry_retry_limit_buffer_pct": (float, "UNIFIED_MOMENTUM_ENTRY_RETRY_LIMIT_BUFFER_PCT", "1",
                                     lambda v: None if 0 <= v <= 5 else "must be 0-5"),
    # Safety sweep of this strategy's own unmanaged BUY orders / untracked broker positions (0 = off).
    "orphan_sweep_seconds": (int, "UNIFIED_MOMENTUM_ORPHAN_SWEEP_SECONDS", "30",
                             lambda v: None if v == 0 or 10 <= v <= 600 else "must be 0 (off) or 10-600"),
    # ---- Ratcheted broker stop (engine A's calls and hedges) ----
    "stop_ratchet_mode": (_parse_filter_mode, "UNIFIED_MOMENTUM_STOP_RATCHET_MODE", "on", None),
    "stop_ratchet_min_step_rs": (float, "UNIFIED_MOMENTUM_STOP_RATCHET_MIN_STEP_RS", "250",
                                 lambda v: None if 50 <= v <= 5000 else "must be 50-5000"),
    "stop_ratchet_min_interval_seconds": (float, "UNIFIED_MOMENTUM_STOP_RATCHET_MIN_INTERVAL_SECONDS", "5",
                                          lambda v: None if 1 <= v <= 120 else "must be 1-120"),
    # ---- Engine B: momentum PUTs (SwingMomentum's signal, made intraday) ----
    "engine_b_enabled": (_parse_bool, "UNIFIED_MOMENTUM_ENGINE_B_ENABLED", "true", None),
    "b_max_concurrent_trades": (int, "UNIFIED_MOMENTUM_B_MAX_CONCURRENT_TRADES", "2",
                                lambda v: None if 0 <= v <= 10 else "must be 0-10"),
    "b_entry_cutoff_time": (_parse_hhmm, "UNIFIED_MOMENTUM_B_ENTRY_CUTOFF_TIME", "14:30", None),
    "b_max_loss_rs": (float, "UNIFIED_MOMENTUM_B_MAX_LOSS_RS", "4500", _pos),
    "b_target_pct": (float, "UNIFIED_MOMENTUM_B_TARGET_PCT", "0.35",
                     lambda v: None if 0 <= v < 5 else "must be 0 (off) to < 5"),
    "b_profit_protection_rs": (float, "UNIFIED_MOMENTUM_B_PROFIT_PROTECTION_RS", "3000", _pos),
    "b_profit_protection_giveback": (float, "UNIFIED_MOMENTUM_B_PROFIT_PROTECTION_GIVEBACK", "0.02",
                                     lambda v: None if 0 < v < 1 else "must be between 0 and 1"),
    "b_hard_stop_pct": (float, "UNIFIED_MOMENTUM_B_HARD_STOP_PCT", "0.20",
                        lambda v: None if 0 < v < 1 else "must be between 0 and 1"),
    "b_supertrend_exit": (_parse_bool, "UNIFIED_MOMENTUM_B_SUPERTREND_EXIT", "true", None),
    "b_volume_floor_ratio": (float, "UNIFIED_MOMENTUM_B_VOLUME_FLOOR_RATIO", "0.6",
                             lambda v: None if 0 <= v <= 5 else "must be 0 (off) to 5"),
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
        logger.exception("Could not read %s - using .env defaults for every Unified Momentum setting", OVERRIDE_FILE)


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
    square = parsed.get("square_off_time", get("square_off_time"))
    for key in ("entry_cutoff_time", "b_entry_cutoff_time"):
        cutoff = parsed.get(key, get(key))
        if not errors and cutoff > square:
            errors.append(f"{key} ({cutoff}) must not be after square_off_time ({square})")
    return parsed, errors


def _sync_env(values: dict) -> bool:
    try:
        if not ENV_FILE.exists():
            logger.warning("%s not found (cwd=%s) - Unified Momentum runtime settings apply to this process "
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
        logger.exception("Could not update .env with Unified Momentum settings %s - the runtime change still "
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
    logger.info("Unified Momentum settings updated: %s (.env %s)", parsed, "updated" if env_synced else "NOT updated")
    return env_synced
