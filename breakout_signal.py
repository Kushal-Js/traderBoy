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

CE and PE are tracked as genuinely SEPARATE watchlists (added 20 Sep
2026, user request) - two independent `_Watchlist` objects per strategy,
each with its own persisted file (`history/<date>_breakout_signal_
<strategy>_<CE|PE>.json`), same one-file-per-option-type convention
alert_bucket.py already uses. Nothing about CE ever touches PE's state or
file, and either can be reset independently.

DAILY REFRESH (added 20 Sep 2026, user request): each watchlist resets
to empty at TWO points, both handled inside `signal_scanner_loop` itself
so they happen unconditionally every ~60s regardless of market hours or
whether `BREAKOUT_SIGNAL_ENABLED` is on:
  1. BEFORE market starts - a fresh calendar date always starts empty
     (`_ensure_today_locked`, keyed on the date change itself, so the
     very first loop tick after midnight already resets it - hours
     before the 09:15 open, not merely "whenever the next alert/scan
     happens to occur").
  2. AFTER market ends - once past `BREAKOUT_MARKET_END_TIME` (default
     15:35 IST, matching this module's own `_market_hours_now` upper
     bound), the watchlist is explicitly truncated to empty for the rest
     of the day (`_maybe_clear_after_close`, a once-per-date action) so
     it doesn't sit around showing the completed day's full signaled
     state into the evening - it reads as "done for today" immediately.

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
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import trade_history

logger = logging.getLogger("breakout_signal")

IST = ZoneInfo("Asia/Kolkata")

# Own single-thread pool for this feature's blocking REST reads - see
# alert_bucket.py's identical rationale: its own REST traffic must never
# starve the shared pool real order placement depends on.
_SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="breakout-signal")
_LOCK = asyncio.Lock()

# Round-robin position for _scan_cycle/_dispatch_scan_cycle, keyed by
# strategy (DISPATCHER_STRATEGY_NAME for the dispatcher) - see
# _rotate_pending_from_cursor's own docstring for the starvation bug this
# fixes (real incident, 22 Sep 2026).
_scan_cursor: dict[str, str] = {}


def _rotate_pending_from_cursor(strategy: str, pending: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Rotates `pending` (rebuilt fresh every cycle, in stable watchlist
    insertion order - CE items then PE items) so a cycle resumes scanning
    right after wherever the previous cycle left off, instead of always
    restarting from the front.

    Without this, a fixed per-cycle budget (BREAKOUT_SCAN_MAX_PER_CYCLE)
    starves every symbol past the first batch forever: a symbol that's
    checked and shows no signal stays unsignaled, so it lands right back
    in the exact same front-of-list position next cycle, and anything
    after the first MAX_PER_CYCLE entries is never reached at all - not
    "slow", genuinely never scanned. Real incident, 22 Sep 2026: with 38
    CE + 45 PE symbols tracked by UniverseDispatcher and MAX_PER_CYCLE=10,
    only the first ~10 CE symbols (in original alert-insertion order) were
    EVER evaluated all session; PE was never reached even once, since CE
    alone already exhausted the per-cycle budget every single tick.

    Tracks the last symbol NAME examined (not a raw index), so this is
    naturally resilient to symbols being added/signaled/removed between
    cycles - if that symbol is no longer in `pending` (it got signaled,
    the day rolled over, or post-close truncation cleared the watchlist),
    this simply falls back to starting from the front, which is always a
    safe default."""
    if not pending:
        return pending
    last_sym = _scan_cursor.get(strategy)
    if last_sym is None:
        return pending
    for i, (_ot, sym) in enumerate(pending):
        if sym == last_sym:
            start = (i + 1) % len(pending)
            return pending[start:] + pending[:start]
    return pending


def _today() -> date:
    return datetime.now(IST).date()


class _Watchlist:
    """One strategy's own today's alerted symbols for ONE option type -
    symbol -> {"first_alert_at", "signaled", "signaled_at"}. CE and PE are
    always separate instances, never share a dict - see module docstring."""

    def __init__(self, strategy: str, option_type: str) -> None:
        self.strategy = strategy
        self.option_type = option_type
        self.day: Optional[date] = None
        self.items: dict[str, dict] = {}
        # Tracks which date's post-close truncation has already run, so
        # _maybe_clear_after_close only ever fires once per date.
        self.cleared_after_close_for: Optional[date] = None


_WATCHLISTS: dict[tuple[str, str], _Watchlist] = {}


def _watchlist(strategy: str, option_type: str) -> _Watchlist:
    return _WATCHLISTS.setdefault((strategy, option_type), _Watchlist(strategy, option_type))


# ------------------------------------------------------------ persistence ---
def _path(strategy: str, option_type: str, d: date) -> Path:
    return trade_history.HISTORY_DIR / f"{d.isoformat()}_breakout_signal_{strategy.lower()}_{option_type}.json"


def _load_sync(strategy: str, option_type: str, d: date) -> dict[str, dict]:
    p = _path(strategy, option_type, d)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        logger.exception("Could not read %s - starting today's %s %s breakout-signal watchlist empty",
                          p, strategy, option_type)
        return {}


def _write_sync(strategy: str, option_type: str, d: date, payload: str) -> None:
    p = _path(strategy, option_type, d)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)  # atomic
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist %s %s breakout-signal watchlist - in-memory state unaffected",
                          strategy, option_type)


async def _persist(w: _Watchlist) -> None:
    payload, d = json.dumps(w.items), w.day
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_sync, w.strategy, w.option_type, d, payload)


def _ensure_today_locked(w: _Watchlist) -> None:
    """Caller holds _LOCK. Restart- and day-rollover-safe, same pattern as
    alert_bucket.py's own _ensure_today_locked. This is what makes "before
    market starts" true: the FIRST touch of a new calendar date (which,
    per signal_scanner_loop below, happens within one tick of midnight,
    not merely "whenever the next alert/scan occurs") resets to that
    date's own (empty, unless restored from a same-day restart) file."""
    today = _today()
    if w.day != today:
        w.day = today
        w.items = _load_sync(w.strategy, w.option_type, today)
        w.cleared_after_close_for = None
        logger.info("%s %s breakout-signal watchlist ready for %s (%d symbol(s) restored)",
                    w.strategy, w.option_type, today, len(w.items))


async def _maybe_clear_after_close(w: _Watchlist, cfg) -> None:
    """Explicit post-market-close truncation (user request 20 Sep 2026) -
    once past cfg.BREAKOUT_MARKET_END_TIME, zero the watchlist for the
    rest of the day so it reads as "done for today" immediately rather
    than sitting on the completed day's full signaled state until the
    next calendar date rolls over. Runs at most once per date."""
    now = datetime.now(IST)
    end_h, end_m = (int(x) for x in cfg.BREAKOUT_MARKET_END_TIME.split(":"))
    async with _LOCK:
        _ensure_today_locked(w)
        if (now.hour, now.minute) < (end_h, end_m):
            return
        if w.cleared_after_close_for == w.day:
            return
        had_items = len(w.items)
        w.items = {}
        w.cleared_after_close_for = w.day
        await _persist(w)
    if had_items:
        logger.info("%s %s breakout-signal watchlist truncated to zero after market close (%s) - %d symbol(s) cleared.",
                     w.strategy, w.option_type, cfg.BREAKOUT_MARKET_END_TIME, had_items)


# ---------------------------------------------------------------- recording ---
async def record_alert(strategy: str, option_type: str, stocks: list[str]) -> None:
    """Fire-and-forget from the calling package's own webhook handler -
    never raises, never affects the real alert response."""
    try:
        if option_type not in ("CE", "PE") or not stocks:
            return
        w = _watchlist(strategy, option_type)
        now = datetime.now(IST).isoformat()
        async with _LOCK:
            _ensure_today_locked(w)
            for raw in stocks:
                sym = str(raw).strip().upper()
                if not sym:
                    continue
                if sym not in w.items:
                    w.items[sym] = {"first_alert_at": now, "signaled": False, "signaled_at": None,
                                     "checked_through_epoch": None}
            await _persist(w)
    except Exception:  # noqa: BLE001
        logger.exception("%s %s: record_alert failed - no effect on trading", strategy, option_type)


async def snapshot(strategy: str) -> dict:
    """Read-only view of today's CE and PE watchlists, for observability -
    same {"CE": {...}, "PE": {...}} shape as before, now backed by two
    genuinely separate underlying watchlists."""
    out = {}
    for option_type in ("CE", "PE"):
        w = _watchlist(strategy, option_type)
        async with _LOCK:
            _ensure_today_locked(w)
            out[option_type] = json.loads(json.dumps(w.items))
    return out


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


def _fetch_5m_hybrid(symbol: str, cfg) -> dict:
    """WS-first, REST-fallback (added 21 Sep 2026 - see underlying_candle_
    feed.py's own module docstring for the full rationale: REST-polling a
    wide watchlist every scan cycle doesn't scale, but polling stays the
    correctness fallback rather than being replaced outright, per the
    user's own framing). Inert unless cfg.BREAKOUT_USE_WS_CANDLES is true
    (default false) - falls straight through to the original REST-only
    _fetch_5m_sync otherwise, byte-for-byte the same behavior as before
    this function existed."""
    if not getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False):
        return _fetch_5m_sync(symbol, cfg.BREAKOUT_CANDLE_LOOKBACK_DAYS)
    import underlying_candle_feed
    stale_after = getattr(cfg, "BREAKOUT_WS_STALE_AFTER_SECONDS", 90)
    if underlying_candle_feed.is_fresh(symbol, stale_after):
        data = underlying_candle_feed.get_candles_dict(symbol)
        if data:
            return data
    # Not subscribed yet, no tick received yet, or gone stale - REST fallback.
    return _fetch_5m_sync(symbol, cfg.BREAKOUT_CANDLE_LOOKBACK_DAYS)


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


def _rows_from_candles(candles: dict) -> list[tuple]:
    """Drops a still-forming candle and any zero-volume padding bar - same
    filter every candle source (REST or WS) needs before check logic sees
    it. `underlying_candle_feed.get_candles_dict` already never returns
    the still-forming bar, but the epoch filter is kept here too rather
    than trusted per-source, so this function is safe regardless of which
    fetcher produced `candles`."""
    ts = candles.get("timestamp") or []
    opens, highs, lows, closes, vols = (candles.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    now_epoch = time.time()
    return [
        (e, o, h, l, c, v) for e, o, h, l, c, v in zip(ts, opens, highs, lows, closes, vols)
        if v and e + 300 <= now_epoch
    ]


def _check_candle(rows: list[tuple], idx: int, daily: dict, direction: str, cfg) -> Optional[dict]:
    """Pure per-candle check - the same 7 checks (5 candle-based, 2 daily-
    based) _evaluate_signal_sync always ran, with `rows[idx]` as the
    'current' candle. Split out of that single-snapshot function (added
    23 Sep 2026, user request) so a WS-fed symbol's entire unchecked
    candle history can be walked one candle at a time in one pass - see
    _evaluate_ws_walk_sync's own docstring for why a single-snapshot check
    was silently losing real signals to the scan-cadence bottleneck.
    `daily` is passed in (not fetched here) so a multi-candle walk fetches
    it once, not once per candle - see _fetch_daily_cached."""
    needed = cfg.BREAKOUT_LOOKBACK_CANDLES + 1
    if idx < needed - 1:
        return None
    window = rows[idx - needed + 1: idx + 1]
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
        "symbol": None, "close": c_close, "range_pct": round(range_pct, 2),
        "body_pct": round(body_pct, 2), "relative_volume": round(relative_volume, 2),
        "detected_at": datetime.now(IST).isoformat(), "candle_epoch": _e,
    }


_daily_cache: dict[str, tuple[date, dict]] = {}  # symbol -> (date, daily_dict)


def _fetch_daily_cached(symbol: str, cfg) -> tuple[dict, bool]:
    """20d/50d daily closes/volumes don't change intraday - fetching them
    fresh every scan cycle (the only behavior before 23 Sep 2026) was
    needless REST load, and would become a real rate-limit risk once a
    WS-fresh symbol can be walked through many candles per cycle instead
    of just one (see _evaluate_ws_walk_sync). Cached per symbol per
    calendar date; refetched once the date rolls over. Returns
    (daily_dict, did_fetch) - did_fetch lets a caller pace ONLY the calls
    that actually hit Dhan, not the (overwhelmingly common, after the
    first pass of the day) cache-hit case."""
    today = _today()
    cached = _daily_cache.get(symbol)
    if cached is not None and cached[0] == today:
        return cached[1], False
    daily = _fetch_daily_sync(symbol, cfg.BREAKOUT_DAILY_LOOKBACK_DAYS)
    if daily.get("close"):
        _daily_cache[symbol] = (today, daily)
    return daily, True


def _evaluate_signal_sync(symbol: str, direction: str, cfg) -> Optional[dict]:
    """Blocking, REST/hybrid path - UNCHANGED behavior from before 23 Sep
    2026: checks only the most recently completed candle. Kept exactly
    this way for symbols not on a fresh WS feed - walking every unchecked
    candle every cycle (like the WS path now does) would reintroduce the
    real REST rate-limit risk underlying_candle_feed.py was built to
    avoid (each candle-source fetch here is a REST round-trip, unlike the
    WS path's free in-memory read). Never raises - caller treats any
    exception as "no signal yet, try again next cycle"."""
    try:
        candles = _fetch_5m_hybrid(symbol, cfg)
        rows = _rows_from_candles(candles)
        if not rows:
            return None
        daily, _did_fetch = _fetch_daily_cached(symbol, cfg)
        sig = _check_candle(rows, len(rows) - 1, daily, direction, cfg)
        if sig:
            sig["symbol"] = symbol
        return sig
    except Exception:  # noqa: BLE001
        logger.exception("%s: breakout-signal evaluation failed - will retry next cycle", symbol)
        return None


def _evaluate_ws_walk_sync(symbol: str, direction: str, cfg,
                            checked_through_epoch: Optional[float]) -> tuple[Optional[dict], Optional[float], bool]:
    """Blocking, WS-only path (added 23 Sep 2026, user request after a
    real, quantified miss the same day: BANDHANBNK's own qualifying
    candle was 09:15, but the scan-cadence bottleneck
    (BREAKOUT_SCAN_MAX_PER_CYCLE/BREAKOUT_SCAN_INTERVAL_SECONDS means a
    ~90-symbol combined watchlist only gets one look per symbol roughly
    every ~9 minutes) meant the live check didn't happen until ~09:22 -
    by then the underlying had already run and the real ATM CE entry cost
    ~3x more than it would have at 09:15, and that late, already-extended
    entry stopped out for a real loss minutes later.
    _evaluate_signal_sync's single-snapshot design (only ever the MOST
    RECENT candle) throws away every candle it wasn't looking at the
    instant it closed - once missed, gone for the day, even though the
    data was sitting right there.

    For a WS-fresh symbol the candle history is a free, local, already-
    persisted list (underlying_candle_feed.get_candles_dict, up to
    MAX_BARS_KEPT=120 bars) - no REST cost to check every unchecked
    candle instead of just the last one. Walks forward from the first
    candle after `checked_through_epoch` (None = never checked - starts
    from the earliest evaluable candle, so a symbol added mid-session
    still gets its full day's history examined, not just whatever candle
    happens to be 'current' the moment it's first scanned), returning the
    FIRST one that confirms (preserving the existing "first qualifying
    candle wins" semantics) or None if none did. Either way also returns
    the epoch of the last candle actually examined (so the caller can
    advance checked_through_epoch and never re-examine an already-cleared
    candle) and whether a fresh daily REST fetch happened this call (so
    the caller can pace only that, not the common all-cached case).

    STALE-CANDLE GUARD: a walked candle that ISN'T the freshest one
    available (i.e. real time has passed since it closed - the exact
    "was missed, found on replay" case this function exists for) is
    re-validated against the LATEST close via reversal_filters.
    check_underlying_move_confirms_exit before being treated as
    tradeable - the same real, already-backtested 0.10%-move check the
    capacity backlog already trusts to answer "is a past signal still
    live", just pointed at a replayed candle instead of a backlog entry.
    Without this, retroactively catching a qualifying candle from long
    ago could fire a real entry into a breakout that has since fully
    reversed - not a hypothetical: this is exactly the failure mode a
    fixed 'just check every unchecked candle' walk would otherwise have.
    A stale-and-reversed candle is skipped (not returned, not treated as
    the walk's answer) and the walk continues to the next candle - a
    LATER, still-fresh qualifying candle should still fire."""
    try:
        import underlying_candle_feed
        import reversal_filters
        candles = underlying_candle_feed.get_candles_dict(symbol)
        rows = _rows_from_candles(candles)
        needed = cfg.BREAKOUT_LOOKBACK_CANDLES + 1
        if len(rows) < needed:
            return None, checked_through_epoch, False

        start_idx = needed - 1
        if checked_through_epoch is not None:
            first_unchecked = next((i for i, r in enumerate(rows) if r[0] > checked_through_epoch), None)
            if first_unchecked is None:
                return None, checked_through_epoch, False  # every row already checked
            start_idx = max(start_idx, first_unchecked)
        if start_idx >= len(rows):
            return None, checked_through_epoch, False

        daily, did_fetch = _fetch_daily_cached(symbol, cfg)
        current_close = rows[-1][4]
        option_type = "CE" if direction == "bullish" else "PE"
        for idx in range(start_idx, len(rows)):
            sig = _check_candle(rows, idx, daily, direction, cfg)
            if sig is None:
                continue
            if idx < len(rows) - 1:
                reversed_against = reversal_filters.check_underlying_move_confirms_exit(
                    sig["close"], current_close, option_type)
                if reversed_against:
                    logger.info("%s: WS-walk found a %s candle at %s but it has since reversed "
                                "(close then=%.2f now=%.2f) - skipping, not treated as a live signal",
                                symbol, direction, datetime.fromtimestamp(sig["candle_epoch"], tz=IST).strftime("%H:%M"),
                                sig["close"], current_close)
                    continue
            sig["symbol"] = symbol
            return sig, rows[idx][0], did_fetch
        return None, rows[-1][0], did_fetch
    except Exception:  # noqa: BLE001
        logger.exception("%s: WS-walk breakout-signal evaluation failed - will retry next cycle", symbol)
        return None, checked_through_epoch, False


# ------------------------------------------------------------------- loop ---
def _market_hours_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 10) <= (now.hour, now.minute) <= (15, 35)


async def _handle_confirmed_signal(strategy: str, ot: str, sym: str, sig: dict,
                                    entry_fn: Callable[[str, str], Awaitable[dict]]) -> None:
    """Shared "mark signaled, log, attempt real entry" tail (added 23 Sep
    2026) - identical regardless of whether `sig` came from the WS-walk
    path or the REST single-snapshot path, so both share this instead of
    each re-implementing it."""
    w = _watchlist(strategy, ot)
    # Mark signaled BEFORE attempting entry - at most one attempt per
    # symbol per day even if entry_fn itself fails/skips, matching the
    # backtest's own "first qualifying candle only" design.
    async with _LOCK:
        it = w.items.get(sym)
        if it is None or it["signaled"]:
            return
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
        "strategy": strategy, "option_type": ot, "symbol": sym,
        **{k: v for k, v in sig.items() if k not in ("symbol", "candle_epoch")},
        "entry_result_status": (result or {}).get("status"), "entry_result_reason": (result or {}).get("reason"),
        "logged_at": datetime.now().isoformat(),
    })


def _split_ws_rest(cfg, pending: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Splits `pending` into (ws_pending, rest_pending) - a symbol goes to
    ws_pending only if cfg.BREAKOUT_USE_WS_CANDLES is on AND
    underlying_candle_feed reports it fresh (a thin/dead WS stream falls
    through to the REST path exactly as it always has via
    _fetch_5m_hybrid, this split is purely about which EVALUATION
    strategy - single-snapshot vs. full-history walk - a symbol gets, not
    a new source-of-truth decision)."""
    if not getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False):
        return [], pending
    import underlying_candle_feed
    stale_after = getattr(cfg, "BREAKOUT_WS_STALE_AFTER_SECONDS", 90)
    ws_pending, rest_pending = [], []
    for ot, sym in pending:
        (ws_pending if underlying_candle_feed.is_fresh(sym, stale_after) else rest_pending).append((ot, sym))
    return ws_pending, rest_pending


async def _ws_pass(strategy: str, cfg, entry_fn: Callable[[str, str], Awaitable[dict]],
                    pending_ws: list[tuple[str, str]]) -> None:
    """Evaluates EVERY WS-fresh not-yet-signaled symbol every cycle (added
    23 Sep 2026) - no BREAKOUT_SCAN_MAX_PER_CYCLE cap, and paced only on
    the (rare, after the first pass of the day) calls that actually hit
    Dhan for a fresh daily fetch - see _evaluate_ws_walk_sync's own
    docstring for the real incident (today, BANDHANBNK) this path exists
    to close, and _fetch_daily_cached's for why the common case is free."""
    loop = asyncio.get_running_loop()
    for ot, sym in pending_ws:
        direction = "bullish" if ot == "CE" else "bearish"
        w = _watchlist(strategy, ot)
        it = w.items.get(sym)
        if it is None or it["signaled"]:
            continue
        checked_through = it.get("checked_through_epoch")
        sig, new_checked_through, did_daily_fetch = await loop.run_in_executor(
            _SCAN_EXECUTOR, _evaluate_ws_walk_sync, sym, direction, cfg, checked_through)
        if did_daily_fetch:
            await asyncio.sleep(cfg.BREAKOUT_SCAN_PACE_SECONDS)
        if new_checked_through != checked_through:
            async with _LOCK:
                it = w.items.get(sym)
                if it is not None and not it["signaled"]:
                    it["checked_through_epoch"] = new_checked_through
                    await _persist(w)
        if sig is not None:
            await _handle_confirmed_signal(strategy, ot, sym, sig, entry_fn)


async def _scan_cycle(strategy: str, cfg, entry_fn: Callable[[str, str], Awaitable[dict]]) -> None:
    pending: list[tuple[str, str]] = []
    for option_type in ("CE", "PE"):
        w = _watchlist(strategy, option_type)
        async with _LOCK:
            _ensure_today_locked(w)
            pending.extend((option_type, sym) for sym, it in w.items.items() if not it["signaled"])

    ws_pending, rest_pending = _split_ws_rest(cfg, pending)
    await _ws_pass(strategy, cfg, entry_fn, ws_pending)

    rest_pending = _rotate_pending_from_cursor(strategy, rest_pending)
    loop = asyncio.get_running_loop()
    checked = 0
    last_examined: Optional[str] = None
    for ot, sym in rest_pending:
        if checked >= cfg.BREAKOUT_SCAN_MAX_PER_CYCLE:
            break
        checked += 1
        last_examined = sym
        direction = "bullish" if ot == "CE" else "bearish"
        sig = await loop.run_in_executor(_SCAN_EXECUTOR, _evaluate_signal_sync, sym, direction, cfg)
        await asyncio.sleep(cfg.BREAKOUT_SCAN_PACE_SECONDS)
        if sig is None:
            continue
        await _handle_confirmed_signal(strategy, ot, sym, sig, entry_fn)
    if last_examined is not None:
        _scan_cursor[strategy] = last_examined


_universe_seeded_for: dict[str, date] = {}
_universe_bucket_synced_count: dict[str, tuple[int, int]] = {}  # strategy -> (CE count, PE count) last synced


async def _ws_subscribe_best_effort(strategy: str, symbols: list[str]) -> None:
    if not symbols:
        return
    try:
        import underlying_candle_feed
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, underlying_candle_feed.subscribe, symbols)
    except Exception:  # noqa: BLE001
        logger.exception("%s: WS-subscribing the curated universe failed - scan cycle falls back to REST for it", strategy)


async def _seed_static_universe(strategy: str, cfg) -> None:
    """Curated-universe watchlist seeding, "static" source (added 21 Sep
    2026, user request - see underlying_candle_feed.py's own module
    docstring and Options/config.py's BREAKOUT_SEED_UNIVERSE_ENABLED
    block). Inert unless both cfg.BREAKOUT_SEED_UNIVERSE_ENABLED and
    cfg.BREAKOUT_UNIVERSE_SYMBOLS are set - byte-for-byte the existing
    alerts-only behavior otherwise. Runs at most once per calendar date
    per strategy: seeds BOTH the CE and PE watchlists with the same fixed
    symbol list via record_alert itself (ADDITIVE to, never a replacement
    for, real Chartink alerts still recording normally - record_alert's
    own idempotent `if sym not in w.items` check leaves an already-
    alerted symbol untouched), and - if cfg.BREAKOUT_USE_WS_CANDLES is
    also on - WS-subscribes the same list so _fetch_5m_hybrid has
    something fresh to read from the very first scan cycle of the day."""
    if not (getattr(cfg, "BREAKOUT_SEED_UNIVERSE_ENABLED", False) and getattr(cfg, "BREAKOUT_UNIVERSE_SYMBOLS", None)):
        return
    today = _today()
    if _universe_seeded_for.get(strategy) == today:
        return
    symbols = cfg.BREAKOUT_UNIVERSE_SYMBOLS
    for option_type in ("CE", "PE"):
        await record_alert(strategy, option_type, symbols)
    if getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False):
        await _ws_subscribe_best_effort(strategy, symbols)
    _universe_seeded_for[strategy] = today
    logger.warning("%s breakout-signal: seeded STATIC curated universe of %d symbols for %s (ws_candles=%s).",
                    strategy, len(symbols), today, getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False))


async def _sync_universe_bucket_source(strategy: str, cfg) -> None:
    """Curated-universe watchlist seeding, "universe_bucket" source
    (added 21 Sep 2026, user request - see universe_bucket.py's own
    module docstring for the rolling-3-trading-day bucket this reads
    from). Unlike _seed_static_universe above, this runs EVERY scan
    cycle, not once/day - a fresh alert posted to universe_bucket's own
    webhook should reach this package's watchlist within one scan
    interval, not wait for tomorrow's seed. Cheap even every cycle:
    record_alert's own idempotent check means re-recording an
    already-present symbol is a fast no-op; the WS-subscribe call is
    similarly idempotent (underlying_candle_feed.subscribe skips already-
    subscribed symbols). CE and PE are synced from universe_bucket's own
    separate CE/PE buckets, matching this package's own CE/PE watchlist
    split exactly - never crossed."""
    if not (getattr(cfg, "BREAKOUT_SEED_UNIVERSE_ENABLED", False) and getattr(cfg, "BREAKOUT_UNIVERSE_SOURCE", "static") == "universe_bucket"):
        return
    try:
        import universe_bucket
        ce_symbols = sorted(await universe_bucket.active_symbols("CE"))
        pe_symbols = sorted(await universe_bucket.active_symbols("PE"))
    except Exception:  # noqa: BLE001
        logger.exception("%s: reading universe_bucket failed - scan cycle continues with the existing watchlist unchanged", strategy)
        return
    if ce_symbols:
        await record_alert(strategy, "CE", ce_symbols)
    if pe_symbols:
        await record_alert(strategy, "PE", pe_symbols)
    if getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False):
        await _ws_subscribe_best_effort(strategy, sorted(set(ce_symbols) | set(pe_symbols)))
    counts = (len(ce_symbols), len(pe_symbols))
    if _universe_bucket_synced_count.get(strategy) != counts:
        _universe_bucket_synced_count[strategy] = counts
        logger.warning("%s breakout-signal: synced from universe_bucket - CE=%d PE=%d symbols (ws_candles=%s).",
                        strategy, counts[0], counts[1], getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False))


async def _maybe_seed_universe(strategy: str, cfg) -> None:
    """Dispatches to the configured curated-universe source - see
    Options/config.py's own BREAKOUT_UNIVERSE_SOURCE docstring for the
    two values ("static", the default/unchanged behavior, or
    "universe_bucket"). Both underlying functions are already no-ops
    unless their own preconditions are met, so calling both here
    unconditionally is safe regardless of which (if either) is active.
    Skipped entirely for a strategy the universe_dispatcher_loop below
    already owns - see _dispatcher_owned_strategies' own docstring for
    why running both at once would double-process every signal."""
    if strategy in _dispatcher_owned_strategies:
        return
    await _seed_static_universe(strategy, cfg)
    await _sync_universe_bucket_source(strategy, cfg)


# =============================================================== dispatcher ===
# Cross-package signal dispatcher for universe_bucket-sourced symbols
# (added 21 Sep 2026, user request: "since there would be just one source
# of signals now (breakout scanner), we... have to design to pass these
# signals to LUXURY and FUTURES properly so that no trade is left and is
# divided between 2 without dupes").
#
# THE PROBLEM WITH TWO INDEPENDENT SCANNERS ON ONE SHARED SOURCE: with
# BREAKOUT_UNIVERSE_SOURCE="universe_bucket" (see _sync_universe_bucket_
# source above), Luxury and Futures would each independently sync the
# SAME shared universe_bucket symbols into their OWN separate watchlists
# and independently evaluate the SAME underlying data against the SAME
# (currently identical) thresholds - meaning both would typically detect
# the identical signal within a cycle of each other and both attempt
# entry, relying entirely on cross_strategy_registry's atomic claim to
# stop a DUPLICATE real order. That part works. What it does NOT solve:
# each package's own watchlist marks a symbol "signaled" (never retried
# today) the INSTANT it makes its one entry attempt - including when that
# attempt fails ONLY because the other package's claim briefly won the
# race, even though that other package then rejects the trade itself
# (capacity, a gate, whatever) and releases the claim moments later. The
# first package never gets a second look, and neither does the trade -
# genuinely LOST to a race, not to either package's own real risk
# controls. That's the exact failure mode the user is naming.
#
# THE FIX: for any strategy this dispatcher owns (see
# _dispatcher_owned_strategies below), it - not that strategy's own
# signal_scanner_loop - is the ONLY thing that reads universe_bucket and
# evaluates signals for it (_maybe_seed_universe above no-ops for an
# owned strategy for exactly this reason: two evaluators racing on one
# signal is the bug, not the fix). Each signal is detected EXACTLY ONCE,
# then offered to each target package IN TURN, never in parallel - so
# there is no race between Luxury and Futures for a dispatcher-owned
# signal to begin with, and no possibility of both attempting the same
# symbol at once. If a target rejects (any status other than an
# entered/placed one), the NEXT target gets a real, un-raced attempt at
# the exact same signal before it's given up on for the day - closing
# the "lost to a race" gap directly. The starting target ROTATES per
# option_type (round-robin) so volume is DIVIDED across both packages
# over time rather than one always getting first (and therefore most)
# refusal of first look - not a strict 50/50 (that depends on each
# package's own capacity/gates on the day), but never structurally
# biased toward whichever package happens to be listed first.
#
# cross_strategy_registry still applies exactly as it always does -
# this dispatcher doesn't bypass or replace it, it just removes the ONE
# extra race (two packages BOTH independently discovering and attempting
# the same universe_bucket signal) that sat on top of it.

DISPATCHER_STRATEGY_NAME = "UniverseDispatcher"  # reuses _watchlist()'s existing per-strategy persistence/day-rollover machinery, unmodified, keyed under this synthetic "strategy" name
_TERMINAL_SUCCESS_STATUSES = ("entered", "amo_placed", "pending_confirmation")  # mirrors alert_bucket.ENTERED_STATUSES - kept local so the two modules stay independent, per this codebase's own "deliberately separate" convention
_CAPACITY_REJECTION_REASON = "duplicate_or_capacity_full"  # the exact reason string all 3 packages' own _process_one_entry use - see reserve_symbol's own call site in each trading_engine.py
_dispatcher_owned_strategies: set[str] = set()
_dispatch_turn: dict[str, int] = {"CE": 0, "PE": 0}


async def _dispatch_to_targets(option_type: str, sym: str, sig: dict, targets: list[tuple[str, Any, Callable[[str, str], Awaitable[dict]]]]) -> bool:
    """Tries each (strategy, cfg, entry_fn) in `targets`, SEQUENTIALLY
    (never concurrently - that would just reintroduce the race this whole
    dispatcher exists to remove), starting from a rotating position so
    the offer is DIVIDED across targets over time - see this section's
    own module-level docstring above. Returns True if EVERY target that
    declined did so specifically with _CAPACITY_REJECTION_REASON (i.e.
    this signal is a genuine capacity-backlog candidate - see
    _dispatch_backlog_cycle below); False if it succeeded OR if at least
    one decline was for a different reason (a gap-down delay, an RSI
    block, insufficient funds, ...) - those aren't capacity problems, so
    a freed slot elsewhere wouldn't fix them, and this signal should NOT
    be kept waiting on one."""
    n = len(targets)
    start = _dispatch_turn[option_type] % n
    order = targets[start:] + targets[:start]
    _dispatch_turn[option_type] = (_dispatch_turn[option_type] + 1) % n

    all_capacity_blocked = True
    for strategy, _cfg, entry_fn in order:
        try:
            result = await entry_fn(sym, option_type)
        except Exception:  # noqa: BLE001
            logger.exception("UniverseDispatcher: %s entry attempt for %s failed", strategy, sym)
            result = {"status": "error"}
        status = (result or {}).get("status")
        reason = (result or {}).get("reason")
        trade_history.append_jsonl("universe_dispatch", {
            "strategy": strategy, "option_type": option_type, "symbol": sym,
            **{k: v for k, v in sig.items() if k != "symbol"},
            "entry_result_status": status, "entry_result_reason": reason,
            "logged_at": datetime.now().isoformat(),
        })
        if status in _TERMINAL_SUCCESS_STATUSES:
            logger.warning("UniverseDispatcher: %s %s %s -> %s took it (%s)", option_type, sym, order, strategy, status)
            return False
        if reason != _CAPACITY_REJECTION_REASON:
            all_capacity_blocked = False
        logger.info("UniverseDispatcher: %s %s %s declined (%s/%s) - offering to the next target",
                    strategy, option_type, sym, status, reason)
    logger.warning("UniverseDispatcher: %s %s declined by EVERY target (%s)%s",
                    option_type, sym, [t[0] for t in targets],
                    " - all capacity-blocked, queuing for retry" if all_capacity_blocked else " - not traded today")
    return all_capacity_blocked


# ------------------------------------------------------ capacity backlog ---
# "Once any slot from LUXURY or FUTURES gets free... [a signal] which
# couldn't be filled earlier due to max capacity limit (stock still in
# momentum) should be attempted" (user request, 21 Sep 2026). Scoped
# NARROWLY to capacity - see _dispatch_to_targets' own docstring for why a
# non-capacity decline (funds, a gate, a delay) is never backlogged: a
# freed slot elsewhere doesn't fix those, so retrying would just spend a
# real order-placement attempt for no reason.
#
# "Still in momentum" is checked at EVERY retry, not just once, via
# reversal_filters.check_underlying_move_confirms_exit - the SAME real,
# already-backtested 0.10% confirmation threshold this codebase already
# trusts to decide "has the underlying genuinely moved against a CE/PE
# position", just pointed the other direction here (against the BREAKOUT
# rather than against an open position). A backlog entry whose underlying
# has moved back through its own original close by that much is dropped
# immediately - no point re-attempting entry into a breakout that's
# already reversed, capacity or not.
#
# In-memory only (resets on restart) - same accepted tradeoff as
# cross_strategy_registry/PositionStore's own reserved_symbols; a restart
# just means a currently-backlogged signal quietly stops being retried,
# never an incorrect trade.
_capacity_backlog: dict[str, list[dict]] = {"CE": [], "PE": []}  # FIFO, oldest first


def _momentum_still_valid_sync(symbol: str, direction: str, original_close: float, cfg) -> Optional[bool]:
    """Blocking. True = still worth retrying, False = reversed, drop from
    backlog, None = couldn't verify this cycle (data unavailable) - leave
    the entry untouched, try again next cycle rather than guessing either
    way. Reuses _fetch_5m_hybrid (WS-first/REST-fallback, same as a fresh
    signal check) for the latest COMPLETED candle's close as "current
    price" - never the still-forming candle, same discipline as every
    other price read in this module."""
    try:
        import reversal_filters
        candles = _fetch_5m_hybrid(symbol, cfg)
        closes, vols, ts = candles.get("close") or [], candles.get("volume") or [], candles.get("timestamp") or []
        now_epoch = time.time()
        rows = [(e, c) for e, c, v in zip(ts, closes, vols) if v and e + 300 <= now_epoch]
        if not rows:
            return None
        current_close = rows[-1][1]
        option_type = "CE" if direction == "bullish" else "PE"
        reversed_against = reversal_filters.check_underlying_move_confirms_exit(original_close, current_close, option_type)
        return not reversed_against
    except Exception:  # noqa: BLE001
        logger.exception("%s: momentum re-check failed - leaving backlog entry untouched this cycle", symbol)
        return None


async def _dispatch_backlog_cycle(cfg, targets: list[tuple[str, Any, Callable[[str, str], Awaitable[dict]]]]) -> None:
    """Drains the capacity backlog - called FIRST each dispatcher tick,
    BEFORE fresh signals get their own scan budget (user's own ordering:
    a freed slot should go to "the next signal OR one which couldn't be
    filled earlier" - backlog entries have already been waiting, so they
    get first look rather than being perpetually outrun by newer
    signals). Each entry gets re-tried via the SAME _dispatch_to_targets
    used for a fresh signal - if it succeeds now, it's removed; if it's
    still capacity-blocked, it stays queued (subject to the momentum
    check and max-age below); anything else (a genuine decline, momentum
    reversed, or aged out) drops it for good."""
    loop = asyncio.get_running_loop()
    now = datetime.now(IST)
    max_age = timedelta(minutes=cfg.BREAKOUT_CAPACITY_BACKLOG_MAX_AGE_MINUTES)
    for option_type in ("CE", "PE"):
        direction = "bullish" if option_type == "CE" else "bearish"
        remaining: list[dict] = []
        for entry in _capacity_backlog[option_type]:
            sym, sig, queued_at = entry["symbol"], entry["signal"], entry["queued_at"]
            if now - queued_at > max_age:
                logger.info("UniverseDispatcher backlog: %s %s aged out after %.0f min - dropped",
                            option_type, sym, (now - queued_at).total_seconds() / 60)
                continue
            still_valid = await loop.run_in_executor(_SCAN_EXECUTOR, _momentum_still_valid_sync, sym, direction, sig["close"], cfg)
            if still_valid is False:
                logger.info("UniverseDispatcher backlog: %s %s momentum reversed - dropped", option_type, sym)
                continue
            if still_valid is None:
                remaining.append(entry)  # inconclusive this cycle - keep waiting, don't spend a real attempt
                continue
            still_capacity_blocked = await _dispatch_to_targets(option_type, sym, sig, targets)
            if still_capacity_blocked:
                remaining.append(entry)  # entered nothing, still purely capacity-blocked - keep in backlog
            # else: either entered successfully, or declined for a different
            # reason this time (already logged by _dispatch_to_targets) -
            # either way, done with this entry.
        _capacity_backlog[option_type] = remaining


async def _handle_dispatcher_confirmed_signal(ot: str, sym: str, sig: dict,
                                               targets: list[tuple[str, Any, Callable[[str, str], Awaitable[dict]]]]) -> None:
    """Dispatcher-side counterpart of _handle_confirmed_signal (added 23
    Sep 2026) - marks signaled, logs, then offers the signal to targets
    via _dispatch_to_targets/backlog instead of a single entry_fn. Shared
    between the WS-walk pass and the REST pass below."""
    w = _watchlist(DISPATCHER_STRATEGY_NAME, ot)
    async with _LOCK:
        it = w.items.get(sym)
        if it is None or it["signaled"]:
            return
        it["signaled"], it["signaled_at"] = True, sig["detected_at"]
        await _persist(w)

    logger.warning(
        "BREAKOUT SIGNAL [UniverseDispatcher]: %s %s confirmed (range=%.2f%% body=%.2f%% relvol=%.2fx) - dispatching",
        ot, sym, sig["range_pct"], sig["body_pct"], sig["relative_volume"],
    )
    all_capacity_blocked = await _dispatch_to_targets(ot, sym, sig, targets)
    if all_capacity_blocked:
        _capacity_backlog[ot].append({"symbol": sym, "signal": sig, "queued_at": datetime.now(IST)})


async def _dispatch_ws_pass(cfg, targets: list[tuple[str, Any, Callable[[str, str], Awaitable[dict]]]],
                             pending_ws: list[tuple[str, str]]) -> None:
    """Dispatcher-side counterpart of _ws_pass - see that function's own
    docstring for the rationale (real incident, BANDHANBNK, today)."""
    loop = asyncio.get_running_loop()
    for ot, sym in pending_ws:
        direction = "bullish" if ot == "CE" else "bearish"
        w = _watchlist(DISPATCHER_STRATEGY_NAME, ot)
        it = w.items.get(sym)
        if it is None or it["signaled"]:
            continue
        checked_through = it.get("checked_through_epoch")
        sig, new_checked_through, did_daily_fetch = await loop.run_in_executor(
            _SCAN_EXECUTOR, _evaluate_ws_walk_sync, sym, direction, cfg, checked_through)
        if did_daily_fetch:
            await asyncio.sleep(cfg.BREAKOUT_SCAN_PACE_SECONDS)
        if new_checked_through != checked_through:
            async with _LOCK:
                it = w.items.get(sym)
                if it is not None and not it["signaled"]:
                    it["checked_through_epoch"] = new_checked_through
                    await _persist(w)
        if sig is not None:
            await _handle_dispatcher_confirmed_signal(ot, sym, sig, targets)


async def _dispatch_scan_cycle(cfg, targets: list[tuple[str, Any, Callable[[str, str], Awaitable[dict]]]]) -> None:
    """Same shape as _scan_cycle, but on a confirmed signal calls
    _dispatch_to_targets instead of a single package's own entry_fn.
    `cfg` supplies the SHARED scan-cadence/threshold settings (read from
    whichever package's config the dispatcher was started with - see
    universe_dispatcher_loop's own docstring: these are already identical
    across Options/Luxury/Futures as of 21 Sep 2026, so any one of the
    target configs is representative)."""
    pending: list[tuple[str, str]] = []
    for option_type in ("CE", "PE"):
        w = _watchlist(DISPATCHER_STRATEGY_NAME, option_type)
        async with _LOCK:
            _ensure_today_locked(w)
            pending.extend((option_type, sym) for sym, it in w.items.items() if not it["signaled"])

    ws_pending, rest_pending = _split_ws_rest(cfg, pending)
    await _dispatch_ws_pass(cfg, targets, ws_pending)

    rest_pending = _rotate_pending_from_cursor(DISPATCHER_STRATEGY_NAME, rest_pending)
    loop = asyncio.get_running_loop()
    checked = 0
    last_examined: Optional[str] = None
    for ot, sym in rest_pending:
        if checked >= cfg.BREAKOUT_SCAN_MAX_PER_CYCLE:
            break
        checked += 1
        last_examined = sym
        direction = "bullish" if ot == "CE" else "bearish"
        sig = await loop.run_in_executor(_SCAN_EXECUTOR, _evaluate_signal_sync, sym, direction, cfg)
        await asyncio.sleep(cfg.BREAKOUT_SCAN_PACE_SECONDS)
        if sig is None:
            continue
        await _handle_dispatcher_confirmed_signal(ot, sym, sig, targets)
    if last_examined is not None:
        _scan_cursor[DISPATCHER_STRATEGY_NAME] = last_examined


async def universe_dispatcher_loop(targets: list[tuple[str, Any, Callable[[str, str], Awaitable[dict]]]]) -> None:
    """Started ONCE for the whole process (not per-package - see main.py's
    own lifespan for where), given the list of (strategy, cfg, entry_fn)
    tuples it should divide universe_bucket-sourced signals between.
    Registers every listed strategy in _dispatcher_owned_strategies FIRST
    (before the loop starts touching anything) so _maybe_seed_universe
    correctly no-ops for all of them from the very first tick - see that
    function's own docstring for why running both paths at once would
    double-process every signal.

    Same unconditional-every-tick shape as signal_scanner_loop: day-
    rollover/post-close truncation for the dispatcher's OWN watchlist
    (_watchlist(DISPATCHER_STRATEGY_NAME, ...)), syncing fresh symbols
    from universe_bucket every tick (not just once/day - see
    _sync_universe_bucket_source's own docstring for why), then a scan
    cycle - all gated the same way this module's other loop already is."""
    if len(targets) < 2:
        logger.warning("UniverseDispatcher: started with only %d target(s) - dispatching still works "
                        "but there's nothing to divide between.", len(targets))
    _dispatcher_owned_strategies.update(strategy for strategy, _cfg, _fn in targets)
    primary_cfg = targets[0][1]
    logger.info("UniverseDispatcher started for targets=%s (interval=%ss, max %d/cycle).",
                [t[0] for t in targets], primary_cfg.BREAKOUT_SCAN_INTERVAL_SECONDS, primary_cfg.BREAKOUT_SCAN_MAX_PER_CYCLE)
    while True:
        try:
            for option_type in ("CE", "PE"):
                await _maybe_clear_after_close(_watchlist(DISPATCHER_STRATEGY_NAME, option_type), primary_cfg)
            try:
                import universe_bucket
                ce_symbols = sorted(await universe_bucket.active_symbols("CE"))
                pe_symbols = sorted(await universe_bucket.active_symbols("PE"))
            except Exception:  # noqa: BLE001
                logger.exception("UniverseDispatcher: reading universe_bucket failed - cycle continues with the existing watchlist unchanged")
                ce_symbols, pe_symbols = [], []
            if ce_symbols:
                await record_alert(DISPATCHER_STRATEGY_NAME, "CE", ce_symbols)
            if pe_symbols:
                await record_alert(DISPATCHER_STRATEGY_NAME, "PE", pe_symbols)
            # WS-subscribe (added 22 Sep 2026, user request: turning on
            # BREAKOUT_USE_WS_CANDLES should need a flag flip + restart,
            # never a fresh deployment). Without this, the dispatcher path
            # (Luxury/Futures' real watchlist source today) never called
            # _ws_subscribe_best_effort at all - only the older per-package
            # _sync_universe_bucket_source did, which no-ops for any
            # strategy this dispatcher owns (see this function's own
            # docstring) - so flipping the flag on would have silently done
            # nothing for the two strategies actually using this universe.
            if getattr(primary_cfg, "BREAKOUT_USE_WS_CANDLES", False):
                await _ws_subscribe_best_effort(DISPATCHER_STRATEGY_NAME, sorted(set(ce_symbols) | set(pe_symbols)))
            if primary_cfg.BREAKOUT_SIGNAL_ENABLED and _market_hours_now():
                await _dispatch_backlog_cycle(primary_cfg, targets)  # backlog gets first look at any freed capacity, see its own docstring
                await _dispatch_scan_cycle(primary_cfg, targets)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("UniverseDispatcher: scan cycle failed - will retry next interval")
        await asyncio.sleep(primary_cfg.BREAKOUT_SCAN_INTERVAL_SECONDS)


async def signal_scanner_loop(strategy: str, cfg, entry_fn: Callable[[str, str], Awaitable[dict]]) -> None:
    """Started once from the calling package's own lifespan. The day-
    rollover check (via _ensure_today_locked, inside _scan_cycle/_maybe_
    clear_after_close) and the post-close truncation both run every tick
    UNCONDITIONALLY - not gated behind BREAKOUT_SIGNAL_ENABLED or market
    hours - so the watchlist's own daily refresh behavior (see module
    docstring) is guaranteed regardless of whether scanning itself is on.
    Curated-universe seeding (_maybe_seed_universe, added 21 Sep 2026) is
    the same way - unconditional every tick, itself a no-op unless
    configured. Its "static" source re-seeds once/day (a no-op the rest
    of the day); its "universe_bucket" source (added later the same day)
    re-syncs from universe_bucket.py's own rolling bucket EVERY tick, by
    design - see _sync_universe_bucket_source's own docstring."""
    logger.info(
        "%s breakout-signal scanner started (enabled=%s, interval=%ss, max %d/cycle, market_end=%s, "
        "seed_universe=%s, ws_candles=%s).",
        strategy, cfg.BREAKOUT_SIGNAL_ENABLED, cfg.BREAKOUT_SCAN_INTERVAL_SECONDS,
        cfg.BREAKOUT_SCAN_MAX_PER_CYCLE, cfg.BREAKOUT_MARKET_END_TIME,
        getattr(cfg, "BREAKOUT_SEED_UNIVERSE_ENABLED", False), getattr(cfg, "BREAKOUT_USE_WS_CANDLES", False),
    )
    while True:
        try:
            for option_type in ("CE", "PE"):
                await _maybe_clear_after_close(_watchlist(strategy, option_type), cfg)
            await _maybe_seed_universe(strategy, cfg)
            if cfg.BREAKOUT_SIGNAL_ENABLED and _market_hours_now():
                await _scan_cycle(strategy, cfg, entry_fn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("%s: breakout-signal scan cycle failed - will retry next interval", strategy)
        await asyncio.sleep(cfg.BREAKOUT_SCAN_INTERVAL_SECONDS)
