"""
Breakout-signal live entry trigger (added 20 Sep 2026, user request).

A live, recalibrated version of the 5-min/1%/0.5% breakout screener built
from "Claude Code Built a FREE Breakout Stock Screener" (see
~/Desktop/projects/breakout-scanner and trading-skills' designs/
breakout-scanner-vs-real-pnl.md / designs/luxury-signal-gated-live-
simulation.md for the full backtest history behind every threshold used
here - this file intentionally carries no thresholds of its own that
weren't already backtested).

WHAT THIS DOES: a calling package (currently Luxury only) records every
symbol it gets alerted about today via `record_alert()`, called from that
package's own webhook handler - the SAME moment `trade_history.record_
webhook_alert` already fires, pure bookkeeping, no effect on the normal
alert-driven entry path. A background loop (`signal_scanner_loop`)
started from that package's own lifespan then periodically re-checks
every not-yet-signaled symbol's REAL, live 5-min candles against the 7
checks below and, the instant ALL of them hold on the most recently
completed candle, calls the owning package's own real per-symbol entry
function (`entry_fn` - e.g. Luxury's own `_process_one_entry`, wrapped
with that package's own pre-entry window/gap-down checks). Every real
production entry gate (daily re-entry cap, loss-repeat block, cross-
strategy claim, capacity + the opening-burst slot, liquid-contract
resolution, funds check) applies exactly as it would to any other
alert-driven entry, because `entry_fn` ultimately IS that same function -
nothing about entry-gating is reimplemented here.

DELIBERATELY SEPARATE FROM `alert_bucket.py`: that module's CE/PE pool
and loss-triggered switch feature are a different, still-undecided piece
of work (backtested with mixed results - see trading-skills' designs/
alert-bucket-switch.md). This module keeps its own per-strategy alert
record and shares no state with it, so enabling this feature never
implies anything about that one, and vice versa.

THE 7 CHECKS (8th - market cap - dropped, no Dhan equivalent; see the
backtest docs for why this was never binding for this universe anyway),
evaluated on the most recently completed 5-min candle using a continuous
multi-day candle series (standing rule - never resets at a day
boundary):
  1. Consolidation: prior N candles' range <= BREAKOUT_MAX_CONSOLIDATION_RANGE_PCT.
  2. Breakout/breakdown clearance: close >= high*(1+clearance%) for CE,
     or <= low*(1-clearance%) for PE (mirrored, same relationship as
     ribbon_score.py's score_ribbon_expansion/score_ribbon_breakdown pair).
  3. Candle body size >= BREAKOUT_MIN_BODY_PCT.
  4. Relative volume >= BREAKOUT_MIN_RELATIVE_VOLUME vs. the same prior
     window's average.
  5. Liquidity: 20-day average daily volume >= BREAKOUT_MIN_AVG_DAILY_VOLUME.
  6. Price level: within BREAKOUT_MAX_PCT_FROM_HIGH_LOW of the 20d/50d
     high (CE) or low (PE).
  7. Trend: close above (CE) / below (PE) both the 20d and 50d SMA.

Every threshold is read from the CALLING package's own config module
(the `cfg` parameter, matching `alert_bucket.maybe_switch`'s own
convention) - see Luxury/config.py's own LUXURY_BREAKOUT_* block for the
actual live values this runs with.

BACKTEST EVIDENCE (see trading-skills for full detail/caveats): a
Luxury-only, real-entry-gate-AND-real-exit-stack simulation over 14 real
trading days found 19 signals, 18 entered, netting +Rs32,649.10 better
than Luxury's real PnL over the same window - but on only 18 trades, with
`PROFIT_PROTECTION_HIT` dominating the exits and often landing near
breakeven on the cheap, high-volume-surge contracts this signal tends to
find. Directional evidence, not a guarantee - see `BREAKOUT_SIGNAL_
ENABLED` to turn this off per-package if live experience doesn't match.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import trade_history

logger = logging.getLogger("breakout_signal")

IST = ZoneInfo("Asia/Kolkata")

# Own single-thread pool for this feature's blocking REST reads - see
# alert_bucket.py's identical rationale: its own REST traffic must never
# starve the shared pool real order placement depends on.
_SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="breakout-signal")
_LOCK = asyncio.Lock()


def _today() -> date:
    return datetime.now(IST).date()


class _Watchlist:
    """One strategy's own today's alerted symbols - {"CE": {...}, "PE": {...}},
    each symbol mapping to {"first_alert_at", "signaled", "signaled_at"}."""

    def __init__(self, strategy: str) -> None:
        self.strategy = strategy
        self.day: Optional[date] = None
        self.items: dict[str, dict[str, dict]] = {"CE": {}, "PE": {}}


_WATCHLISTS: dict[str, _Watchlist] = {}


def _watchlist(strategy: str) -> _Watchlist:
    return _WATCHLISTS.setdefault(strategy, _Watchlist(strategy))


# ------------------------------------------------------------ persistence ---
def _path(strategy: str, d: date) -> Path:
    return trade_history.HISTORY_DIR / f"{d.isoformat()}_breakout_signal_{strategy.lower()}.json"


def _load_sync(strategy: str, d: date) -> dict[str, dict]:
    p = _path(strategy, d)
    if not p.exists():
        return {"CE": {}, "PE": {}}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s - starting today's %s breakout-signal watchlist empty", p, strategy)
        return {"CE": {}, "PE": {}}


def _write_sync(strategy: str, d: date, payload: str) -> None:
    p = _path(strategy, d)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)  # atomic
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist %s breakout-signal watchlist - in-memory state unaffected", strategy)


async def _persist(w: _Watchlist) -> None:
    payload, d = json.dumps(w.items), w.day
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_sync, w.strategy, d, payload)


def _ensure_today_locked(w: _Watchlist) -> None:
    """Caller holds _LOCK. Restart- and day-rollover-safe, same pattern as
    alert_bucket.py's own _ensure_today_locked."""
    today = _today()
    if w.day != today:
        w.day = today
        w.items = _load_sync(w.strategy, today)
        logger.info("%s breakout-signal watchlist ready for %s (%d CE / %d PE symbol(s) restored)",
                    w.strategy, today, len(w.items["CE"]), len(w.items["PE"]))


# ---------------------------------------------------------------- recording ---
async def record_alert(strategy: str, option_type: str, stocks: list[str]) -> None:
    """Fire-and-forget from the calling package's own webhook handler -
    never raises, never affects the real alert response."""
    try:
        if option_type not in ("CE", "PE") or not stocks:
            return
        w = _watchlist(strategy)
        now = datetime.now(IST).isoformat()
        async with _LOCK:
            _ensure_today_locked(w)
            bucket = w.items[option_type]
            for raw in stocks:
                sym = str(raw).strip().upper()
                if not sym:
                    continue
                if sym not in bucket:
                    bucket[sym] = {"first_alert_at": now, "signaled": False, "signaled_at": None}
            await _persist(w)
    except Exception:  # noqa: BLE001
        logger.exception("%s: record_alert failed - no effect on trading", strategy)


async def snapshot(strategy: str) -> dict:
    """Read-only view of today's watchlist, for observability."""
    w = _watchlist(strategy)
    async with _LOCK:
        _ensure_today_locked(w)
        return json.loads(json.dumps(w.items))


# ------------------------------------------------------------------ checks ---
def _sma(values: list[float]) -> float:
    return sum(values) / len(values)


def _fetch_5m_sync(symbol: str, lookback_days: int) -> dict:
    from Options.dhan_client import dhan_wrapper
    if dhan_wrapper._client is None:  # never trigger a lazy login from here
        return {}
    now = datetime.now(IST)
    resp = dhan_wrapper.client.Dhan.intraday_minute_data(
        security_id=dhan_wrapper._equity_security_id(symbol), exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=(now - timedelta(days=lookback_days)).strftime("%Y-%m-%d"), to_date=now.strftime("%Y-%m-%d"),
        interval=5,
    )
    return (resp.get("data") or {}) if isinstance(resp, dict) else {}


def _fetch_daily_sync(symbol: str, lookback_days: int) -> dict:
    from Options.dhan_client import dhan_wrapper
    if dhan_wrapper._client is None:
        return {}
    now = datetime.now(IST)
    resp = dhan_wrapper.client.Dhan.historical_daily_data(
        security_id=dhan_wrapper._equity_security_id(symbol), exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=(now - timedelta(days=lookback_days)).strftime("%Y-%m-%d"),
        # yesterday, deliberately - today's own daily bar isn't closed yet;
        # using it would leak look-ahead into the 20d/50d SMA/high/low checks.
        to_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    return (resp.get("data") or {}) if isinstance(resp, dict) else {}


def _evaluate_signal_sync(symbol: str, direction: str, cfg) -> Optional[dict]:
    """Blocking. Returns signal detail dict if every check passes on the
    most recently completed 5-min candle, else None. Never raises -
    caller treats any exception as "no signal yet, try again next cycle"."""
    try:
        candles = _fetch_5m_sync(symbol, cfg.BREAKOUT_CANDLE_LOOKBACK_DAYS)
        ts = candles.get("timestamp") or []
        opens, highs, lows, closes, vols = (candles.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        # Drop a still-forming candle and any zero-volume padding bar.
        now_epoch = time.time()
        rows = [
            (e, o, h, l, c, v) for e, o, h, l, c, v in zip(ts, opens, highs, lows, closes, vols)
            if v and e + 300 <= now_epoch
        ]
        needed = cfg.BREAKOUT_LOOKBACK_CANDLES + 1
        if len(rows) < needed:
            return None
        window = rows[-needed:]
        prior, current = window[:-1], window[-1]
        _e, c_open, c_high, c_low, c_close, c_vol = current

        consolidation_high = max(max(o, c) for _e, o, h, l, c, v in prior)
        consolidation_low = min(min(o, c) for _e, o, h, l, c, v in prior)
        if consolidation_low <= 0:
            return None
        range_pct = (consolidation_high - consolidation_low) / consolidation_low * 100
        if not (range_pct <= cfg.BREAKOUT_MAX_CONSOLIDATION_RANGE_PCT):
            return None

        clearance = 1 + cfg.BREAKOUT_CLEARANCE_PCT / 100
        if direction == "bullish":
            if not (c_close >= consolidation_high * clearance):
                return None
        else:
            if not (c_close <= consolidation_low * (2 - clearance)):
                return None

        body_pct = abs(c_close - c_open) / c_open * 100
        if not (body_pct >= cfg.BREAKOUT_MIN_BODY_PCT):
            return None

        avg_prior_vol = _sma([v for _e, o, h, l, c, v in prior])
        relative_volume = c_vol / avg_prior_vol if avg_prior_vol > 0 else 0
        if not (relative_volume >= cfg.BREAKOUT_MIN_RELATIVE_VOLUME):
            return None

        daily = _fetch_daily_sync(symbol, cfg.BREAKOUT_DAILY_LOOKBACK_DAYS)
        d_closes, d_vols = daily.get("close") or [], daily.get("volume") or []
        if len(d_closes) < 50:
            return None
        last20c, last50c, last20v = d_closes[-20:], d_closes[-50:], d_vols[-20:]

        if _sma(last20v) < cfg.BREAKOUT_MIN_AVG_DAILY_VOLUME:
            return None

        if direction == "bullish":
            high20, high50 = max(last20c), max(last50c)
            pct_from_20 = (high20 - c_close) / high20 * 100
            pct_from_50 = (high50 - c_close) / high50 * 100
            if not (pct_from_20 <= cfg.BREAKOUT_MAX_PCT_FROM_HIGH_LOW or pct_from_50 <= cfg.BREAKOUT_MAX_PCT_FROM_HIGH_LOW):
                return None
            if not (c_close > _sma(last20c) and c_close > _sma(last50c)):
                return None
        else:
            low20, low50 = min(last20c), min(last50c)
            pct_from_20 = (c_close - low20) / low20 * 100
            pct_from_50 = (c_close - low50) / low50 * 100
            if not (pct_from_20 <= cfg.BREAKOUT_MAX_PCT_FROM_HIGH_LOW or pct_from_50 <= cfg.BREAKOUT_MAX_PCT_FROM_HIGH_LOW):
                return None
            if not (c_close < _sma(last20c) and c_close < _sma(last50c)):
                return None

        return {
            "symbol": symbol, "close": c_close, "range_pct": round(range_pct, 2),
            "body_pct": round(body_pct, 2), "relative_volume": round(relative_volume, 2),
            "detected_at": datetime.now(IST).isoformat(),
        }
    except Exception:  # noqa: BLE001
        logger.exception("%s: breakout-signal evaluation failed - will retry next cycle", symbol)
        return None


# ------------------------------------------------------------------- loop ---
def _market_hours_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 10) <= (now.hour, now.minute) <= (15, 35)


async def _scan_cycle(strategy: str, cfg, entry_fn: Callable[[str, str], Awaitable[dict]]) -> None:
    w = _watchlist(strategy)
    async with _LOCK:
        _ensure_today_locked(w)
        pending = [
            (ot, sym) for ot in ("CE", "PE") for sym, it in w.items[ot].items() if not it["signaled"]
        ]
    loop = asyncio.get_running_loop()
    checked = 0
    for ot, sym in pending:
        if checked >= cfg.BREAKOUT_SCAN_MAX_PER_CYCLE:
            break
        checked += 1
        direction = "bullish" if ot == "CE" else "bearish"
        sig = await loop.run_in_executor(_SCAN_EXECUTOR, _evaluate_signal_sync, sym, direction, cfg)
        await asyncio.sleep(cfg.BREAKOUT_SCAN_PACE_SECONDS)
        if sig is None:
            continue

        # Mark signaled BEFORE attempting entry - at most one attempt per
        # symbol per day even if entry_fn itself fails/skips, matching the
        # backtest's own "first qualifying candle only" design.
        async with _LOCK:
            it = w.items[ot].get(sym)
            if it is None or it["signaled"]:
                continue
            it["signaled"], it["signaled_at"] = True, sig["detected_at"]
            await _persist(w)

        logger.warning(
            "BREAKOUT SIGNAL [%s]: %s %s confirmed (range=%.2f%% body=%.2f%% relvol=%.2fx) - attempting real entry",
            strategy, ot, sym, sig["range_pct"], sig["body_pct"], sig["relative_volume"],
        )
        try:
            result = await entry_fn(sym, ot)
        except Exception:  # noqa: BLE001
            logger.exception("%s: breakout-signal entry attempt for %s failed", strategy, sym)
            result = {"status": "error"}
        trade_history.append_jsonl("breakout_signals", {
            "strategy": strategy, "option_type": ot, "symbol": sym, **{k: v for k, v in sig.items() if k != "symbol"},
            "entry_result_status": (result or {}).get("status"), "entry_result_reason": (result or {}).get("reason"),
            "logged_at": datetime.now().isoformat(),
        })


async def signal_scanner_loop(strategy: str, cfg, entry_fn: Callable[[str, str], Awaitable[dict]]) -> None:
    """Started once from the calling package's own lifespan."""
    logger.info(
        "%s breakout-signal scanner started (enabled=%s, interval=%ss, max %d/cycle).",
        strategy, cfg.BREAKOUT_SIGNAL_ENABLED, cfg.BREAKOUT_SCAN_INTERVAL_SECONDS, cfg.BREAKOUT_SCAN_MAX_PER_CYCLE,
    )
    while True:
        try:
            if cfg.BREAKOUT_SIGNAL_ENABLED and _market_hours_now():
                await _scan_cycle(strategy, cfg, entry_fn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("%s: breakout-signal scan cycle failed - will retry next interval", strategy)
        await asyncio.sleep(cfg.BREAKOUT_SCAN_INTERVAL_SECONDS)
