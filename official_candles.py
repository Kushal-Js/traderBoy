"""
Official (Dhan REST) candles behind the live WS candles (2 Oct 2026, user: "use WS feeds for continuous candle usage
across strategies and use REST as a fallback").

BEFORE: Bollinger/signals (Unified Momentum engine A, Super Bollinger, Bollinger) and Swing/signals (engine B, the
NIFTY gate, Swing) kept a multi-day REST base per stock and re-downloaded it at EVERY new bar before computing
anything, although the WS feed already had the bar: ~66 downloads at each 5-min bar (~98 at 15-min ones) through
the 2-calls/s account pacing = ~30-45 s of queue. Unified Momentum refreshes its stocks one by one, so the last
stock's new pending order was ~25-30 s late, and the sleeping downloads held the executor workers orders use
(2.5-3 s order waits measured after the 2 Oct restarts).

NOW:
  1. ws_cover(): when every bar between a REST base and the newest closed bar is a COMPLETE WS bar (watched whole
     by this process - Swing.candle_feed WS_COMPLETE_*), the signal modules compute from base + WS bars at once,
     no download on the signal path. Anything else - stale feed, a gap, the bar a restart landed in, more than
     MAX_WS_BARS bars, a non-NSE series - is "no" and they download and wait exactly as before (REST fallback).
  2. A background thread (this module) then fetches Dhan's official candles - one short window (SHORT_DAYS) per
     instrument + interval, spliced into every registered base of it - Unified Momentum's stocks and NIFTY first
     (set_priority). It runs on its own thread, never on the executor orders use, and goes through the same
     account-wide pacing / DH-904 cooldown. When a base advances, its owner's signal caches are expired so the next
     read uses the official candle ("correct the order if it differs").
  3. refresh_now(): the same fetch for one stock, ahead of the background queue. Unified Momentum calls it before
     a real entry that would rest on a WS-only candle (Bollinger state.provisional, engine B's new signals), so a
     real order waits ~1 s for Dhan's confirmation instead of the old ~30 s queue, and never trades a WS-only
     signal that Dhan's candle does not show (when Dhan can answer).

Measured on 30 Sep (1,393 candles, newest candle WS vs official, history official): engine A same pending order
96.6% (trigger never differs when the side matches); engine B entry flag differs 0.29% - the confirmation step
above removes the WS-only cases from real entries.

Off switch: WS_CONTINUOUS_CANDLES=false (Options/config.py) -> ws_cover always "no", no thread, no confirmation =
the old behaviour exactly.
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

from Options import config as oconfig
from Options.dhan_client import IST, _bump_minute, dhan_wrapper
from Swing import candle_feed

logger = logging.getLogger("official_candles")

NSE_SEGMENTS = ("NSE_EQ", "IDX_I")
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)   # indices; stocks end at candle_feed.NSE_EQ_SESSION_END (15:15, closing auction after)
SUPPORTED_INTERVALS = (5, 15)
MAX_WS_BARS = 12              # never more than an hour of unconfirmed WS bars on top of a base
SHORT_DAYS = 4                # calendar days per official fetch: reaches back over a weekend + a holiday to the base
OFFICIAL_DELAY_SECONDS = 1.0  # after a bar's close before asking Dhan for it (a bar fetched early is re-read by the next fetch)
RETRY_SECONDS = 10            # a base Dhan did not advance (or the WS could not cover) is looked at again after this
PENDING_GRACE_SECONDS = 20    # how long after a close the newest bar may still wait for its closing trade
WS_STALE_SECONDS = 90         # same freshness as the signal modules (BOLLINGER_/SWING_WS_STALE_AFTER_SECONDS)
SERIES_KEYS = ("timestamp", "open", "high", "low", "close", "volume")

_STATS = {
    "ws_candle_reads_covered": 0,        # signal path used complete WS bars instead of downloading
    "ws_candle_reads_pending": 0,        # newest bar still waiting for its closing trade - waited, no download
    "ws_candle_rest_fallbacks": 0,       # signal path had to download (WS stale / incomplete / gap)
    "official_candle_fetches": 0,        # background + confirmation fetches (also in market_data_calls)
    "official_candle_bars_same": 0,      # official candle identical to the WS one the signals used
    "official_candle_bars_corrected": 0,  # official candle differed - signal caches expired, recomputed
    "official_candle_confirms": 0,       # refresh_now calls that had to fetch (a real entry waited for Dhan)
    "official_candle_confirm_ms_last": None,
    "official_candle_confirm_ms_max": 0.0,
    "official_candle_priority_delay_s_max_by_minute": {},  # bar close -> official candle in, priority stocks
}


def _now() -> datetime:
    return datetime.now(IST)


def enabled() -> bool:
    return bool(oconfig.WS_CONTINUOUS_CANDLES)


def running() -> bool:
    """The background thread is up (started by the bot's own startup - main.py lifespan - never by a script or a
    test that merely imports the signal modules). Without it nothing here changes the old behaviour."""
    thread = _state["thread"]
    return enabled() and thread is not None and thread.is_alive()


def _stat_add(key: str, value: float = 1) -> None:
    dhan_wrapper.stats[key] = dhan_wrapper.stats.get(key, 0) + value


def _init_stats() -> None:
    for key, value in _STATS.items():
        dhan_wrapper.stats.setdefault(key, value if not isinstance(value, dict) else {})


# --------------------------------------------------------------------------- #
# NSE session arithmetic, per segment (2 Oct 2026): stocks (NSE_EQ) trade 09:15-15:15 since NSE's closing auction
# (3 Aug 2026) - bars 09:15 ... 15:10 for 5 min, 09:15 ... 15:00 for 15 min, exactly what Dhan's REST history has;
# indices (IDX_I) keep 09:15-15:30 - bars to 15:25 / 15:15. Before this every stock base looked "behind" from 15:15
# to the close (and at the next open, the previous day's 15:10 never "followed" into 09:15), so the signal modules
# re-downloaded every base every 60 s in the first bar of the day and in the last 15 minutes.
# --------------------------------------------------------------------------- #
def session_close(exchange_segment: Optional[str] = None) -> dtime:
    """End of the regular session: 15:15 for stocks (NSE_EQ), 15:30 for indices (and anything unspecified)."""
    return candle_feed.session_end(exchange_segment) or SESSION_CLOSE


def newest_closed_bar_start(now: datetime, interval: int, exchange_segment: Optional[str] = None) -> Optional[datetime]:
    """Start of the newest bar of today's session that has closed by `now`, or None before the first one closes.
    After the session it stays the session's last bar (15:10 / 15:00 for stocks)."""
    close = session_close(exchange_segment)
    open_t = now.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    end = min(now, now.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0))
    closed = int((end - open_t).total_seconds() // (interval * 60))
    if closed < 1:
        return None
    return open_t + timedelta(minutes=interval * (closed - 1))


def _last_bar_time(interval: int, exchange_segment: Optional[str] = None) -> dtime:
    t = datetime.combine(datetime(2000, 1, 3).date(), session_close(exchange_segment)) - timedelta(minutes=interval)
    return t.time()


def _follows(prev: datetime, nxt: datetime, interval: int, exchange_segment: Optional[str] = None) -> bool:
    """`nxt` is the bar right after `prev` in the session series (a session's last bar is followed by the first
    bar of a later date - weekends and holidays have no bars)."""
    if prev.time() >= _last_bar_time(interval, exchange_segment):
        return nxt.date() > prev.date() and nxt.time() == SESSION_OPEN
    return nxt == prev + timedelta(minutes=interval)


def session_capped(bar_start: datetime, interval: int, exchange_segment: Optional[str]) -> datetime:
    """A clock-based "newest closed bar" (the signal modules' own arithmetic) capped at the session's last bar for
    NSE segments: after 15:15 a stock's newest closed bar stays 15:10 (15:00 for 15 min) - no newer one will come."""
    if exchange_segment not in NSE_SEGMENTS or interval not in SUPPORTED_INTERVALS:
        return bar_start
    last = _last_bar_time(interval, exchange_segment)
    if bar_start.time() > last:
        return bar_start.replace(hour=last.hour, minute=last.minute, second=0, microsecond=0)
    return bar_start


def ws_cover(symbol: str, exchange_segment: str, base_last_ts: Optional[float], interval: int,
             now: Optional[datetime] = None, count: bool = False) -> str:
    """Can the bars missing from a REST base ending at `base_last_ts` come from the WS feed?
    "covered" - nothing missing, or every missing bar up to the newest closed one is a complete WS bar;
    "pending" - all of that except the newest bar, which closed by the clock but whose closing trade has not
                arrived yet (the WS bar completes on the next trade, within seconds) - do not download;
    "no"      - download (the old path)."""
    result = _ws_cover(symbol, exchange_segment, base_last_ts, interval, now)
    if count and enabled():
        _stat_add({"covered": "ws_candle_reads_covered", "pending": "ws_candle_reads_pending"}.get(
            result, "ws_candle_rest_fallbacks"))
    return result


def _ws_cover(symbol: str, exchange_segment: str, base_last_ts: Optional[float], interval: int,
              now: Optional[datetime]) -> str:
    if (not running() or base_last_ts is None or exchange_segment not in NSE_SEGMENTS
            or interval not in SUPPORTED_INTERVALS):
        return "no"
    now = now or _now()
    required = newest_closed_bar_start(now, interval, exchange_segment)
    prev = datetime.fromtimestamp(base_last_ts, tz=IST)
    last_bar = _last_bar_time(interval, exchange_segment)
    # Nothing missing -> no WS bar is needed (so no WS freshness either): e.g. 09:15:00 before the first tick, with
    # the base ending at the previous session's last bar, or after 15:15 with a stock base at 15:10.
    if (prev >= required) if required is not None else (prev.date() < now.date() and prev.time() >= last_bar):
        return "covered"
    if not candle_feed.is_fresh(symbol, WS_STALE_SECONDS):
        return "no"
    used = 0
    for start, complete in candle_feed.complete_bars_after(symbol, interval, prev):
        if required is not None and start > required:
            break
        if not complete or not _follows(prev, start, interval, exchange_segment):
            return "no"
        used += 1
        if used > MAX_WS_BARS:
            return "no"
        prev = start
    if required is None:
        # No bar of today has closed: fine when the series ends at an earlier session's close.
        return "covered" if prev.date() < now.date() and prev.time() >= last_bar else "no"
    if prev >= required:
        return "covered"
    forming = candle_feed.forming_bar(symbol)
    if (forming is not None and _follows(prev, required, interval, exchange_segment)
            and candle_feed._candle_start_for(forming["candle_start"], interval) == required
            and (now - required - timedelta(minutes=interval)).total_seconds() <= PENDING_GRACE_SECONDS):
        return "pending"
    return "no"


# --------------------------------------------------------------------------- #
# Registry of REST bases (owned by Bollinger/signals and Swing/signals)
# --------------------------------------------------------------------------- #
@dataclass
class _Base:
    key: tuple
    symbol: str
    security_id: str
    exchange_segment: str
    instrument_type: str
    interval: int
    lookback_days: Optional[int]
    get: Callable[[], Optional[dict]]
    put: Callable[[dict], None]
    expire: Callable[[str], None]
    last_attempt: float = 0.0          # time.monotonic() of the last background look


_lock = threading.Lock()
_bases: dict[tuple, _Base] = {}
_group_locks: dict[tuple, threading.Lock] = {}
_priority: dict[str, frozenset] = {}
_update_listeners: list = []
_state = {"priority_waiting": 0, "thread": None}


def register(key: tuple, symbol: str, security_id: str, exchange_segment: str, instrument_type: str, interval: int,
             lookback_days: Optional[int], get: Callable[[], Optional[dict]], put: Callable[[dict], None],
             expire: Callable[[str], None]) -> None:
    """Idempotent - called by the signal modules each time they hold a base. Only NSE session series qualify."""
    if not enabled() or exchange_segment not in NSE_SEGMENTS or interval not in SUPPORTED_INTERVALS:
        return
    with _lock:
        _note_seen(key, symbol, interval, lookback_days)
        known = _bases.get(key)
        if known is not None and known.security_id == security_id:
            return
        _bases[key] = _Base(key, symbol, security_id, exchange_segment, instrument_type, interval, lookback_days,
                            get, put, expire)
        _group_locks.setdefault((security_id, exchange_segment, instrument_type, interval), threading.Lock())


# --------------------------------------------------------------------------- #
# Pre-open warm-up (2 Oct 2026, pre-Monday load audit). The 08:00 restart leaves every base empty and every stock
# unsubscribed until the loops first touch them at 09:15:00 (1 Oct journal: subscriptions 09:15:01 onwards). So the
# 09:15 WS bar could never count as complete, ~110 bases downloaded at once (~55 s through the 2 calls/s pacing) and
# the 09:20 bar - UM's first entries and engine B's first signals - fell back to downloads again. Now this thread
# loads, at PREWARM_START on weekdays, every base the signal modules used on the last day they ran (BASES_FILE,
# rewritten by register() as bases are used) through each owner's loader: the stock is WS-subscribed (watched from
# before 09:15) and its REST base is downloaded exactly as its signal path would, so at 09:15 nothing is missing.
# --------------------------------------------------------------------------- #
PREWARM_START = dtime(9, 5)        # pre-open order entry is over; the last session's history is final
PREWARM_END = dtime(9, 14)         # never start later than this - the loops take over at 09:15
BASES_FILE = Path("data/official_candles_bases.json")
SEEN_SAVE_SECONDS = 30
_loaders: dict[str, Callable[[str, dict], None]] = {}
_hooks: dict[str, Callable[[], object]] = {}     # name -> fn(): other warm-ups (e.g. the Scalper's own candle books)
_seen = {"date": None, "bases": {}, "dirty": False, "saved_at": 0.0}
_prewarm = {"done_date": None, "last": None, "ident": None}   # ident: the thread running prewarm(), while it runs


def add_prewarm_loader(owner: str, fn: Callable[[str, dict], None]) -> None:
    """fn(symbol, entry) loads one base of `owner` ("bollinger", "swing") exactly as its signal path would, WS
    subscription included. Blocking - runs on this module's thread."""
    _loaders[owner] = fn


def add_prewarm_hook(name: str, fn: Callable[[], object]) -> None:
    """fn() warms something that is not a registered base (2 Oct 2026: the Scalper's 1-min / 15-min books) at
    PREWARM_START, before the bases. Blocking - runs on this module's thread; its return value is shown in the
    warm-up result (GET /official-candles -> prewarm.hooks)."""
    _hooks[name] = fn


def _note_seen(key: tuple, symbol: str, interval: int, lookback_days: Optional[int]) -> None:
    """Under _lock: remembers today's bases for tomorrow's warm-up (saved by the background thread). The warm-up's
    own loads do not count (2 Oct 2026): only a signal path using a base keeps it on tomorrow's list, so a stock
    the weekly refresh removed drops off after one day instead of being warmed (and kept fresh) forever."""
    if _prewarm.get("ident") == threading.get_ident():
        return
    today = _now().date().isoformat()
    if _seen["date"] != today:
        _seen.update(date=today, bases={}, dirty=True)
    k = repr(key)
    if k not in _seen["bases"]:
        _seen["bases"][k] = {"owner": str(key[0]), "symbol": symbol, "interval": interval,
                             "lookback_days": lookback_days}
        _seen["dirty"] = True


def _maybe_save_seen() -> None:
    with _lock:
        if not _seen["dirty"] or time.monotonic() - _seen["saved_at"] < SEEN_SAVE_SECONDS:
            return
        payload = {"date": _seen["date"], "bases": sorted(_seen["bases"].values(), key=lambda e: (
            e["owner"], e["symbol"], e["interval"], e["lookback_days"] or 0))}
        _seen["dirty"], _seen["saved_at"] = False, time.monotonic()
    try:
        BASES_FILE.parent.mkdir(exist_ok=True)
        tmp = BASES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, BASES_FILE)
    except Exception:  # noqa: BLE001
        logger.exception("official candles: could not save %s", BASES_FILE)


def _prewarm_due(now: datetime) -> bool:
    return (now.weekday() < 5 and PREWARM_START <= now.time() < PREWARM_END
            and _prewarm["done_date"] != now.date())


def _series(entry: dict) -> tuple:
    return entry.get("owner"), entry.get("interval"), entry.get("lookback_days")


TEMPLATE_SHARE = 0.8   # a candle series counts as "what a UM stock uses" when >= 80% of UM's listed stocks have it


def _missing_priority_entries(entries: list, priority: set) -> list:
    """Bases for Unified Momentum's stocks that BASES_FILE does not list (2 Oct 2026): a stock the weekly refresh
    (Fri 00:00) just added was not used on the last trading day, so it gets the candle series (nearly) every UM stock
    already in the file uses - today Bollinger 5 min / 60 days + Swing 5 min / 45 days, 15 min / 45 days, 5 min /
    7 days (all 15 stocks); Swing's 15 min / 7 days is only on the stocks Swing paper also trades (8 of 15) and is
    left out. A stock is told apart from an index (NIFTY is a priority symbol with fewer series) by the segment it
    was just WS-subscribed under. Call after the file's entries were loaded."""
    by_symbol: dict[str, set] = {}
    for e in entries:
        by_symbol.setdefault(e.get("symbol"), set()).add(_series(e))
    stocks = [s for s in priority if s in by_symbol and (candle_feed._subscribed_ref.get(s) or (None, None))[1] == "NSE_EQ"]
    if not stocks:
        return []
    counts: dict[tuple, int] = {}
    for s in stocks:
        for series in by_symbol[s]:
            counts[series] = counts.get(series, 0) + 1
    template = {series for series, n in counts.items() if n >= TEMPLATE_SHARE * len(stocks)}
    out = []
    for symbol in sorted(priority):
        if symbol in by_symbol and symbol not in stocks:
            continue                       # an index (or a symbol whose loads failed)
        for owner, interval, lookback in sorted(template - by_symbol.get(symbol, set()), key=str):
            out.append({"owner": owner, "symbol": symbol, "interval": interval, "lookback_days": lookback})
    return out


def prewarm(now: Optional[datetime] = None) -> dict:
    """Loads every base listed in BASES_FILE (Unified Momentum's stocks and NIFTY first), then the bases of any UM
    stock the file does not list yet (_missing_priority_entries - e.g. added by Friday's weekly refresh). Blocking -
    the background thread runs it once per weekday at PREWARM_START. Each load goes through the shared pacing like
    any download. These loads are not "use" (_note_seen ignores them)."""
    now = now or _now()
    _prewarm["done_date"] = now.date()
    try:
        entries = json.loads(BASES_FILE.read_text()).get("bases") or []
    except FileNotFoundError:
        entries = []
    except Exception:  # noqa: BLE001
        logger.exception("official candles: could not read %s - no warm-up", BASES_FILE)
        entries = []
    priority = _priority_symbols()
    entries.sort(key=lambda e: (0 if e.get("symbol") in priority else 1, e.get("symbol") or "", e.get("interval") or 0))
    started = time.monotonic()
    counts = {"loaded": 0, "failed": 0, "skipped": 0}

    def load(entry: dict) -> None:
        loader = _loaders.get(entry.get("owner"))
        if loader is None or not entry.get("symbol"):
            counts["skipped"] += 1
            return
        try:
            loader(entry["symbol"], entry)
            counts["loaded"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.exception("official candles: warm-up of %s %s %sm failed - its loop downloads it at 09:15",
                             entry.get("owner"), entry.get("symbol"), entry.get("interval"))

    _prewarm["ident"] = threading.get_ident()
    added: list = []
    hooks: dict = {}
    try:
        for name, fn in list(_hooks.items()):          # real-money Scalper first: a few calls, ~1-2 s
            try:
                hooks[name] = fn()
            except Exception as exc:  # noqa: BLE001
                hooks[name] = f"failed: {exc}"
                logger.exception("official candles: warm-up hook %s failed - it readies itself as before", name)
        for entry in entries:
            load(entry)
        added = _missing_priority_entries(entries, priority)
        for entry in added:
            load(entry)
    finally:
        _prewarm["ident"] = None
    result = {"date": now.date().isoformat(), "listed": len(entries), **counts,
              "seconds": round(time.monotonic() - started, 1), "symbols": len({e.get("symbol") for e in entries}),
              "added_um_stocks": sorted({e["symbol"] for e in added}), "added_bases": len(added), "hooks": hooks}
    _prewarm["last"] = result
    logger.info("official candles: pre-open warm-up done - %s", result)
    return result


def _maybe_prewarm() -> None:
    now = _now()
    if _prewarm_due(now):
        prewarm(now)


def start() -> None:
    """Starts the background thread (once). Called from the bot's startup (main.py lifespan)."""
    if not enabled():
        logger.info("official candles: WS_CONTINUOUS_CANDLES=false - every new bar waits for Dhan's candle (old path)")
        return
    with _lock:
        if _state["thread"] is not None:
            return
        _init_stats()
        _state["thread"] = threading.Thread(target=_run, name="official-candles", daemon=True)
    _state["thread"].start()
    logger.info("official candles: background thread started - new bars from complete WS candles at once, Dhan's "
                "candles fetched behind them (priority: %s)", sorted(_priority_symbols()) or "none yet")


def set_priority(owner: str, symbols) -> None:
    """Symbols whose official candles are fetched first (Unified Momentum - real money)."""
    _priority[owner] = frozenset(symbols)


def add_update_listener(fn) -> None:
    """fn(symbol, interval) after a base took official candles (called on the background / caller thread)."""
    if fn not in _update_listeners:
        _update_listeners.append(fn)


def _priority_symbols() -> set:
    out: set = set()
    for symbols in list(_priority.values()):
        out |= symbols
    return out


# --------------------------------------------------------------------------- #
# Fetch + splice
# --------------------------------------------------------------------------- #
def _closed_only(data: dict, interval: int, now: datetime) -> dict:
    ts = list(data.get("timestamp") or [])
    out = {key: list(data.get(key) or []) for key in SERIES_KEYS}
    if ts and now < datetime.fromtimestamp(ts[-1], tz=IST) + timedelta(minutes=interval):
        out = {key: values[:-1] for key, values in out.items()}
    return out


def _splice(base: dict, short: dict) -> tuple[str, Optional[dict]]:
    """("ok", new base) - base bars before the short window + the short window;
    ("behind", None) - the short window does not reach past the base (Dhan has not published the bar yet);
    ("gap", None) - the short window starts after the base ends (needs a full fetch)."""
    b_ts = base.get("timestamp") or []
    s_ts = short.get("timestamp") or []
    if not b_ts or not s_ts or s_ts[-1] <= b_ts[-1]:
        return "behind", None
    if s_ts[0] > b_ts[-1]:
        return "gap", None
    cut = bisect.bisect_left(b_ts, s_ts[0])
    return "ok", {key: list((base.get(key) or [])[:cut]) + list(short.get(key) or []) for key in SERIES_KEYS}


def _note_official(symbol: str, interval: int, new: dict, old_last: float) -> None:
    """Counts official candles that match / differ from the WS candles the signals may have used."""
    ws = candle_feed.get_candles_dict(symbol, interval)
    index = {t: i for i, t in enumerate(ws.get("timestamp") or [])}
    ts = new.get("timestamp") or []
    for j in range(len(ts) - 1, -1, -1):
        if ts[j] <= old_last:
            break
        i = index.get(float(ts[j]))
        if i is None:
            continue
        same = all(abs(float(new[k][j]) - float(ws[k][i])) < 1e-6 for k in ("open", "high", "low", "close"))
        _stat_add("official_candle_bars_same" if same else "official_candle_bars_corrected")
        if not same:
            logger.debug("official candle differs: %s %sm %s WS o/h/l/c %s/%s/%s/%s official %s/%s/%s/%s", symbol,
                         interval, datetime.fromtimestamp(ts[j], tz=IST).strftime("%H:%M"), ws["open"][i],
                         ws["high"][i], ws["low"][i], ws["close"][i], new["open"][j], new["high"][j], new["low"][j],
                         new["close"][j])


def _refresh_group(key: tuple, members: list, now: datetime) -> bool:
    """One short official fetch for (security_id, segment, instrument, interval), spliced into every member base.
    Blocking (Dhan call through the shared pacing). True when at least one base advanced."""
    security_id, exchange_segment, instrument_type, interval = key
    for m in members:
        m.last_attempt = time.monotonic()
    _stat_add("official_candle_fetches")
    short = _closed_only(dhan_wrapper.fetch_continuous_intraday(
        security_id, exchange_segment, instrument_type, interval, lookback_days_override=SHORT_DAYS) or {},
        interval, now)
    advanced = False
    for m in members:
        base = m.get() or {}
        old_ts = base.get("timestamp") or []
        if not old_ts:
            continue
        status, new = _splice(base, short)
        if status == "gap":
            _stat_add("official_candle_fetches")
            full = _closed_only(dhan_wrapper.fetch_continuous_intraday(
                security_id, exchange_segment, instrument_type, interval,
                lookback_days_override=m.lookback_days) or {}, interval, now)
            if (full.get("timestamp") or [0])[-1] > old_ts[-1]:
                new = full
        if new is None:
            continue
        m.put(new)
        advanced = True
        try:
            _note_official(m.symbol, interval, new, old_ts[-1])
        except Exception:  # noqa: BLE001
            logger.exception("official candles: could not compare %s %sm with the WS candles", m.symbol, interval)
        try:
            m.expire(m.symbol)
        except Exception:  # noqa: BLE001
            logger.exception("official candles: could not expire %s's signal caches", m.symbol)
        for fn in list(_update_listeners):
            try:
                fn(m.symbol, interval)
            except Exception:  # noqa: BLE001
                logger.exception("official candles: update listener failed for %s", m.symbol)
    if advanced and any(m.symbol in _priority_symbols() for m in members):
        required = newest_closed_bar_start(now, interval, exchange_segment)
        if required is not None:
            delay = round((_now() - required - timedelta(minutes=interval)).total_seconds(), 1)
            dhan_wrapper.stats["official_candle_priority_delay_s_max_by_minute"] = _bump_minute(
                dhan_wrapper.stats.get("official_candle_priority_delay_s_max_by_minute") or {}, delay, how="max")
    return advanced


def _behind(m: _Base, now: datetime) -> Optional[datetime]:
    """The newest closed bar start when `m`'s base does not reach it yet, else None."""
    required = newest_closed_bar_start(now, m.interval, m.exchange_segment)
    base = m.get() or {}
    ts = base.get("timestamp") or []
    if required is None or not ts or datetime.fromtimestamp(ts[-1], tz=IST) >= required:
        return None
    return required


def _work_once() -> bool:
    if _state["priority_waiting"]:
        return False
    now = _now()
    mono = time.monotonic()
    with _lock:
        bases = list(_bases.values())
    groups: dict[tuple, list] = {}
    for m in bases:
        if mono - m.last_attempt < RETRY_SECONDS:
            continue
        required = _behind(m, now)
        if required is None or now < required + timedelta(minutes=m.interval, seconds=OFFICIAL_DELAY_SECONDS):
            continue
        base_ts = (m.get() or {}).get("timestamp") or []
        if ws_cover(m.symbol, m.exchange_segment, base_ts[-1], m.interval, now) == "no":
            m.last_attempt = mono      # the signal path downloads this one itself (old path) - look again later
            continue
        groups.setdefault((m.security_id, m.exchange_segment, m.instrument_type, m.interval), []).append(m)
    if not groups:
        return False
    priority = _priority_symbols()

    def rank(item):
        (_sid, _seg, _inst, interval), members = item
        return (0 if any(m.symbol in priority for m in members) else 1, interval, min(m.symbol for m in members))

    key, members = min(groups.items(), key=rank)
    with _group_locks[key]:
        members = [m for m in members if _behind(m, _now()) is not None]   # a confirm may have done it
        if members:
            _refresh_group(key, members, _now())
    return True


def _run() -> None:
    while True:
        try:
            candle_feed.close_session_bars()      # a stock's 15:10 bar completes at 15:15 (no trade comes after it)
            _maybe_prewarm()
            _maybe_save_seen()
            worked = _work_once()
        except Exception:  # noqa: BLE001
            logger.exception("official candles: background pass failed")
            worked = False
        time.sleep(0.05 if worked or _state["priority_waiting"] else 0.5)


def refresh_now(symbol: str, intervals: Optional[tuple] = None) -> bool:
    """Blocking (executor). Brings every registered base of `symbol` (optionally only `intervals`) up to the newest
    closed bar with Dhan's candles, ahead of the background queue. True when they all reach it - immediately and
    without a Dhan call when they already do."""
    if not running():
        return False
    with _lock:
        members = [m for m in _bases.values()
                   if m.symbol == symbol and (intervals is None or m.interval in intervals)]
    groups: dict[tuple, list] = {}
    for m in members:
        if _behind(m, _now()) is not None:
            groups.setdefault((m.security_id, m.exchange_segment, m.instrument_type, m.interval), []).append(m)
    if not groups:
        return True
    started = time.monotonic()
    with _lock:
        _state["priority_waiting"] += 1
    try:
        for key, group in groups.items():
            with _group_locks[key]:
                group = [m for m in group if _behind(m, _now()) is not None]
                if group:
                    _refresh_group(key, group, _now())
    finally:
        with _lock:
            _state["priority_waiting"] -= 1
    ms = round((time.monotonic() - started) * 1000, 1)
    _stat_add("official_candle_confirms")
    dhan_wrapper.stats["official_candle_confirm_ms_last"] = ms
    dhan_wrapper.stats["official_candle_confirm_ms_max"] = max(dhan_wrapper.stats.get("official_candle_confirm_ms_max")
                                                               or 0.0, ms)
    now = _now()
    return all(_behind(m, now) is None for group in groups.values() for m in group)


def snapshot() -> dict:
    """GET /official-candles - registered bases and how far behind each is (read-only)."""
    now = _now()
    out = {"enabled": enabled(), "running": running(), "priority": sorted(_priority_symbols()),
           "prewarm": _prewarm["last"], "bases": []}
    with _lock:
        bases = list(_bases.values())
    for m in bases:
        ts = (m.get() or {}).get("timestamp") or []
        out["bases"].append({
            "owner": str(m.key[0]), "symbol": m.symbol, "interval": m.interval, "lookback_days": m.lookback_days,
            "last_official_bar": datetime.fromtimestamp(ts[-1], tz=IST).strftime("%Y-%m-%d %H:%M") if ts else None,
            "behind": _behind(m, now) is not None,
        })
    return out
