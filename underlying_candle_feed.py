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
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("underlying_candle_feed")

IST = ZoneInfo("Asia/Kolkata")

# Bars kept per symbol - comfortably more than any real signal check's own
# lookback (BREAKOUT_LOOKBACK_CANDLES=10) plus the 20d/50d DAILY checks
# (which stay on REST regardless - see module docstring; only the 5-min
# intraday series is WS-sourced). 120 bars = 10 trading hours, i.e. two
# full sessions of headroom even though this resets at day rollover anyway.
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
        # First tick ever for this symbol/day - open a bar, no completed
        # bar to emit, and volume baseline starts at this tick's own
        # cumulative reading (a fresh trading day's cumulative volume is
        # 0 immediately before the first tick, so the first bar's own
        # volume is simply cum_volume itself once it closes).
        st.current_bar_start = bar_start
        st.bar_open = st.bar_high = st.bar_low = st.bar_close = ltp
        st.cum_volume_at_bar_start = 0.0
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
    with _lock:
        st = _state.setdefault(underlying_symbol, _SymbolState())
        if st.day != today:
            # Day rollover (or first tick ever for this symbol) - a fresh
            # trading day's cumulative volume baseline is 0, and any prior
            # day's bars are stale for the 10-candle lookback (continuous-
            # candle rule is about never RESETTING a lookback mid-window,
            # not about carrying yesterday's session into today's).
            st.day = today
            st.current_bar_start = None
            st.bars = []
            st.cum_volume_at_bar_start = 0.0
            st.cum_volume_now = 0.0
        completed = _update_bar(st, ltp, cum_volume, t)
        if completed is not None:
            st.bars.append(completed)
            if len(st.bars) > MAX_BARS_KEPT:
                del st.bars[: len(st.bars) - MAX_BARS_KEPT]
        st.last_tick_at = t


def subscribe(symbols: list[str]) -> None:
    """Idempotent - subscribes each symbol's underlying equity in Quote
    mode. Safe to call repeatedly (e.g. once per day at universe-seed
    time) with the same list; already-subscribed symbols are a no-op."""
    from Options.dhan_client import dhan_wrapper
    global _tick_subscriber_registered  # noqa: PLW0603
    if not _tick_subscriber_registered:
        dhan_wrapper.add_quote_tick_subscriber(_on_tick)
        _tick_subscriber_registered = True
    for sym in symbols:
        if sym in _subscribed:
            continue
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
