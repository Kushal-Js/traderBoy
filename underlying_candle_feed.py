"""
WebSocket-based local 5-min candle reconstruction for underlying equities
(added 21 Sep 2026, user request: "how would this work if we scanned every
F&O stock" -> the answer was "not on REST polling alone" - see trading-
skills' designs/all-fno-universe-breakout-signal-15day-backtest.md's own
"what's still open" section and the conversation that followed it).

THE PROBLEM THIS SOLVES: breakout_signal.py's real signal check
(_evaluate_signal_sync) fetches a symbol's 5-min candles via a REST call
(Options.dhan_client's intraday_minute_data) EVERY time it's checked. That
is fine for a watchlist of a few dozen alerted symbols/day, but does not
scale to a curated multi-symbol universe: BREAKOUT_SCAN_MAX_PER_CYCLE(10)
symbols/BREAKOUT_SCAN_INTERVAL_SECONDS(60) means a ~100-symbol watchlist
takes ~10 minutes for one full pass, and a naive push to check faster
collides with Dhan's real per-account rate limit (DH-904, confirmed live
21 Sep 2026 - see incidents/2026-09-21-local-backtest-dhan-session-
collision.md) shared with the live bot's OWN real-time order/LTP traffic.

THE FIX: subscribe each watchlist symbol's underlying equity on the
SAME shared market-data WebSocket connection Options/Luxury/Futures
already use for option LTP (Options.dhan_client.dhan_wrapper), in Quote
mode (volume-bearing, unlike option's Ticker mode - see dhan_client.py's
subscribe_equity_quote), and reconstruct 5-min OHLCV bars LOCALLY from the
resulting tick stream instead of re-fetching them from Dhan on every scan
cycle. Checking a symbol's signal then costs ZERO REST calls (an in-memory
list read) instead of one REST round-trip - the scan-cadence bottleneck
this was built to remove.

BAR RECONSTRUCTION: a Quote-mode tick carries LTP + the day's CUMULATIVE
volume-so-far (not a per-tick delta) - confirmed from the vendored
dhanhq SDK's own process_quote/process_full (both set a "volume" key,
Ticker/process_ticker never does). Each tick is bucketed into the 5-min
window it falls in (`_candle_start_for` - plain wall-clock flooring is
correct here because NSE's session opens exactly on a 5-min boundary,
09:15, so no session-relative offset math is needed); a bar's own volume
is (cumulative volume at the bar's last tick) - (cumulative volume at the
tick immediately BEFORE the bar started) - i.e. the delta across the
window, matching what a real 5-min REST candle's own volume field means.
`get_candles()` NEVER returns the still-forming current bar - same "drop a
still-forming candle" discipline breakout_signal.py's own REST path
already uses (_evaluate_signal_sync's `e + 300 <= now_epoch` filter) - a
partial bar would read as an artificially small/incomplete range and could
misfire a consolidation-range or relative-volume check.

CORRECTNESS: the bucketing function itself (`_update_bar`) is a pure,
independently-testable function of (state, ltp, cum_volume, tick_time) -
see backtest_ws_candle_reconstruction_parity.py, which replays a real
day's 1-min REST data as a synthetic tick stream through this exact
function and compares the resulting 5-min bars against Dhan's own real
5-min REST candles for the same symbols/day. Run that BEFORE trusting this
module's output for any real signal.

CAUGHT LIVE, NOT BY THAT REPLAY (22 Sep 2026): the mid-day-subscribe
volume bug fixed in `_update_bar` (see its own comment) was invisible to
backtest_ws_candle_reconstruction_parity.py by construction - a REST
replay always starts from the beginning of a symbol's day, so
`cum_volume` is naturally near 0 at the first synthetic tick either way,
the exact condition under which the old bug's assumption happened to be
correct. Only a genuine live dry-run (subscribing mid-session, as
production actually does) could have caught it, and did - see
tests/test_underlying_candle_feed.py::test_mid_day_subscribe_first_bar_
volume_excludes_pre_subscription_volume for a regression test that
specifically covers the case the REST-replay method structurally cannot.
`BREAKOUT_USE_WS_CANDLES` stays off until a fresh live dry-run confirms
this fix and open-price accuracy (a separate, still-open gap - see
trading-skills' incidents/designs for the live parity numbers).

FRESHNESS / FALLBACK: `is_fresh(symbol)` reports whether a tick for this
symbol has arrived within BREAKOUT_WS_STALE_AFTER_SECONDS (per-package
config, read by breakout_signal.py's own hybrid fetch path) - a thin/dead
WS stream (session drop, symbol not actually subscribed yet, a
Ticker-only fallback silently substituted) reads as NOT fresh, and the
caller falls back to the existing REST fetch for that symbol on that
cycle. This module is BY DESIGN never the sole source of truth - polling
remains a correctness fallback, not replaced, per the user's own framing
("WebSocket-based rearchitecture and polling as a fallback only").

FLAG-GATED, NOTHING LIVE YET: this module has no effect unless a package's
own `cfg.BREAKOUT_USE_WS_CANDLES` is true (default false for all three -
Options/Luxury/Futures config.py) AND that package's own curated-universe
seeding is configured. Building + a correctness backtest come first; going
live is a separate, explicit, later decision per [[feedback-live-trading-
safety]] - a backtest number alone is never itself authorization.

THREAD SAFETY: ticks arrive on the MarketFeed's own background thread
(not the asyncio event loop - see dhan_client.py's _on_market_tick), so
all per-symbol state lives behind one plain threading.Lock (cheap;
Python's GIL already serializes the dict/list mutations involved, but the
lock makes the multi-step "read state, decide if a new bar started,
mutate state" sequence atomic against a tick landing mid-update from the
same background thread re-entering - can't happen with a single feed
thread today, but costs nothing to make explicit rather than relying on
that single-thread assumption forever).

DISK RECONCILIATION (added 22 Sep 2026, real incident): this module used
to be pure in-memory, the one store in this codebase that wasn't - every
other one (position_store, universe_bucket, breakout_signal's own
watchlists) already persists to history/. An unplanned mid-session
restart wiped an already-running live dry-run's entire accumulated state,
and separately made the automated daily parity check report a false
"recon_bar_count: 0" for every symbol - indistinguishable from a
genuinely broken feed, when the real cause was just an ordinary restart.
Every completed bar is now appended to `history/<date>_underlying_
candles_<symbol>.log` (JSONL, matching this codebase's own webhook_
alerts.log/breakout_signals.log/real_trades.log convention) - see
_persist_bar (write, outside `_lock`, best-effort) and
_restore_from_disk (read, called from `subscribe()` before the first
live tick for a symbol can arrive in a fresh process, covering the last
3 calendar days so MAX_BARS_KEPT's own multi-session continuity survives
a restart instead of starting cold).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("underlying_candle_feed")

IST = ZoneInfo("Asia/Kolkata")
HISTORY_DIR = Path("history")

# Bars kept per symbol - comfortably more than any real signal check's own
# lookback (BREAKOUT_LOOKBACK_CANDLES=5, lowered from 10 on 23 Sep 2026 -
# see Options/config.py's own comment) plus the 20d/50d DAILY checks
# (which stay on REST regardless - see module docstring; only the 5-min
# intraday series is WS-sourced). 120 bars = 10 trading hours = TWO full
# sessions genuinely kept continuous across the day boundary (see _on_tick's
# own comment, corrected 22 Sep 2026 - this used to wipe bars at rollover,
# which was wrong per the standing continuous-candles rule).
MAX_BARS_KEPT = 120


class _SymbolState:
    __slots__ = ("day", "current_bar_start", "bar_open", "bar_high", "bar_low", "bar_close",
                 "cum_volume_at_bar_start", "cum_volume_now", "last_tick_at", "bars")

    def __init__(self) -> None:
        self.day: Optional[date] = None
        self.current_bar_start: Optional[datetime] = None
        self.bar_open: Optional[float] = None
        self.bar_high: Optional[float] = None
        self.bar_low: Optional[float] = None
        self.bar_close: Optional[float] = None
        self.cum_volume_at_bar_start: float = 0.0
        self.cum_volume_now: float = 0.0
        self.last_tick_at: Optional[datetime] = None
        self.bars: list[dict] = []  # completed bars only, oldest first


_lock = threading.Lock()
_state: dict[str, _SymbolState] = {}
_subscribed: set[str] = set()
_tick_subscriber_registered = False

# --------------------------------------------------------------------------- #
# Disk reconciliation (added 22 Sep 2026, real incident: an unplanned restart
# mid-session wiped this module's in-memory state entirely, silently zeroing
# out an already-running live dry-run AND the automated daily parity check's
# own comparison - "recon_bar_count: 0" for every symbol, same failure shape
# as a genuinely broken feed, when the actual cause was just a restart no
# different from any other this bot already survives cleanly for every OTHER
# store (position_store, universe_bucket, breakout_signal's own watchlists -
# all persist to `history/` already). This module was the one exception,
# purely in-memory. Every completed bar is now appended to a per-symbol,
# per-day JSONL log under history/ (matching this codebase's own established
# append-only convention - webhook_alerts.log, breakout_signals.log,
# real_trades.log all use the identical shape), and `subscribe()` restores
# from those logs before the first live tick can arrive, so a restart mid-
# session picks up exactly where it left off instead of starting cold.
# --------------------------------------------------------------------------- #
def _persist_path(symbol: str, day: date) -> Path:
    return HISTORY_DIR / f"{day.isoformat()}_underlying_candles_{symbol}.log"


def _persist_bar(symbol: str, day: date, bar: dict) -> None:
    """Appends one completed bar to disk - called once per symbol per
    completed 5-min bar (a few times an hour per symbol, not per tick),
    and deliberately OUTSIDE `_lock` (see _on_tick) so a slow disk write
    never blocks tick processing for every other subscribed symbol.
    Best-effort: a write failure is logged, not raised - the in-memory
    state (what every live signal check actually reads) is unaffected
    either way, this only protects against a FUTURE restart losing this
    one bar."""
    try:
        HISTORY_DIR.mkdir(exist_ok=True)
        row = {**bar, "candle_start": bar["candle_start"].isoformat()}
        with open(_persist_path(symbol, day), "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception(
            "underlying_candle_feed: failed to persist a completed bar for %s to disk - in-memory "
            "state is still correct, but a restart before the next successful write would lose it", symbol,
        )


def _load_persisted_bars(symbol: str, as_of: date, lookback_days: int = 3) -> list[dict]:
    """Restores completed bars from disk across the last `lookback_days`
    calendar days (including `as_of` itself) - so MAX_BARS_KEPT's own
    "two full sessions continuous" intent survives a restart instead of
    starting from a cold, empty state. 3 days comfortably covers a
    weekend gap (Friday + Monday) without needing trading-day-aware
    logic - a missing file for any one day (weekend, holiday, or a day
    before this reconciliation feature existed) is silently skipped, not
    an error. Best-effort: a corrupt line/file is skipped and logged,
    never fatal to the restore as a whole."""
    bars: list[dict] = []
    for i in range(lookback_days, -1, -1):
        day = as_of - timedelta(days=i)
        path = _persist_path(symbol, day)
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
                    bars.append(row)
        except Exception:  # noqa: BLE001
            logger.exception(
                "underlying_candle_feed: failed to restore persisted bars for %s from %s - skipping "
                "that file, reconstruction continues from live ticks only", symbol, path,
            )
    bars.sort(key=lambda b: b["candle_start"])
    return bars[-MAX_BARS_KEPT:]


def _restore_from_disk(symbol: str) -> None:
    """Called once per symbol, the first time `subscribe()` sees it in
    THIS process's lifetime - i.e. exactly the "did we just restart"
    moment. A no-op if this symbol already has in-memory bars (can't
    happen on a genuine fresh process start, only guards against a
    theoretical double-call)."""
    today = datetime.now(IST).date()
    bars = _load_persisted_bars(symbol, today)
    if not bars:
        return
    with _lock:
        st = _state.setdefault(symbol, _SymbolState())
        if st.bars:
            return
        st.bars = bars
    logger.info(
        "underlying_candle_feed: restored %d persisted bar(s) for %s from disk - reconciliation after "
        "a possible restart, not starting cold", len(bars), symbol,
    )


def _candle_start_for(t: datetime) -> datetime:
    """Floors to the 5-min boundary this tick belongs in. Plain wall-clock
    flooring is correct for NSE because the session opens exactly on one
    (09:15) - see module docstring."""
    floored_minute = t.minute - (t.minute % 5)
    return t.replace(minute=floored_minute, second=0, microsecond=0)


def _update_bar(st: _SymbolState, ltp: float, cum_volume: float, t: datetime) -> Optional[dict]:
    """Pure-ish core (only touches `st`) - kept separate from the tick
    callback so backtest_ws_candle_reconstruction_parity.py can drive it
    directly with synthetic ticks. Returns the just-COMPLETED bar dict if
    this tick started a new 5-min window, else None. Caller (on_tick)
    handles day-rollover and MAX_BARS_KEPT trimming."""
    bar_start = _candle_start_for(t)
    completed = None
    if st.current_bar_start is None:
        # First tick ever for this symbol/subscription - open a bar, no
        # completed bar to emit yet. Real incident (22 Sep 2026, live
        # parity check): this used to hardcode the volume baseline to 0.0
        # here, on the assumption a "fresh" first tick always means the
        # trading day has just opened (cum_volume genuinely near 0). That
        # assumption only holds if the subscription itself starts at
        # 09:15 - it does NOT for a symbol subscribed mid-day (confirmed
        # live: the parity check subscribes at 10:00 IST, and in
        # production the dispatcher subscribes a symbol whenever it first
        # enters its pool, any time in the session). With a 0.0 baseline,
        # the first bar's "volume" became cum_volume_now - 0 = the
        # ENTIRE day's cumulative volume so far, not that one bar's
        # volume - confirmed live as a 25-60x overshoot on every test
        # symbol's first reconstructed bar. Seeding the baseline from
        # THIS tick's own cum_volume instead means the first (partial)
        # bar only ever counts volume from the moment we started
        # listening onward - correct for a mid-day subscribe, and no
        # different from before for a true day-open subscribe (where
        # cum_volume is already ~0 at the first tick anyway).
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
        st = _state.setdefault(underlying_symbol, _SymbolState())
        if st.day != today:
            # Day rollover (or first tick ever for this symbol) - CORRECTED
            # 22 Sep 2026 (user's own standing continuous-candles rule,
            # restated explicitly: "candles spanning across last few days
            # to current timestamp like all brokers and charting platforms
            # do"). This used to wipe st.bars here on the theory that
            # "continuous-candle rule is about never resetting a lookback
            # mid-window, not about carrying yesterday's session into
            # today's" - that reasoning was simply wrong: a real chart's
            # 5-min series does NOT reset at midnight, so the FIRST candle
            # right after today's 09:15 open must still see yesterday's
            # closing bars for the consolidation lookback (BREAKOUT_
            # LOOKBACK_CANDLES, 5 as of 23 Sep 2026), exactly
            # like breakout_signal.py's own REST path (_fetch_5m_sync)
            # already does by fetching BREAKOUT_CANDLE_LOOKBACK_DAYS of
            # continuous history. Only the intra-day CUMULATIVE VOLUME
            # baseline genuinely needs a day-boundary reset (Dhan's
            # Quote-tick cum_volume is day-relative, not truly cumulative
            # forever) - st.bars itself is left untouched here, still
            # trimmed to MAX_BARS_KEPT by the append step below.
            st.day = today
            st.current_bar_start = None
            st.cum_volume_at_bar_start = 0.0
            st.cum_volume_now = 0.0
        completed = _update_bar(st, ltp, cum_volume, t)
        if completed is not None:
            st.bars.append(completed)
            if len(st.bars) > MAX_BARS_KEPT:
                del st.bars[: len(st.bars) - MAX_BARS_KEPT]
        st.last_tick_at = t
    if completed is not None:
        # Deliberately outside _lock - this is disk I/O (see _persist_bar's
        # own docstring for why it must never block tick processing for
        # every other subscribed symbol sharing the same lock).
        _persist_bar(underlying_symbol, today, completed)


def subscribe(symbols: list[str]) -> None:
    """Idempotent - subscribes each symbol's underlying equity in Quote
    mode. Safe to call repeatedly (e.g. once per day at universe-seed
    time) with the same list; already-subscribed symbols are a no-op.

    Restores this symbol's persisted bars from disk BEFORE subscribing
    to live ticks, the first time this process sees it - so a restart
    mid-session reconciles from history/ instead of starting cold (see
    _restore_from_disk's own docstring for the incident this fixes)."""
    from Options.dhan_client import dhan_wrapper
    global _tick_subscriber_registered  # noqa: PLW0603
    if not _tick_subscriber_registered:
        dhan_wrapper.add_quote_tick_subscriber(_on_tick)
        _tick_subscriber_registered = True
    for sym in symbols:
        if sym in _subscribed:
            continue
        _restore_from_disk(sym)
        try:
            dhan_wrapper.subscribe_equity_quote(sym)
            _subscribed.add(sym)
        except Exception:  # noqa: BLE001
            logger.exception("underlying_candle_feed: failed to WS-subscribe %s - it will stay on REST fallback", sym)


def is_fresh(symbol: str, max_age_seconds: float) -> bool:
    with _lock:
        st = _state.get(symbol)
        if st is None or st.last_tick_at is None:
            return False
        age = (datetime.now(IST) - st.last_tick_at).total_seconds()
        return age <= max_age_seconds


def has_bars(symbol: str) -> bool:
    """True once at least one COMPLETED bar exists for this symbol - i.e.
    there's something for the WS-walk evaluator to actually walk. A
    symbol can be `is_fresh` (ticking) with zero bars for its first ~5
    minutes after subscribe (no REST backfill on subscribe - see module
    docstring), which used to route it into the WS-walk path anyway with
    nothing to evaluate. Callers should fall back to the REST path
    (which fetches real history immediately) until this turns True.

    NOTE (23 Sep 2026): "at least one bar" is NOT the same threshold the
    WS-walk evaluator itself needs (BREAKOUT_LOOKBACK_CANDLES + 1, 6 by
    default as of 23 Sep 2026, to compute the prior-N-candles consolidation
    range) - a symbol with fewer bars than that passes this check but still can't actually be
    evaluated by _evaluate_ws_walk_sync, which just returns "nothing to
    do yet" every cycle with no error and no checked_through_epoch
    update. Real incident, same day: TORNTPHARM/BIOCON/DIVISLAB sat with
    3-5 bars each, is_fresh=True, has_bars=True - routed to the WS-walk
    path by breakout_signal.py's _split_ws_rest and then silently never
    evaluated at all, for up to 40+ minutes, when a REST call would have
    seen their real history immediately. See bar_count() below, which
    _split_ws_rest now uses instead of this function for that decision -
    has_bars() itself is kept as-is (still a correct, narrower claim: at
    least one bar to walk) for any other caller that only needs that."""
    with _lock:
        st = _state.get(symbol)
        return bool(st and st.bars)


def bar_count(symbol: str) -> int:
    """Number of COMPLETED bars currently held for this symbol - lets a
    caller compare against its OWN "how many do I actually need"
    threshold (e.g. breakout_signal.py's BREAKOUT_LOOKBACK_CANDLES + 1)
    instead of has_bars()'s fixed ">= 1" answer - see that function's own
    updated docstring for the real gap this closes."""
    with _lock:
        st = _state.get(symbol)
        return len(st.bars) if st is not None else 0


def get_candles_dict(symbol: str) -> dict:
    """Returns the SAME Dhan dict-of-lists shape (timestamp/open/high/low/
    close/volume, epoch seconds) breakout_signal.py's REST fetchers
    already produce - a drop-in substitute for _fetch_5m_sync's own
    return value, so the hybrid fetch path in breakout_signal.py needs no
    special-casing beyond picking which function to call. Only ever
    returns COMPLETED bars (never the still-forming current one)."""
    with _lock:
        st = _state.get(symbol)
        bars = list(st.bars) if st is not None else []
    if not bars:
        return {}
    return {
        "timestamp": [b["candle_start"].timestamp() for b in bars],
        "open": [b["open"] for b in bars], "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars], "close": [b["close"] for b in bars],
        "volume": [b["volume"] for b in bars],
    }


def snapshot() -> dict:
    """Read-only observability view - subscribed symbols, per-symbol last-
    tick age, and completed-bar count. For a future /underlying-feed-stats
    endpoint or manual inspection; not on any trading decision path."""
    now = datetime.now(IST)
    out = {}
    with _lock:
        for sym, st in _state.items():
            age = (now - st.last_tick_at).total_seconds() if st.last_tick_at else None
            out[sym] = {"bars": len(st.bars), "last_tick_age_seconds": age, "day": str(st.day)}
    return out
