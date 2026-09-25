"""
WS-based local multi-timeframe candle reconstruction for Swing's regime/
Supertrend signals (added 23 Sep 2026, user request, direct follow-up to
Options/Luxury/Futures' own underlying_candle_feed.py rearchitecture the
same day - see that module's docstring for the original design this one
is modeled on, and Swing/config.py's USE_WS_CANDLES for the flag).

THE PROBLEM THIS SOLVES: Swing/signals.py's _fetch_regime_state_once and
_fetch_supertrend_state_once each call Options.dhan_client's
fetch_continuous_intraday over REST EVERY refresh cycle - regime every
config.REGIME_REFRESH_SECONDS(60) for 2 timeframes (5m+15m), Supertrend
every config.SUPERTREND_REFRESH_SECONDS(15) for up to 2 timeframes (5m+
15m, v2's combined strategy). That is already more REST calls per symbol
per minute than any other package in this codebase makes, sharing the
SAME account-wide Dhan rate limit (DH-904) Options/Futures/Luxury/
structure_break.py all draw from - confirmed live (23 Sep 2026, right
after a restart): continuous DH-904 failures on ASHOKLEY/NATURALGAS
regime+Supertrend fetches, one every few seconds, for the entire
restart-warm-up window.

THE FIX: same idea as underlying_candle_feed.py - subscribe each
watchlist symbol's underlying instrument on the SAME shared market-data
WebSocket connection in Quote mode, reconstruct 5-min OHLCV bars locally
from the resulting tick stream, and serve regime/Supertrend's REST calls
from that local series instead once it has enough history. Checking a
symbol's signal then costs ZERO REST calls (an in-memory list read) on
every refresh where the local series is fresh and warm enough - polling
remains a correctness fallback, not replaced, exactly like the other
three packages' own hybrid design.

WHY THIS IS A SEPARATE MODULE, NOT A REUSE OF underlying_candle_feed.py:
  1. NSE-only there (subscribe_equity_quote) - Swing needs BOTH NSE
     equities (ASHOKLEY) AND MCX futures contracts (COPPER, NATURALGAS),
     which is a materially different subscription path (Options/
     dhan_client.py's subscribe_mcx_quote, added alongside this module -
     see its own docstring for the real cross-segment security_id
     collision this must avoid).
  2. Much deeper history needed. Breakout_signal.py's own WS-candle use
     only ever needs BREAKOUT_LOOKBACK_CANDLES(10) 5-min bars - Swing's
     200-period EMA needs 200 bars on the 15-min timeframe ALONE, which
     is 600 5-min bars (200 * 3) at minimum, comfortably more with margin
     for a stable reading (matching Swing/config.py's own REGIME_EMA_
     LOOKBACK_DAYS=45-calendar-day REST override for exactly the same
     reason). MAX_BARS_KEPT below is sized accordingly - a different,
     much larger number than the other module's 120.
  3. An MCX symbol's underlying instrument is a specific FUTURES CONTRACT
     that rolls monthly (Swing/signals.py's own get_mcx_futures_contract/
     _underlying_reference) - a genuinely different instrument each roll,
     not a continuous spot price. This module resets a symbol's
     accumulated bars on a detected roll and persists to a security_id-
     namespaced file (see _persist_path) specifically so a plain restart
     never accidentally splices one contract's price series onto
     another's - REST's own fetch_continuous_intraday never does this
     either (it always fetches whatever the CURRENT contract's own
     history is, nothing from a retired one), so this preserves parity
     with what the REST path already does rather than introducing a new,
     WS-only discontinuity-across-rolls bug.

BAR RECONSTRUCTION: identical algorithm to underlying_candle_feed.py's
own _update_bar/_on_tick (Quote-mode cumulative-volume bucketing, mid-
day-subscribe volume-baseline fix, continuous-across-day-rollover
history) - see that module's docstring for the full reasoning; not
re-derived here since it's the same proven logic, just parameterized
differently (MAX_BARS_KEPT, persistence path, dual NSE/MCX subscribe).

MULTI-TIMEFRAME: only 5-min bars are ever built directly from ticks.
Every other timeframe Swing needs (currently only 15-min) is derived by
resampling the 5-min series on read (_resample) - simpler and less state
to get wrong than maintaining N independently-bucketed bar streams, and
cheap enough to redo on every call given how infrequently regime/
Supertrend actually refresh (15-60s).

FRESHNESS / FALLBACK / FLAG-GATED: identical discipline to
underlying_candle_feed.py - is_fresh() gates the caller's own decision to
trust this module at all, and Swing/signals.py's hybrid fetch helper
(_get_intraday_series) ALSO requires the resampled series to already
have enough bars for that specific call's own minimum before accepting
it, falling back to the existing REST path otherwise. Inert unless
Swing/config.py's USE_WS_CANDLES is true (default false).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("swing_candle_feed")

IST = ZoneInfo("Asia/Kolkata")
HISTORY_DIR = Path("history")

BASE_INTERVAL_MINUTES = 5

# 2600 5-min bars ~ 35 trading days at ~75 bars/session - comfortably
# more than the 600 bars a 200-period EMA on the 15-min timeframe needs
# (200 * 3), matching Swing/config.py's own REGIME_EMA_LOOKBACK_DAYS(45)
# REST override's intent (warm margin, not a bare minimum).
MAX_BARS_KEPT = 2600

# Calendar days (not trading days) to look back when restoring from disk
# on a restart - comfortably covers MAX_BARS_KEPT's ~35 trading days
# including weekends/holidays in between.
DISK_RESTORE_LOOKBACK_DAYS = 55


class _SymbolState:
    __slots__ = ("security_id", "day", "current_bar_start", "bar_open", "bar_high", "bar_low",
                 "bar_close", "cum_volume_at_bar_start", "cum_volume_now", "last_tick_at", "bars",
                 "last_persisted_candle_start")

    def __init__(self, security_id: str) -> None:
        self.security_id = security_id
        self.day: Optional[date] = None
        self.current_bar_start: Optional[datetime] = None
        self.bar_open: Optional[float] = None
        self.bar_high: Optional[float] = None
        self.bar_low: Optional[float] = None
        self.bar_close: Optional[float] = None
        self.cum_volume_at_bar_start: float = 0.0
        self.cum_volume_now: float = 0.0
        self.last_tick_at: Optional[datetime] = None
        self.bars: list[dict] = []  # completed 5-min bars only, oldest first
        # The candle_start of the most recently PERSISTED bar (from disk on
        # restore, or from this process's own _on_tick since) - added 25
        # Sep 2026, real incident: after a restart, current_bar_start above
        # starts fresh at None (deliberately - it's the CURRENTLY FORMING
        # bar, unrelated to history), so the first live ticks after restart
        # re-form and re-COMPLETE whatever 5-min window they land in, even
        # when that exact candle_start was already completed and persisted
        # by the process instance that just died. Confirmed live: COPPER/
        # NATURALGAS (MCX's long session means far more restarts land
        # mid-session than for NSE-hours-only symbols) had the same
        # candle_start persisted 2-3x with different partial OHLC each
        # time. This field is checked before every persist/append below so
        # a candle_start already on disk can never be written again in the
        # same process's lifetime, regardless of what current_bar_start's
        # own (unrelated) bootstrap does.
        self.last_persisted_candle_start: Optional[datetime] = None


_lock = threading.Lock()
_state: dict[str, _SymbolState] = {}
# symbol -> (security_id, exchange_segment) actually subscribed right now -
# lets ensure_subscribed() detect both a first-time subscribe and an MCX
# contract roll (security_id changed under the same symbol) idempotently.
_subscribed_ref: dict[str, tuple[str, str]] = {}
_tick_subscriber_registered = False


# --------------------------------------------------------------------------- #
# Disk reconciliation - same append-only JSONL convention as
# underlying_candle_feed.py, but the filename includes security_id (see
# module docstring point 3) so a restore after an MCX contract roll can
# never load a retired contract's bars into the new one's series.
# --------------------------------------------------------------------------- #
def _persist_path(symbol: str, security_id: str, day: date) -> Path:
    return HISTORY_DIR / f"{day.isoformat()}_swing_candles_{symbol}_{security_id}.log"


def _persist_bar(symbol: str, security_id: str, day: date, bar: dict) -> None:
    """Best-effort, deliberately outside `_lock` - see underlying_candle_
    feed.py's own _persist_bar docstring for why a slow disk write must
    never block tick processing for every other subscribed symbol."""
    try:
        HISTORY_DIR.mkdir(exist_ok=True)
        row = {**bar, "candle_start": bar["candle_start"].isoformat()}
        with open(_persist_path(symbol, security_id, day), "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception(
            "swing_candle_feed: failed to persist a completed bar for %s to disk - in-memory state "
            "is still correct, but a restart before the next successful write would lose it", symbol,
        )


def _load_persisted_bars(symbol: str, security_id: str, as_of: date, lookback_days: int) -> list[dict]:
    """Dedupes by candle_start (added 25 Sep 2026, real incident - see
    last_persisted_candle_start's docstring in _SymbolState) - a restart
    landing mid-candle could persist the SAME candle_start more than once
    across process instances before this was fixed at the write side, and
    old on-disk files from before that fix still have real duplicate
    lines. Reads day files in chronological (oldest-first) order and a
    plain dict write naturally keeps the LAST occurrence of any repeated
    candle_start - confirmed live this is also the most-complete version
    (each successive persist of the "same" candle showed progressively
    more accurate high/low/close as later ticks fed it), so this is a
    real repair, not an arbitrary pick, for old files - and a pure no-op
    for files with no duplicates."""
    by_candle_start: dict[datetime, dict] = {}
    for i in range(lookback_days, -1, -1):
        day = as_of - timedelta(days=i)
        path = _persist_path(symbol, security_id, day)
        if not path.exists():
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    row["candle_start"] = datetime.fromisoformat(row["candle_start"])
                    by_candle_start[row["candle_start"]] = row
        except Exception:  # noqa: BLE001
            logger.exception(
                "swing_candle_feed: failed to restore persisted bars for %s from %s - skipping that "
                "file, reconstruction continues from live ticks only", symbol, path,
            )
    bars = sorted(by_candle_start.values(), key=lambda b: b["candle_start"])
    return bars[-MAX_BARS_KEPT:]


def _candle_start_for(t: datetime, interval_minutes: int) -> datetime:
    floored_minute = t.minute - (t.minute % interval_minutes)
    return t.replace(minute=floored_minute, second=0, microsecond=0)


def _update_bar(st: _SymbolState, ltp: float, cum_volume: float, t: datetime) -> Optional[dict]:
    """Identical algorithm to underlying_candle_feed.py's own _update_bar -
    see that module's docstring for the mid-day-subscribe volume-baseline
    reasoning, not re-derived here.

    Real incident (25 Sep 2026): COPPER's very first tick of the day
    (05:30 IST, pre-market - MCX doesn't open COPPER until ~09:00) arrived
    with ltp=0 (a malformed/keepalive packet, not a real trade) and, since
    it landed right after the day-rollover reset below set current_bar_
    start=None, became that bar's bootstrap price - producing a real,
    persisted open=high=low=close=0.0 bar. That one corrupted bar then
    fed structure_break.py's adaptive EMA/ATR bands for the 5m and 15m
    legs (via Swing/signals.py's own ws_candles_fn hook), flipping both to
    a false BEARISH regime while a clean REST-only read of the same
    period showed BULLISH on all three timeframes - directly caused
    combined==0 (no 3-way agreement) when it should have been +1. Fix:
    reject ltp<=0 outright, before it can seed OR update any bar - a
    zero/negative price is never a real trade, on any exchange this feed
    subscribes to."""
    if ltp <= 0:
        return None
    bar_start = _candle_start_for(t, BASE_INTERVAL_MINUTES)
    completed = None
    if st.current_bar_start is None:
        st.current_bar_start = bar_start
        st.bar_open = st.bar_high = st.bar_low = st.bar_close = ltp
        st.cum_volume_at_bar_start = cum_volume
    elif bar_start != st.current_bar_start:
        completed = {
            "candle_start": st.current_bar_start, "open": st.bar_open, "high": st.bar_high,
            "low": st.bar_low, "close": st.bar_close,
            "volume": max(0.0, st.cum_volume_now - st.cum_volume_at_bar_start),
        }
        st.current_bar_start = bar_start
        st.bar_open = st.bar_high = st.bar_low = st.bar_close = ltp
        st.cum_volume_at_bar_start = st.cum_volume_now
    else:
        st.bar_high = max(st.bar_high, ltp)
        st.bar_low = min(st.bar_low, ltp)
        st.bar_close = ltp
    st.cum_volume_now = cum_volume
    return completed


def _on_tick(underlying_symbol: str, ltp: float, cum_volume: float, t: datetime) -> None:
    today = t.date()
    completed = None
    with _lock:
        st = _state.get(underlying_symbol)
        if st is None:
            return  # not (or no longer) subscribed under this symbol - ignore stray tick
        if st.day != today:
            # Day rollover - st.bars is deliberately left untouched (see
            # underlying_candle_feed.py's own _on_tick comment: a real
            # chart's 5-min series does not reset at midnight, only the
            # intraday cumulative-volume baseline does, per this
            # codebase's standing continuous-candles rule).
            st.day = today
            st.current_bar_start = None
            st.cum_volume_at_bar_start = 0.0
            st.cum_volume_now = 0.0
        completed = _update_bar(st, ltp, cum_volume, t)
        if completed is not None:
            # Guard against re-completing a candle_start this process (or a
            # prior instance, restored via ensure_subscribed) already
            # persisted - see last_persisted_candle_start's own docstring.
            # current_bar_start's fresh-None bootstrap after a restart is
            # deliberately left alone (it's what makes the FIRST tick after
            # subscribe start a bar at all) - this check is what stops that
            # bootstrap from ever re-writing a bar that's already on disk.
            if st.last_persisted_candle_start is not None and completed["candle_start"] <= st.last_persisted_candle_start:
                completed = None
            else:
                st.bars.append(completed)
                if len(st.bars) > MAX_BARS_KEPT:
                    del st.bars[: len(st.bars) - MAX_BARS_KEPT]
                st.last_persisted_candle_start = completed["candle_start"]
        st.last_tick_at = t
        security_id = st.security_id
    if completed is not None:
        _persist_bar(underlying_symbol, security_id, today, completed)


def ensure_subscribed(symbol: str, security_id: str, exchange_segment: str) -> None:
    """Idempotent - safe to call on every regime/Supertrend fetch cycle
    (that's the intended call pattern, via Swing/signals.py's
    _underlying_reference). A no-op if already correctly subscribed under
    this exact (security_id, exchange_segment) pair; detects and handles
    both a first-time subscribe and an MCX contract roll (security_id
    changed under the same symbol name) transparently.

    Blocking (WS subscribe calls + a disk read on first subscribe) - only
    ever called from an executor thread via _underlying_reference, never
    from the asyncio loop or the WS feed's own tick-callback thread."""
    from Options.dhan_client import dhan_wrapper
    global _tick_subscriber_registered  # noqa: PLW0603

    with _lock:
        prev = _subscribed_ref.get(symbol)
        if prev == (security_id, exchange_segment):
            return
        _subscribed_ref[symbol] = (security_id, exchange_segment)
        rolled = prev is not None and prev[0] != security_id
        # Unconditional fresh state either way (first-time subscribe or a
        # roll) - the only path that reaches here with prev not None is a
        # genuine change (the early return above already caught "already
        # correctly subscribed"), so whatever was accumulated under the
        # old reference must not be treated as continuous with this one
        # (see module docstring point 3).
        _state[symbol] = _SymbolState(security_id)

    if not _tick_subscriber_registered:
        dhan_wrapper.add_quote_tick_subscriber(_on_tick)
        _tick_subscriber_registered = True

    if rolled:
        old_security_id, old_segment = prev
        try:
            if old_segment == "MCX_COMM":
                dhan_wrapper.unsubscribe_mcx_quote(symbol, old_security_id)
            elif old_segment == "IDX_I":
                dhan_wrapper.unsubscribe_index_quote(symbol, old_security_id)
            else:
                dhan_wrapper.unsubscribe_equity_quote(symbol)
        except Exception:  # noqa: BLE001
            logger.exception(
                "swing_candle_feed: failed to unsubscribe %s's retired contract %s - continuing "
                "to subscribe the new one regardless", symbol, old_security_id,
            )
        logger.info(
            "swing_candle_feed: %s's underlying instrument changed (%s -> %s, likely an MCX contract "
            "roll) - local bar history reset, re-warming from scratch under the new contract",
            symbol, old_security_id, security_id,
        )

    today = datetime.now(IST).date()
    bars = _load_persisted_bars(symbol, security_id, today, DISK_RESTORE_LOOKBACK_DAYS)
    if bars:
        with _lock:
            st = _state.get(symbol)
            if st is not None and not st.bars:
                st.bars = bars
                # Seed the re-persist guard from the restored history's own
                # last entry - see last_persisted_candle_start's docstring.
                # Without this, a restart's first live ticks would treat
                # this exact candle_start as fair game to re-complete.
                st.last_persisted_candle_start = bars[-1]["candle_start"]
        logger.info(
            "swing_candle_feed: restored %d persisted bar(s) for %s (security_id=%s) from disk - "
            "reconciliation after a possible restart, not starting cold", len(bars), symbol, security_id,
        )

    try:
        if exchange_segment == "MCX_COMM":
            dhan_wrapper.subscribe_mcx_quote(symbol, security_id)
        elif exchange_segment == "IDX_I":
            dhan_wrapper.subscribe_index_quote(symbol, security_id)
        else:
            dhan_wrapper.subscribe_equity_quote(symbol)
    except Exception:  # noqa: BLE001
        logger.exception("swing_candle_feed: failed to WS-subscribe %s - it will stay on REST fallback", symbol)


def is_fresh(symbol: str, max_age_seconds: float) -> bool:
    with _lock:
        st = _state.get(symbol)
        if st is None or st.last_tick_at is None:
            return False
        age = (datetime.now(IST) - st.last_tick_at).total_seconds()
        return age <= max_age_seconds


def _resample(bars: list[dict], interval_minutes: int) -> list[dict]:
    """Aggregates completed BASE_INTERVAL_MINUTES(5) bars into
    interval_minutes buckets (must be a positive multiple of 5). A bucket
    is only emitted if it's provably complete - either it already has the
    full expected number of constituent 5-min bars, or at least one later
    bar from a DIFFERENT bucket exists (proving this one has fully
    elapsed) - so the still-forming trailing bucket is never included,
    matching this codebase's standing "never return a still-forming
    candle" discipline."""
    if interval_minutes == BASE_INTERVAL_MINUTES:
        return bars
    if interval_minutes % BASE_INTERVAL_MINUTES != 0:
        raise ValueError(f"interval_minutes must be a multiple of {BASE_INTERVAL_MINUTES}, got {interval_minutes}")
    expected = interval_minutes // BASE_INTERVAL_MINUTES

    groups: dict[datetime, list[dict]] = {}
    order: list[datetime] = []
    for b in bars:
        key = _candle_start_for(b["candle_start"], interval_minutes)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)

    out: list[dict] = []
    for i, key in enumerate(order):
        members = groups[key]
        is_last_group = i == len(order) - 1
        if len(members) < expected and is_last_group:
            continue  # still-forming - not enough constituent bars yet, and nothing later proves it's done
        out.append({
            "candle_start": key,
            "open": members[0]["open"],
            "high": max(m["high"] for m in members),
            "low": min(m["low"] for m in members),
            "close": members[-1]["close"],
            "volume": sum(m["volume"] for m in members),
        })
    return out


def get_candles_dict(symbol: str, interval_minutes: int) -> dict:
    """Returns the SAME Dhan dict-of-lists shape (timestamp/open/high/low/
    close/volume, epoch seconds) fetch_continuous_intraday's own return
    value uses - a drop-in substitute so Swing/signals.py's hybrid fetch
    helper needs no special-casing beyond picking which function to call.
    Only ever returns COMPLETED bars at the requested timeframe."""
    with _lock:
        st = _state.get(symbol)
        bars = list(st.bars) if st is not None else []
    if not bars:
        return {}
    resampled = _resample(bars, interval_minutes)
    if not resampled:
        return {}
    return {
        "timestamp": [b["candle_start"].timestamp() for b in resampled],
        "open": [b["open"] for b in resampled], "high": [b["high"] for b in resampled],
        "low": [b["low"] for b in resampled], "close": [b["close"] for b in resampled],
        "volume": [b["volume"] for b in resampled],
    }


def snapshot() -> dict:
    """Read-only observability view for a /swing/debug/candle-feed
    endpoint - per-symbol subscribed reference, last-tick age, and
    5-min/resampled-15-min bar counts. Not on any trading decision path."""
    now = datetime.now(IST)
    out = {}
    with _lock:
        for sym, st in _state.items():
            age = (now - st.last_tick_at).total_seconds() if st.last_tick_at else None
            bars_5m = list(st.bars)
            out[sym] = {
                "security_id": st.security_id,
                "bars_5m": len(bars_5m),
                "bars_15m": len(_resample(bars_5m, 15)),
                "last_tick_age_seconds": age,
                "day": str(st.day),
            }
    return out
