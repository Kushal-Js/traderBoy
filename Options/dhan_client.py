"""
Thin wrapper around `Dhan_Tradehull.Tradehull` (REST convenience layer) plus
the raw `dhanhq` SDK's WebSocket classes for live order updates and market
data (Tradehull itself has no WebSocket support - see PyPI project page).

References:
  https://pypi.org/project/Dhan-Tradehull/
  https://pypi.org/project/dhanhq/
  https://dhanhq.co/docs/v2/orders/    (order_status enum, AMO fields)
  https://dhanhq.co/docs/v2/portfolio/ (positions response fields)

Ground truth for field names below was taken from the installed packages'
own source (Dhan_Tradehull/Dhan_Tradehull.py, dhanhq/dhan_http.py,
dhanhq/marketfeed.py, dhanhq/orderupdate.py), not just the docs site, since
the docs are thin in places.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from Dhan_Tradehull import Tradehull
from dhanhq import MarketFeed, OrderUpdate

import nifty_market_guard
from . import config

logger = logging.getLogger("dhan_client")

IST = ZoneInfo(config.MARKET_TZ)


def _tick_time_from_ltt(raw_ltt, received_at: datetime) -> datetime:
    """Parses a Quote/Full packet's own `LTT` (Last Trade Time) into a
    real datetime, for bucketing a tick by when the trade actually
    happened on the exchange rather than by when our process happened to
    receive/process the packet.

    Real incident (22 Sep 2026): underlying_candle_feed.py's 5-min bar
    reconstruction used local receipt time for bucketing, which showed a
    confirmed live open-price mismatch (up to 75% of bars wrong on some
    symbols) while close/volume stayed accurate - exactly the pattern a
    boundary-timing bug produces (a trade processed just after a 5-min
    boundary due to network/processing latency gets misbucketed into the
    NEXT bar, corrupting that bar's open specifically - the first
    attributed tick sets it - while close/volume, built from many ticks,
    mostly self-correct). `LTT` is parsed by the SDK (dhanhq's own
    process_quote/process_full) but was never read anywhere in this
    codebase until now.

    Timezone, empirically confirmed live (not guessed): the SDK formats
    LTT via `datetime.utcfromtimestamp(epoch).strftime('%H:%M:%S')` -
    named "utc_time" but a live comparison (22 Sep, post-close) showed
    LTT reading ~10 minutes behind local IST receipt time, not ~5.5
    hours behind as it would if the string genuinely needed a further
    +5:30 UTC->IST conversion. The string is already the correct IST
    wall-clock time-of-day as-is.

    Combines that HH:MM:SS with `received_at`'s own calendar date (NSE/
    MCX trading hours never span a real midnight, so this is safe) -
    falls back to `received_at` itself (the old behavior) if LTT is
    missing or doesn't parse, so a malformed/absent field degrades
    gracefully instead of dropping the tick."""
    if not raw_ltt:
        return received_at
    try:
        t = datetime.strptime(str(raw_ltt), "%H:%M:%S").time()
    except (TypeError, ValueError):
        return received_at
    return received_at.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)


def _retry(fn, *args, retries: int = 2, delay: float = 1.5, **kwargs):
    """Retries a call once or twice with a short backoff. Dhan's market-data
    REST calls (confirmed live) intermittently fail with a bare generic
    failure envelope when called back-to-back without pacing - Tradehull's
    own internal methods work around this with hardcoded time.sleep() calls
    between their own multi-step operations, which suggests this is a
    server-side rate limit rather than anything specific to our code."""
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                logger.warning("%s failed (attempt %s/%s): %s - retrying in %ss",
                                getattr(fn, "__name__", fn), attempt + 1, retries + 1, exc, delay)
                time.sleep(delay)
    raise last_exc


def _round_to_tick(price: float, tick_size: float) -> float:
    """Rounds `price` to the nearest valid multiple of `tick_size` - added
    9 Sep 2026 after a real SL-L order was REJECTED by the exchange with
    "EXCH:16283: The order price is not multiple of the tick size" (a
    controlled live test placed trigger=3.88/limit=3.76, neither a
    multiple of COALINDIA's own real Rs.0.05 tick, since we'd only ever
    rounded to 2 decimal places, not to the actual exchange tick). Unlike
    a normal MARKET/LIMIT order at a self-chosen price (where being off
    a paisa or two just risks a slightly worse fill), a stop order's
    trigger/limit values are COMPUTED (from a rupee-cap formula), so
    they land on an arbitrary tick-misaligned value far more often than
    a manually-chosen price would - this rounding step is required, not
    an edge case. Falls back to plain 2-decimal rounding if tick_size
    isn't a usable positive number (e.g. a lookup failure) rather than
    raising - a slightly-off-tick order that still gets REJECTED cleanly
    by the exchange (as this one was) is a safe failure mode, not a
    silent one."""
    if not tick_size or tick_size <= 0:
        return round(price, 2)
    return round(round(price / tick_size) * tick_size, 2)


def _compute_supertrend(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 10, multiplier: float = 3.0,
) -> list[Optional[float]]:
    """Standard Supertrend indicator (ATR via Wilder's smoothing, matching
    the default used by most charting platforms). Returns one Supertrend
    value per bar; the first `period` entries are None since ATR needs that
    many bars to seed. Pure function - no I/O, so it's cheap to unit-test
    independent of any live candle fetch."""
    n = len(closes)
    if n < period + 1:
        return [None] * n

    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )

    atr: list[Optional[float]] = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    upper_band: list[Optional[float]] = [None] * n
    lower_band: list[Optional[float]] = [None] * n
    supertrend: list[Optional[float]] = [None] * n
    trend_up: list[Optional[bool]] = [None] * n  # True while price is above the Supertrend line

    for i in range(period - 1, n):
        mid = (highs[i] + lows[i]) / 2
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]

        if i == period - 1:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            supertrend[i] = basic_upper if closes[i] <= basic_upper else basic_lower
            trend_up[i] = closes[i] > supertrend[i]
            continue

        prev_upper, prev_lower = upper_band[i - 1], lower_band[i - 1]
        upper_band[i] = basic_upper if (basic_upper < prev_upper or closes[i - 1] > prev_upper) else prev_upper
        lower_band[i] = basic_lower if (basic_lower > prev_lower or closes[i - 1] < prev_lower) else prev_lower

        if trend_up[i - 1]:
            supertrend[i] = lower_band[i] if closes[i] >= lower_band[i] else upper_band[i]
        else:
            supertrend[i] = upper_band[i] if closes[i] <= upper_band[i] else lower_band[i]
        trend_up[i] = closes[i] > supertrend[i]

    return supertrend


def _compute_ema(values: list[float], period: int) -> list[Optional[float]]:
    """Standard exponential moving average, SMA-seeded (the convention every
    mainstream charting platform uses). Returns one value per bar; the first
    `period - 1` entries are None (the seed lands on index period-1). Pure
    function - no I/O, cheap to unit-test on its own."""
    n = len(values)
    ema: list[Optional[float]] = [None] * n
    if n < period or period <= 0:
        return ema
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    ema[period - 1] = seed
    for i in range(period, n):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def _compute_rsi(closes: list[float], period: int) -> list[Optional[float]]:
    """Standard RSI(period), Wilder smoothing - same formula as IndexScalping/
    CopperOptions' own identical helper (kept as its own copy there per this
    repo's per-package independence convention for PAPER strategies), but
    this is the ONE shared implementation used by the real-money entry
    guard (see refresh_rsi_signal below) - a single computation consumed by
    Options/Futures/Luxury, the same reasoning _compute_supertrend/
    _compute_ema above already follow. Returns one value per bar; the
    first `period` entries are None (the seed lands on index period). Pure
    function - no I/O, cheap to unit-test on its own."""
    n = len(closes)
    rsi: list[Optional[float]] = [None] * n
    if n < period + 1:
        return rsi
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        gain, loss = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi


class OrderStatus:
    """Order status values, verbatim from DhanHQ's v2 API docs.
    https://dhanhq.co/docs/v2/orders/"""
    TRANSIT = "TRANSIT"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    PART_TRADED = "PART_TRADED"
    TRADED = "TRADED"
    EXPIRED = "EXPIRED"

    REJECTED_STATUSES = frozenset({REJECTED})
    # No further status change is expected.
    TERMINAL_STATUSES = frozenset({REJECTED, CANCELLED, TRADED, EXPIRED})
    # Still live / working at the exchange.
    OPEN_STATUSES = frozenset({TRANSIT, PENDING, PART_TRADED})


@dataclass
class OrderResult:
    order_id: str
    status: str          # one of OrderStatus.*
    remark: str
    fill_price: float
    filled_quantity: int
    # Unlike Groww, Dhan doesn't report a separate "amo_status" - whether an
    # order is an AMO is just whatever we set afterMarketOrder to when we
    # placed it, so callers pass that back in rather than us inferring it.
    is_amo: bool = False

    @property
    def is_queued_amo(self) -> bool:
        """True if this was placed as an AMO and hasn't resolved yet - i.e.
        genuinely still pending (not rejected/filled), just queued for the
        next session to dispatch."""
        return self.is_amo and self.status not in OrderStatus.TERMINAL_STATUSES


@dataclass
class AtmOption:
    trading_symbol: str
    strike: float
    option_type: str          # "CE" or "PE"
    lot_size: int
    security_id: str
    expiry_date: Optional[date] = None


class MissingOptionLegError(ValueError):
    """Raised by _get_atm_option_once when Tradehull's own ATM_Strike_
    Selection computes a strike but has no matching trading_symbol for
    the requested option_type at that strike (real incident, 22 Sep
    2026: COPPER CE at strike 1415 - Tradehull returned the strike but an
    empty ce_symbol). Carries `.strike` so get_liquid_atm_option can seed
    its nearby-strike search from a real, independently-useful number
    instead of just failing the whole lookup - see that function's own
    docstring for how this differs from an ATM strike that resolves fine
    but turns out illiquid (the case every other exception path here
    already handled before this one existed). Subclasses ValueError so
    any existing `except ValueError` catch site is unaffected."""
    def __init__(self, underlying_symbol: str, option_type: str, strike: float):
        super().__init__(f"No {option_type} leg found for {underlying_symbol} at strike {strike}")
        self.strike = strike


@dataclass
class FuturesContract:
    """A stock's nearest-expiry FUTSTK contract - see get_futures_contract()."""
    trading_symbol: str
    security_id: str
    lot_size: int
    expiry_date: Optional[date] = None


class DhanWrapper:
    """Lazily-authenticated singleton wrapper around Tradehull + dhanhq's
    WebSocket classes."""

    def __init__(self) -> None:
        self._client: Optional[Tradehull] = None
        self._order_update: Optional[OrderUpdate] = None
        # See _run_market_feed_forever()'s own docstring (21 Sep 2026
        # incident fix) for why this is no longer set via a simple lazy
        # property + feed.start() - a supervisor thread now owns its
        # entire lifecycle, so this can be None between reconnect attempts
        # even after the feed has been "started" once.
        self._market_feed: Optional[MarketFeed] = None
        self._market_feed_lock = threading.Lock()
        self._market_feed_thread_started = False
        # Our OWN authoritative subscribed-instrument set, independent of
        # any single MarketFeed instance's own `.instruments` - since a
        # reconnect now constructs a BRAND NEW MarketFeed object each time
        # (see _run_market_feed_forever), this is what gets handed to its
        # constructor so a reconnect automatically resubscribes everything
        # already subscribed before the drop, without needing to replay
        # subscribe_symbols() calls after the fact.
        self._market_feed_instruments: set[tuple] = set()
        # order_id (str) -> latest order-update dict pushed over the socket
        self._order_updates: dict[str, dict] = {}
        # security_id (str) -> last LTP pushed over the socket
        self._ltp_cache: dict[str, float] = {}
        # security_id (str) -> when that LTP was last updated - either by a
        # genuine WebSocket tick, or by a staleness-triggered REST refetch
        # (see note_rest_ltp()) re-priming the clock. Used by
        # get_cached_option_ltp() to force a fresh REST check instead of
        # trusting an old tick indefinitely - see config.LTP_STALE_AFTER_SECONDS.
        self._ltp_cache_ts: dict[str, datetime] = {}
        # security_id (str) -> trading_symbol, for every symbol we've ever
        # subscribed - lets the market-feed tick callback (which only knows
        # security_id) find the trading_symbol to fire on_price_tick with.
        self._security_id_to_symbol: dict[str, str] = {}
        # Fired synchronously from the market-feed's WebSocket thread on
        # every tick, as (trading_symbol, ltp) - must be fast/non-blocking.
        # A list, not a single slot: multiple strategies share this one
        # Dhan connection (Options, Futures, ...), each registering its own
        # handler via add_price_tick_subscriber() to drive its own
        # event-driven exit checks instead of waiting for its monitor_loop's
        # next poll. A single `Optional[Callable]` slot here would let
        # whichever strategy's lifespan runs last silently overwrite an
        # earlier one's handler, degrading it down to poll-only exits with
        # no error - confirmed as a real risk when the Futures package was
        # added (see NOTES.md's design-decision entry), fixed before it
        # could actually happen rather than after.
        self._on_price_tick_subscribers: list[Callable[[str, float], None]] = []
        # underlying-equity security_id (str) -> underlying_symbol, for
        # every equity we've subscribed in Quote mode (added 21 Sep 2026,
        # underlying_candle_feed.py's own WS-based candle reconstruction -
        # see that module's docstring). Kept DELIBERATELY SEPARATE from
        # _security_id_to_symbol above (which is option/NSE_FNO-only) -
        # trading-skills' incidents/2026-09-17-copper-mcx-security-id-
        # collision-and-adoption.md is exactly the failure mode of two
        # different instrument spaces silently sharing one lookup dict, and
        # this repo doesn't repeat that mistake twice.
        self._equity_security_id_to_symbol: dict[str, str] = {}
        # Same idea, MCX-segment futures contracts (added 23 Sep 2026,
        # Swing/candle_feed.py's own WS-based regime/Supertrend rebuild) -
        # a SEPARATE dict from the equity one above, not a shared one keyed
        # by bare security_id, because Dhan's security_id is NOT globally
        # unique across exchange segments and a real collision between an
        # NSE and an MCX instrument has already happened in this exact
        # codebase (trading-skills' incidents/2026-09-17-copper-mcx-
        # security-id-collision-and-adoption.md - an MCX contract's raw
        # security_id resolved to an unrelated NSE instrument when no
        # exchange hint was threaded through). _on_market_tick disambiguates
        # by the tick's own exchange_segment field before choosing which of
        # the two dicts to look in, so a numeric clash between the two
        # spaces can never cross-contaminate either one.
        self._mcx_security_id_to_symbol: dict[str, str] = {}
        # Same idea again, IDX_I-segment indices (NIFTY/BANKNIFTY - added 24
        # Sep 2026, Swing/candle_feed.py's WS coverage extended to
        # config.INDEX_SYMBOLS). A THIRD separate dict, same collision
        # reasoning as the MCX one above - NIFTY's security_id ("13") and
        # BANKNIFTY's ("25") are small integers with no guarantee of being
        # globally unique across NSE_EQ/MCX_COMM's own security_id spaces
        # either, so this never shares a lookup with them.
        self._index_security_id_to_symbol: dict[str, str] = {}
        # Fired synchronously from the market-feed's WebSocket thread on
        # every Quote/Full packet (never Ticker - those have no volume
        # field), as (underlying_symbol, ltp, day_cumulative_volume,
        # tick_time). Separate list from _on_price_tick_subscribers above
        # so existing LTP-only option subscribers are completely
        # unaffected by this addition - see add_quote_tick_subscriber().
        self._on_quote_tick_subscribers: list[Callable[[str, float, float, datetime], None]] = []
        # underlying_symbol -> (fetched_at, is_bearish, candle_start) - see
        # refresh_supertrend_signal()/get_cached_supertrend_bearish().
        self._supertrend_cache: dict[str, tuple[datetime, bool, Optional[datetime]]] = {}
        # underlying_symbol -> (fetched_at, fast_below_slow, crossed_this_candle,
        # candle_start) - see refresh_ema_cross_signal(). Same cache-then-poll-
        # refresh shape as _supertrend_cache; used only by packages that turn
        # config.ENABLE_EMA_CROSS_EXIT on (Futures as of 10 Sep 2026).
        self._ema_cross_cache: dict[str, tuple[datetime, bool, bool, Optional[datetime]]] = {}
        # underlying_symbol -> (fetched_at, current_rsi, prev_rsi, candle_start) -
        # see refresh_rsi_signal()/get_cached_rsi(). Same cache-then-poll-
        # refresh shape as _supertrend_cache/_ema_cross_cache above; used by
        # the same-day RSI-gated loss re-entry block (config.ENABLE_RSI_
        # LOSS_REENTRY_BLOCK), which replaced the old time-based LOSS_
        # COOLDOWN mechanism on 11 Sep 2026 (user request).
        self._rsi_cache: dict[str, tuple[datetime, Optional[float], Optional[float], Optional[datetime]]] = {}
        # option_trading_symbol -> (fetched_at, is_illiquid) - see
        # refresh_liquidity_signal()/get_cached_illiquid() (added 2 Sep
        # 2026, same cache-then-poll-refresh shape as _supertrend_cache
        # above, keyed by the OPTION's own trading_symbol rather than the
        # underlying since liquidity is a property of the specific
        # contract being held, not the underlying stock).
        self._liquidity_cache: dict[str, tuple[datetime, bool]] = {}
        # underlying_symbol -> "MCX"/"NSE" - see _expected_exchange_for
        # (added 23 Sep 2026, MCX WS LTP support). Never expires/resets
        # intraday - an underlying's own exchange classification is a
        # static instrument-master fact, not something that changes
        # during a session, unlike every time-based cache above. Without
        # this, get_cached_option_ltp/note_rest_ltp (both now on EVERY
        # package's per-position monitor-loop poll, not just once per
        # entry like the only other _is_mcx_commodity call site) would
        # re-run a full scrip-master DataFrame filter every ~2s for every
        # open position across all 4 packages, NSE ones included, just to
        # re-derive the same static answer each time.
        self._mcx_underlying_cache: dict[str, str] = {}
        # Nifty50 open gap-down/sharp-fall cool-off - see evaluate_nifty_
        # open_condition()/should_delay_ce_entry(). One dict, computed at
        # most once per calendar date (keyed by result["date"]), not a
        # dict-of-symbols like the caches above since Nifty is a single
        # market-wide fact shared by every strategy/symbol.
        self._nifty_open_condition_cache: Optional[dict] = None
        # (fetched_at, is_recovering) - see is_nifty_recovering(). Separate
        # from the cache above since this one refreshes repeatedly through
        # the morning (config.NIFTY_RECOVERY_REFRESH_SECONDS), not once.
        self._nifty_recovery_cache: Optional[tuple[datetime, bool]] = None
        # Observability: proves (or disproves) whether the WebSocket caches
        # are actually being used instead of REST, rather than assuming it.
        self.stats = {
            "ltp_cache_hits": 0,
            "ltp_cache_misses": 0,
            "ltp_cache_stale": 0,
            "order_status_cache_hits": 0,
            "order_status_rest_calls": 0,
            "price_ticks_received": 0,
            # Feed resilience visibility - added 31 Aug 2026 (user request).
            # CORRECTED 21 Sep 2026 (real incident, trading-skills'
            # incidents/2026-09-21-market-feed-thread-death-on-429.md):
            # this comment used to claim dhanhq's MarketFeed "already
            # self-heals on disconnect" - true only for its OWN internal
            # while-loop, reached only AFTER a first successful connect,
            # and even then with no backoff of its own. Its FIRST connect
            # attempt is unprotected (MarketFeed.run() only catches
            # KeyboardInterrupt) and a single failure there silently
            # killed the whole background thread forever - confirmed
            # live, twice. _run_market_feed_forever/_market_feed_
            # watchdog_forever now own resilience for both failure modes;
            # these counters remain pure observability on top of that.
            "feed_connects": 0,
            "feed_disconnects": 0,
            "feed_errors": 0,
        }
        # Bounds how many REST LTP-fallback calls can be in flight at once
        # across ALL strategies sharing this connection (Options + Futures,
        # via the shared dhan_wrapper singleton - Futures/dhan_client.py
        # just re-exports this instance) - added 31 Aug 2026 (user request,
        # resilience audit). Without this, multiple positions going stale
        # in the same poll tick (most likely during exactly the kind of
        # feed hiccup feed_connects/feed_disconnects above now surface)
        # would each fire their own REST call with zero pacing between
        # them - a burst against Dhan's known undocumented rate limit
        # (NOTES.md bug #5), worst-case exactly when the feed is already
        # struggling. A semaphore, not a fixed sleep, so the common case
        # (a cache hit, no REST needed at all) is completely unaffected -
        # this only throttles the REST-fallback path itself. Constructed
        # here (module-import time, before any event loop runs) - safe in
        # Python 3.10+, where asyncio.Semaphore no longer binds to a loop
        # at construction time.
        self.ltp_rest_fallback_semaphore = asyncio.Semaphore(2)

        # Cross-package throttle/backoff for fetch_continuous_intraday
        # (added 24 Sep 2026, see config.MARKET_DATA_MIN_INTERVAL_SECONDS'
        # own docstring for the full incident history/design). A plain
        # threading.Lock, not an asyncio primitive - every real caller
        # reaches this method from a worker thread (run_in_executor), never
        # directly on the event loop, so blocking here is the intended
        # behavior, not a bug. monotonic() throughout since this is a
        # pure elapsed-time budget, never a wall-clock one.
        self._market_data_lock = threading.Lock()
        self._market_data_next_allowed_at = 0.0
        self._market_data_cooldown_until = 0.0
        self._market_data_consecutive_rate_limit_hits = 0

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def authenticate(self) -> None:
        mode = config.DHAN_AUTH_MODE
        if mode == "pin_totp":
            # Session-collision guard (added 21 Sep 2026, real incident -
            # see trading-skills' incidents/2026-09-21-local-backtest-
            # dhan-session-collision.md). Dhan allows only ONE active
            # access-token session per account: a local backtest/diagnostic
            # script calling authenticate() in pin_totp mode mints a
            # genuinely new token that silently invalidates the LIVE
            # droplet bot's own current session (and vice versa), causing
            # a real, confirmed cascade of "could not fetch data" errors
            # bot-wide (mistaken at the time for a Swing-specific or
            # WebSocket bug - it wasn't; a WS-based data path depends on
            # this exact same session token and would collide identically).
            #
            # systemd sets INVOCATION_ID for every unit it starts
            # (unconditionally, no config needed) - a plain local
            # `python3`/`uv run` invocation never has it set. This is used
            # ONLY to distinguish "am I the actual deployed service" from
            # "am I an ad-hoc local script", never as a security boundary.
            # ALLOW_LOCAL_PIN_TOTP=true is the explicit, deliberate escape
            # hatch for the rare legitimate case (e.g. confirmed the
            # droplet is down) - never set this on a whim.
            running_under_systemd = "INVOCATION_ID" in os.environ
            allow_override = os.environ.get("ALLOW_LOCAL_PIN_TOTP", "").lower() == "true"
            if not running_under_systemd and not allow_override:
                raise RuntimeError(
                    "Refusing to authenticate via pin_totp from a local (non-systemd) process - "
                    "this would mint a new Dhan session and silently kick out the live droplet "
                    "bot's own current one (see incidents/2026-09-21-local-backtest-dhan-session-"
                    "collision.md). Use DHAN_AUTH_MODE=access_token with a hand-off "
                    "HANDOFF_DHAN_ACCESS_TOKEN from the user instead. If you have independently "
                    "confirmed the live bot is not running right now, set "
                    "ALLOW_LOCAL_PIN_TOTP=true to proceed anyway."
                )
            if not config.DHAN_CLIENT_ID or not config.DHAN_PIN or not config.DHAN_TOTP_SECRET:
                raise ValueError("DHAN_CLIENT_ID / DHAN_PIN / DHAN_TOTP_SECRET are not set")
            tsl = Tradehull(config.DHAN_CLIENT_ID, mode="pin_totp",
                             pin=config.DHAN_PIN, totp_secret=config.DHAN_TOTP_SECRET)
        else:
            if not config.DHAN_CLIENT_ID or not config.DHAN_ACCESS_TOKEN:
                raise ValueError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are not set")
            tsl = Tradehull(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN, mode="access_token")

        # Tradehull's __init__ swallows login failures internally (prints
        # and returns a half-initialized object instead of raising), so we
        # have to verify the attributes it only sets on success ourselves.
        if not getattr(tsl, "Dhan", None) or not getattr(tsl, "dhan_context", None):
            raise RuntimeError(
                f"Dhan login failed (mode={mode}) - Tradehull did not initialize its REST client. "
                "Check the relevant DHAN_* env vars and Tradehull's own console output above."
            )
        self._client = tsl
        # Shrink the per-request HTTP timeout from the SDK's own 60s
        # default (see config.DHAN_HTTP_TIMEOUT_SECONDS's own docstring
        # for the full audit finding this closes). dhan_http is a single
        # shared DhanHTTP instance every API call on this client routes
        # through - confirmed via the SDK's own source (DhanContext
        # constructs it once; every mixin gets the same instance via
        # get_dhan_http()) - so this one assignment bounds every call.
        # Best-effort: never let a change here block a successful login,
        # even if a future SDK version restructures this internal path.
        try:
            tsl.Dhan.dhan_http.timeout = config.DHAN_HTTP_TIMEOUT_SECONDS
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not set Dhan HTTP client's per-request timeout to %.1fs - proceeding with "
                "whatever the SDK's own default is (login itself succeeded)",
                config.DHAN_HTTP_TIMEOUT_SECONDS,
            )
        logger.info("Authenticated with Dhan (Tradehull, mode=%s, http_timeout=%.1fs)",
                    mode, config.DHAN_HTTP_TIMEOUT_SECONDS)

    @property
    def client(self) -> Tradehull:
        if self._client is None:
            self.authenticate()
        return self._client

    def instruments(self):
        """Tradehull's own cached scrip-master DataFrame (downloaded once
        per day on login) - reused here instead of downloading a second copy."""
        return self.client.instrument_df

    @staticmethod
    def _underlying_from_trading_symbol(sem_trading_symbol: str) -> str:
        """Derives the underlying from Dhan's SEM_TRADING_SYMBOL format -
        options: "{UNDERLYING}-{Mon}{YYYY}-{STRIKE}-{CE|PE}" (3 trailing
        segments); stock futures: "{UNDERLYING}-{Mon}{YYYY}-FUT" (2
        trailing segments, added 31 Aug 2026 alongside genuine futures-
        contract support for the Swing package). A naive split("-")[0]
        breaks for underlyings that themselves contain a hyphen - confirmed
        live: BAJAJ-AUTO's OPTION contracts are "BAJAJ-AUTO-Aug2026-10000-CE"
        (split("-")[0] mangles to just "BAJAJ"), and the exact same problem
        exists for its FUTURES contract, "BAJAJ-AUTO-Sep2026-FUT" - this
        function was only ever exercised against options until Swing's
        get_futures_contract() started calling it too, so the futures
        branch below was a real, if previously dormant (nothing in this
        codebase bought a real futures contract before), latent bug in
        get_open_fno_positions()/_instrument_meta_by_security_id() for any
        hyphenated-name stock future. Branches on the trailing segment
        ("FUT" vs an option type) since the caller doesn't know in advance
        which shape it's looking at (this is called for every broker
        position row, options and futures alike)."""
        parts = sem_trading_symbol.split("-")
        if parts and parts[-1] == "FUT":
            if len(parts) < 3:
                return parts[0]
            return "-".join(parts[:-2])
        if len(parts) < 4:
            return parts[0]
        return "-".join(parts[:-3])

    def _is_index_underlying(self, underlying_symbol: str) -> bool:
        """Data-driven check (same convention as _is_mcx_commodity right
        below - no hardcoded symbol list) - does a real NSE OPTIDX/FUTIDX
        contract for this underlying exist in the instrument master right
        now? Added 24 Sep 2026 for get_liquid_atm_option's own real bug:
        _is_contract_liquid_and_active hardcoded instrument_type="OPTSTK"
        for every non-MCX underlying, including an index - Dhan's real
        instrument master confirms NIFTY's own option rows are
        SEM_INSTRUMENT_NAME=="OPTIDX", not "OPTSTK" (verified live via a
        real instrument-master query, not assumed), so get_daily_volume_
        sum's historical_daily_data call was silently asking Dhan for the
        WRONG instrument type - liable to return empty/zero volume for
        even NIFTY's own ATM strike (the single most liquid F&O contract
        on the exchange) and incorrectly reject it as illiquid. Used
        alongside _is_mcx_commodity (mutually exclusive in practice - an
        underlying is never both) to pick the right instrument_type string
        for the liquidity/volume checks below."""
        df = self.instruments()
        return bool((
            (df["SEM_EXM_EXCH_ID"] == "NSE")
            & (df["SEM_INSTRUMENT_NAME"].isin(["OPTIDX", "FUTIDX"]))
            & (df["SEM_CUSTOM_SYMBOL"].str.startswith(underlying_symbol.upper() + " "))
        ).any())

    def _is_mcx_commodity(self, underlying_symbol: str) -> bool:
        """Data-driven check (no hardcoded symbol list, no cross-package
        config import needed) - does a real MCX FUTCOM contract for this
        underlying exist in the instrument master right now? Same
        STARTSWITH convention _get_mcx_futures_contract_once already uses.
        Used by _get_atm_option_once to tell _instrument_meta which
        exchange it actually wants (see that function's own
        expected_exchange docstring for the real incident this exists to
        prevent)."""
        df = self.instruments()
        return bool((
            (df["SEM_EXM_EXCH_ID"] == "MCX")
            & (df["SEM_INSTRUMENT_NAME"] == "FUTCOM")
            & (df["SEM_TRADING_SYMBOL"].str.startswith(underlying_symbol + "-"))
        ).any())

    def _instrument_meta(self, trading_symbol: str, expected_exchange: Optional[str] = None) -> dict:
        """Looks up an instrument by trading_symbol string. NOTE: the scrip
        master is not guaranteed unique on SEM_TRADING_SYMBOL (confirmed
        live - two different SBIN option contracts shared the exact same
        SEM_TRADING_SYMBOL with different SEM_SMST_SECURITY_ID). Only use
        this for symbols we just got directly from Tradehull (e.g.
        ATM_Strike_Selection's return) where there's no better key to match
        on; prefer _instrument_meta_by_security_id() whenever a security_id
        is already available (e.g. from a broker position record).

        Exchange filter widened NSE-only -> NSE-or-MCX (12 Sep 2026, Swing
        v2's Copper support) - Tradehull's own ATM_Strike_Selection ALREADY
        natively resolves a valid MCX OPTFUT SEM_CUSTOM_SYMBOL for a
        commodity underlying (it has its own commodity_step_dict branch);
        this function was the only thing rejecting the row it hands back.

        `expected_exchange` (added 14 Sep 2026 - REAL LIVE BUG found while
        investigating Copper's margin requirements, not theoretical):
        Dhan's own instrument master is not even guaranteed unique ACROSS
        exchanges on this symbol-string match, contrary to this function's
        original "NSE and MCX symbol strings look nothing alike" assumption
        above. Confirmed live: "COPPER-23Sep2026-1360-CE" / "COPPER 23 SEP
        1360 CALL" matches BOTH a genuine MCX OPTFUT row (security_id
        574852, expiry 23-Sep 23:30 - a real MCX Copper option expiry time)
        AND a bogus row tagged SEM_EXM_EXCH_ID="NSE" with an invalid expiry
        time (security_id 123250, expiry 20:00 - not a real NSE or MCX
        session time), which looks like corrupted/duplicate data in Dhan's
        own scrip master rather than a real tradeable instrument. Without
        a hint, `.iloc[-1]` below picked the bogus NSE row - get_atm_option
        ("COPPER", "CE") would have handed a live entry a security_id that
        doesn't correspond to any real instrument. Every call site that
        knows which exchange it actually wants (all of them do - each one
        either only ever deals with NSE_FNO, or MCX_COMM specifically) now
        passes it, so the ambiguous cross-exchange row can never win a
        tiebreak it has no business being in."""
        df = self.instruments()
        row = df[
            ((df["SEM_TRADING_SYMBOL"] == trading_symbol) | (df["SEM_CUSTOM_SYMBOL"] == trading_symbol))
            & (df["SEM_EXM_EXCH_ID"].isin(["NSE", "MCX"]))
        ]
        if expected_exchange is not None:
            row = row[row["SEM_EXM_EXCH_ID"] == expected_exchange]
        if row.empty:
            suffix = f" on {expected_exchange}" if expected_exchange else ""
            raise ValueError(f"No instrument found for trading_symbol {trading_symbol}{suffix}")
        r = row.iloc[-1]
        return {
            "security_id": str(int(r["SEM_SMST_SECURITY_ID"])),
            "lot_size": int(float(r["SEM_LOT_UNITS"])),
            "underlying_symbol": self._underlying_from_trading_symbol(str(r["SEM_TRADING_SYMBOL"])),
            # Used to skip a doomed entry on a stock option's own expiry day
            # (see get_atm_option's expiry-day guard) - Tradehull's own
            # ATM_Strike_Selection parses this same column the same way
            # (pd.to_datetime(...).dt.date) internally.
            "expiry_date": pd.to_datetime(r["SEM_EXPIRY_DATE"], errors="coerce").date(),
            # Dhan's own SEM_TICK_SIZE is in PAISE (e.g. 5.0 = Rs.0.05), not
            # rupees - added 9 Sep 2026 for place_stop_loss_limit_order's
            # own tick-rounding (see _round_to_tick's docstring for why
            # this is required, not optional, for a COMPUTED trigger/limit
            # price). Divided here so every caller gets a rupee value
            # directly, matching every other price field in this codebase.
            # NOT verified for an MCX row specifically (12 Sep 2026) - the
            # /100 convention is only confirmed against NSE instruments so
            # far. Harmless today since Swing's BROKER_STOP_LOSS_ENABLED
            # stays off for Copper in this first rollout (place_mcx_stop_
            # loss_limit_order, the only caller that would use this for an
            # MCX row, isn't wired into any live entry path yet) - verify
            # this against a real MCX tick before ever turning that on.
            "tick_size": float(r["SEM_TICK_SIZE"]) / 100.0,
        }

    def _instrument_meta_by_security_id(self, security_id: str) -> dict:
        """Looks up an instrument by its unique SEM_SMST_SECURITY_ID -
        unlike SEM_TRADING_SYMBOL, this key IS unique, so prefer this
        whenever the security_id is already known (e.g. from a Dhan
        position record). Returns `trading_symbol` in SEM_CUSTOM_SYMBOL
        format ("RELIANCE 25 AUG 1310 CALL" style) since that's the format
        Tradehull's own REST methods (get_ltp_data, order_placement, ...)
        reliably match against - they force-uppercase the symbol before
        matching, which silently breaks against SEM_TRADING_SYMBOL's mixed-
        case month format ("SBIN-Aug2026-1100-CE" - confirmed live: this
        broke get_ltp_data with a bare "Check the Tradingsymbol" failure)."""
        df = self.instruments()
        row = df[df["SEM_SMST_SECURITY_ID"].astype(str) == str(security_id)]
        if row.empty:
            raise ValueError(f"No instrument found for security_id {security_id}")
        r = row.iloc[0]
        return {
            "trading_symbol": str(r["SEM_CUSTOM_SYMBOL"]),
            "lot_size": int(float(r["SEM_LOT_UNITS"])),
            "underlying_symbol": self._underlying_from_trading_symbol(str(r["SEM_TRADING_SYMBOL"])),
        }

    def _equity_security_id(self, underlying_symbol: str) -> str:
        """Resolves an underlying's own NSE cash-segment security_id (for
        Supertrend candle fetches) - distinct from any of its option
        contracts' security_ids. Filters SEM_SERIES=="EQ" (not just
        SEM_INSTRUMENT_NAME=="EQUITY") - found live 26 Sep 2026: MOTHERSON
        has TWO NSE EQUITY rows sharing the exact SEM_TRADING_SYMBOL
        "MOTHERSON", a live equity share (SEM_SERIES="EQ") and an unrelated
        listed non-convertible debenture (SEM_SERIES="D1", SEM_CUSTOM_SYMBOL
        "SMIL-6.5%-20092027-NCD") - row.iloc[0] silently picked the bond,
        which has its own (near-empty) intraday candle history, so a
        Supertrend/regime fetch for MOTHERSON would silently get zero bars
        instead of a clean error. "EQ" is NSE's standard cash-equity series
        code; a bond/debenture series (D1 etc.) is never a valid answer here."""
        df = self.instruments()
        row = df[
            (df["SEM_EXM_EXCH_ID"] == "NSE")
            & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")
            & (df["SEM_SERIES"] == "EQ")
            & (df["SEM_TRADING_SYMBOL"] == underlying_symbol)
        ]
        if row.empty:
            raise ValueError(f"No NSE equity instrument found for {underlying_symbol}")
        return str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))

    def _equity_instrument_meta(self, underlying_symbol: str) -> dict:
        """Equity-segment counterpart to _instrument_meta (added 12 Sep 2026,
        Swing v2's equity basket-type) - deliberately does NOT reuse
        _instrument_meta itself, which matches on SEM_EXM_EXCH_ID=="NSE"
        alone and takes row.iloc[-1], the exact non-uniqueness footgun that
        function's own docstring warns about (it's built for options/
        futures trading symbols, not plain equity). This uses the same
        precise EQUITY+SEM_SERIES=="EQ"+exact-symbol filter as
        _equity_security_id above (see that function's docstring for the
        real MOTHERSON bond/equity collision this guards against), so
        there's exactly one matching row, and returns the tick size in
        the same rupee-converted form _instrument_meta does (Dhan reports
        SEM_TICK_SIZE in paise)."""
        df = self.instruments()
        row = df[
            (df["SEM_EXM_EXCH_ID"] == "NSE")
            & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")
            & (df["SEM_SERIES"] == "EQ")
            & (df["SEM_TRADING_SYMBOL"] == underlying_symbol)
        ]
        if row.empty:
            raise ValueError(f"No NSE equity instrument found for {underlying_symbol}")
        r = row.iloc[0]
        return {
            "security_id": str(int(r["SEM_SMST_SECURITY_ID"])),
            "lot_size": int(float(r["SEM_LOT_UNITS"])) or 1,
            "tick_size": float(r["SEM_TICK_SIZE"]) / 100.0,
        }

    # ------------------------------------------------------------------ #
    # Market hours (Dhan requires an explicit afterMarketOrder flag - unlike
    # Groww it does NOT auto-detect AMO from placement time)
    # ------------------------------------------------------------------ #
    def is_market_open(self, exchange_segment: str = "NSE_FNO") -> bool:
        """Defaults to NSE F&O hours (config.MARKET_OPEN_TIME/_CLOSE_TIME) -
        every existing caller (Options/Futures/Luxury's own EOD-gating
        logic, plus place_market_order/place_equity_market_order) is
        NSE-only and relies on that default unchanged. Pass
        exchange_segment="MCX_COMM" for an MCX order/check instead - MCX's
        session runs materially longer than NSE's (see config.MCX_MARKET_
        OPEN_TIME/_CLOSE_TIME's own docstring for the real incident this
        fixed: an MCX order placed well within MCX's live evening session
        used to be wrongly tagged AMO because this checked only NSE hours,
        which caused Swing to treat it as a failed entry and rapid-retry,
        placing several duplicate real orders before this was fixed)."""
        now = datetime.now(IST).time()
        if exchange_segment == "MCX_COMM":
            open_t = datetime.strptime(config.MCX_MARKET_OPEN_TIME, "%H:%M").time()
            close_t = datetime.strptime(config.MCX_MARKET_CLOSE_TIME, "%H:%M").time()
        else:
            open_t = datetime.strptime(config.MARKET_OPEN_TIME, "%H:%M").time()
            close_t = datetime.strptime(config.MARKET_CLOSE_TIME, "%H:%M").time()
        return open_t <= now <= close_t

    # ------------------------------------------------------------------ #
    # Live feed (WebSocket)
    # ------------------------------------------------------------------ #
    @property
    def order_update_feed(self) -> OrderUpdate:
        if self._order_update is None:
            feed = OrderUpdate(self.client.dhan_context)
            feed.on_update = self._on_order_update
            self._order_update = feed
            threading.Thread(target=self._run_order_update_forever, daemon=True).start()
            logger.info("Dhan order-update WebSocket connecting in the background.")
        return self._order_update

    def _run_order_update_forever(self) -> None:
        # connect_to_dhan_websocket_sync() is blocking and does not
        # auto-reconnect on its own (confirmed from dhanhq's orderupdate.py
        # source), so we own the retry loop here.
        while True:
            try:
                self._order_update.connect_to_dhan_websocket_sync()
            except Exception:  # noqa: BLE001
                logger.exception("Order-update WebSocket dropped; reconnecting in 5s")
            time.sleep(5)

    def _on_order_update(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        data = message.get("Data") or message.get("data") or {}
        order_id = data.get("orderNo") or data.get("orderId") or data.get("order_id")
        if order_id:
            self._order_updates[str(order_id)] = data

    def _on_market_connect(self, _feed) -> None:
        # Fires on the INITIAL connect AND every auto-reconnect (dhanhq's
        # own connect() calls this every time it (re)establishes the
        # socket) - see MarketFeed.connect()'s own source. Counting this
        # separately from "process started" is what makes a mid-session
        # reconnect actually visible instead of silently self-healed.
        self.stats["feed_connects"] += 1
        logger.info("Dhan market-data WebSocket connected (connect #%d this run).",
                    self.stats["feed_connects"])

    def _on_market_close(self, _feed) -> None:
        self.stats["feed_disconnects"] += 1
        logger.warning("Dhan market-data WebSocket disconnected (disconnect #%d this run).",
                        self.stats["feed_disconnects"])

    def _on_market_error(self, _feed, exc) -> None:
        self.stats["feed_errors"] += 1
        logger.warning("Dhan market-data WebSocket error (#%d this run): %s",
                        self.stats["feed_errors"], exc)

    def _run_market_feed_forever(self) -> None:
        """Owns the market-data WebSocket's entire connection lifecycle,
        including its very FIRST connection attempt - added 21 Sep 2026
        after a real incident (trading-skills' incidents/2026-09-21-
        market-feed-thread-death-on-429.md). dhanhq's own MarketFeed.run()
        only catches KeyboardInterrupt, and its _run_async()'s first
        `await self.connect()` call sits OUTSIDE the while loop's own
        try/except - so a single HTTP 429 on that very first attempt
        silently killed the whole background thread forever, zero further
        retries, confirmed live twice the same day. The comment that used
        to sit on _on_market_close ("dhanhq's own MarketFeed auto-
        reconnects internally") was true only for a session that had
        already connected once - it was never true of the very first
        attempt, and that gap is exactly what bit us.

        This method wraps the ENTIRE blocking feed.run() call in its own
        infinite retry loop with exponential backoff
        (config.MARKET_FEED_BACKOFF_BASE_SECONDS, doubling up to
        config.MARKET_FEED_BACKOFF_MAX_SECONDS) - the same "own the whole
        call, don't trust the SDK's internals" discipline
        _run_order_update_forever already uses for the order-update feed,
        just with backoff instead of a flat retry delay since a fixed
        short delay is exactly what turned one 429 into a sustained
        multi-hundred-error storm the OTHER failure mode seen the same
        incident (see _market_feed_watchdog_forever for why that second
        mode needs its own separate handling - it happens INSIDE a
        feed.run() call that never returns control here).

        A brand new MarketFeed is constructed on every attempt, seeded
        with self._market_feed_instruments (our own authoritative
        subscribed-instrument set) so a reconnect automatically
        resubscribes everything already subscribed before the drop - see
        connect()'s own subscribe_instruments() call in the vendored SDK,
        which sends the constructor's own instrument list fresh on every
        successful open."""
        delay = config.MARKET_FEED_BACKOFF_BASE_SECONDS
        while True:
            started_at = time.monotonic()
            try:
                with self._market_feed_lock:
                    instruments = list(self._market_feed_instruments)
                feed = MarketFeed(
                    self.client.dhan_context, instruments, version="v2", on_ticks=self._on_market_tick,
                    on_connect=self._on_market_connect, on_close=self._on_market_close,
                    on_error=self._on_market_error,
                )
                with self._market_feed_lock:
                    self._market_feed = feed
                feed.run()  # blocking - returns/raises only once the feed's own loop has died
                logger.warning("Market-data WebSocket feed exited cleanly (unexpected) - reconnecting in %.0fs.", delay)
            except Exception:  # noqa: BLE001
                logger.exception("Market-data WebSocket feed thread died - reconnecting in %.0fs.", delay)
            with self._market_feed_lock:
                self._market_feed = None
            if time.monotonic() - started_at >= config.MARKET_FEED_BACKOFF_RESET_AFTER_SECONDS:
                delay = config.MARKET_FEED_BACKOFF_BASE_SECONDS
            time.sleep(delay)
            delay = min(delay * 2, config.MARKET_FEED_BACKOFF_MAX_SECONDS)

    def _market_feed_watchdog_forever(self) -> None:
        """Handles the OTHER failure mode from the same 21 Sep 2026
        incident: once dhanhq's MarketFeed gets PAST its first connect,
        its own internal loop (_run_async's `while self._running:`) DOES
        reconnect on its own on disconnect/error - but at a fixed ~1s
        cadence with no backoff of its own, so once Dhan's WS endpoint
        starts rate-limiting reconnects, the SDK just hammers it
        continuously instead of ever leaving the endpoint a quiet gap to
        clear (confirmed live: feed_errors climbed ~1/sec for 25+ minutes
        straight, feed_connects frozen, zero ticks the entire time).
        feed.run() never returns control to _run_market_feed_forever
        while stuck this way - that inner loop lives entirely inside the
        vendored SDK - so this watchdog polls self.stats["feed_errors"]
        from OUTSIDE instead, and force-closes the feed if the error rate
        looks like a stuck storm rather than an occasional blip. Closing
        it makes feed.run() return (not raise), which hands control back
        to _run_market_feed_forever's own outer loop - THAT loop is what
        actually applies the backoff delay before the next attempt, since
        this watchdog has no delay logic of its own."""
        last_errors = self.stats["feed_errors"]
        while True:
            time.sleep(config.MARKET_FEED_WATCHDOG_INTERVAL_SECONDS)
            if not config.ENABLE_WS_FEED:
                continue
            errors_now = self.stats["feed_errors"]
            error_rate = errors_now - last_errors
            last_errors = errors_now
            if error_rate < config.MARKET_FEED_WATCHDOG_ERROR_THRESHOLD:
                continue
            with self._market_feed_lock:
                feed = self._market_feed
            if feed is None:
                continue
            logger.warning(
                "Market-data WebSocket watchdog: %d error(s) in the last %.0fs looks like a stuck "
                "reconnect storm - force-closing so the supervisor's own backoff can take over.",
                error_rate, config.MARKET_FEED_WATCHDOG_INTERVAL_SECONDS,
            )
            try:
                feed.close_connection()
            except Exception:  # noqa: BLE001
                logger.exception("Market-data WebSocket watchdog: force-close itself failed")

    @property
    def market_feed(self) -> Optional[MarketFeed]:
        """Returns the CURRENT MarketFeed instance, or None if the
        supervisor thread (_run_market_feed_forever) hasn't constructed
        one yet - briefly, right after the very first access, or any time
        it's between reconnect attempts. Callers (subscribe_option_price/
        unsubscribe_option_price) tolerate None - see their own
        docstrings for why that's safe (the authoritative instrument set
        those two update is what actually drives resubscription on the
        NEXT reconnect, not a direct call on whatever instance happens to
        exist right now).

        Lazily starts BOTH the supervisor thread and its watchdog on
        first access. Unlike before 21 Sep 2026, this does NOT call
        feed.start() / trust dhanhq's own MarketFeed to survive its own
        first connection attempt - see _run_market_feed_forever's own
        docstring for the incident that made that trust look misplaced."""
        if not self._market_feed_thread_started:
            self._market_feed_thread_started = True
            threading.Thread(target=self._run_market_feed_forever, daemon=True,
                              name="market-feed-supervisor").start()
            threading.Thread(target=self._market_feed_watchdog_forever, daemon=True,
                              name="market-feed-watchdog").start()
            logger.info("Dhan market-data WebSocket supervisor + watchdog starting in the background.")
        return self._market_feed

    def add_price_tick_subscriber(self, callback: Callable[[str, float], None]) -> None:
        """Registers a callback to fire on every price tick, as
        (trading_symbol, ltp) - see _on_price_tick_subscribers' docstring
        for why this is additive rather than a single-slot assignment."""
        self._on_price_tick_subscribers.append(callback)

    def add_quote_tick_subscriber(self, callback: Callable[[str, float, float, datetime], None]) -> None:
        """Registers a callback to fire on every Quote/Full-mode packet
        (added 21 Sep 2026 for underlying_candle_feed.py), as
        (underlying_symbol, ltp, day_cumulative_volume, tick_time). Never
        fires for a Ticker-mode packet (options remain Ticker-subscribed,
        no volume field exists there) - see _on_market_tick's own routing.
        Additive list, same rationale as add_price_tick_subscriber above."""
        self._on_quote_tick_subscribers.append(callback)

    def _on_market_tick(self, _feed, tick: dict) -> None:
        # Runs on the MarketFeed's own background thread, not the asyncio
        # event loop - on_price_tick (if set) is responsible for hopping
        # back onto the event loop safely (see main.py's wiring of it).
        if not isinstance(tick, dict):
            return
        security_id = tick.get("security_id")
        ltp = tick.get("LTP")
        if security_id is None or ltp is None:
            return
        try:
            ltp_val = float(ltp)
        except (TypeError, ValueError):
            return
        if ltp_val <= 0:
            # Real incident (25 Sep 2026): a malformed/keepalive packet with
            # LTP=0 for COPPER (pre-market, before this guard existed) got
            # cached and dispatched like any real tick - it became a WS
            # candle's bootstrap price in Swing/candle_feed.py's own
            # _update_bar (now separately guarded there too - see that
            # module's comment), corrupting a structure-break regime read.
            # Investigating that led here: this single dispatch point feeds
            # EVERY subscriber (on_price_tick AND on_quote_tick) across all
            # four packages (Options/Futures/Luxury/Swing all reuse this one
            # Dhan connection) - including on_price_tick's own live exit
            # check (_exit_reason_for in each package's trading_engine.py),
            # where `ltp <= trailing_sl` is unconditional: a single ltp=0
            # tick for ANY symbol with an open real position would have
            # forced an immediate false STOP_LOSS_HIT exit, no candle-feed
            # involvement needed. A zero/negative price is never a real
            # trade on any exchange this feed subscribes to - reject it
            # here, once, before it reaches anything downstream (self.
            # _ltp_cache included - a stale-but-real cached value is safer
            # left untouched than overwritten with 0).
            return

        security_id = str(security_id)
        now = datetime.now(IST)
        self._ltp_cache[security_id] = ltp_val
        self._ltp_cache_ts[security_id] = now
        self.stats["price_ticks_received"] += 1

        if self._on_price_tick_subscribers:
            trading_symbol = self._security_id_to_symbol.get(security_id)
            if trading_symbol:
                for callback in self._on_price_tick_subscribers:
                    try:
                        callback(trading_symbol, ltp_val)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_price_tick subscriber failed for %s", trading_symbol)

        # Quote/Full packets only (process_quote/process_full both set a
        # "volume" key - Ticker packets, process_ticker, never do) - routed
        # via the SEPARATE equity/MCX lookup dicts, never _security_id_to_
        # symbol, so this can never fire for an option tick or cross-
        # contaminate the two instrument spaces (see _equity_security_id_
        # to_symbol's own docstring for the incident that makes this worth
        # being explicit about). Disambiguated by the tick's OWN exchange_
        # segment (not just "which dict happens to have this numeric id"),
        # since security_id is not unique across segments - a bare dict
        # lookup without this check is exactly the class of bug
        # _mcx_security_id_to_symbol's own docstring describes.
        if self._on_quote_tick_subscribers and "volume" in tick:
            tick_segment = tick.get("exchange_segment")
            if tick_segment == MarketFeed.MCX:
                underlying_symbol = self._mcx_security_id_to_symbol.get(security_id)
            elif tick_segment == MarketFeed.IDX:
                # An index has no real traded volume of its own - IF Dhan's
                # Quote/Full packet for one still carries a "volume" key
                # (unverified as of 24 Sep 2026 - watch candle_feed's own
                # snapshot endpoint after deploy to confirm ticks actually
                # arrive here, not just that subscribe_symbols() accepted
                # the instrument), it should read 0, matching what the REST
                # candle series already reports for NIFTY/BANKNIFTY too -
                # _update_bar's own OHLC reconstruction never depends on
                # volume being nonzero, only on LTP + LTT. If Dhan omits the
                # key entirely for IDX_I, this whole branch silently never
                # fires and Swing/candle_feed.py's own is_fresh() check
                # correctly keeps the symbol on its existing REST fallback -
                # fails safe either way, never a crash or a wrong price.
                underlying_symbol = self._index_security_id_to_symbol.get(security_id)
            else:
                underlying_symbol = self._equity_security_id_to_symbol.get(security_id)
            if underlying_symbol is not None:
                try:
                    volume_val = float(tick["volume"])
                except (TypeError, ValueError):
                    volume_val = None
                if volume_val is not None:
                    # Bucket by the exchange's own trade time (LTT), not
                    # local receipt time - see _tick_time_from_ltt's own
                    # docstring for the real incident this fixes and the
                    # live-confirmed timezone verification behind it.
                    # Deliberately does NOT touch `now`/_ltp_cache_ts above -
                    # those answer "how fresh is our own knowledge of the
                    # price" (correctly local-receipt-time-based) which is a
                    # different question from "which 5-min bar does this
                    # trade belong to".
                    trade_time = _tick_time_from_ltt(tick.get("LTT"), now)
                    for callback in self._on_quote_tick_subscribers:
                        try:
                            callback(underlying_symbol, ltp_val, volume_val, trade_time)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_quote_tick subscriber failed for %s", underlying_symbol)

    def start_feed(self) -> None:
        """Eagerly opens both socket connections (otherwise they lazily open
        on first use). Call once at app startup."""
        if not config.ENABLE_WS_FEED:
            logger.info("WebSocket feed disabled (ENABLE_WS_FEED=false); running REST-only.")
            return
        _ = self.order_update_feed
        _ = self.market_feed

    def _expected_exchange_for(self, trading_symbol: str) -> str:
        """Returns "MCX" or "NSE" for a trading_symbol whose exchange isn't
        already known by the caller (added 23 Sep 2026, MCX WS LTP support -
        see subscribe_option_price's own updated docstring). Reuses
        _is_mcx_commodity (already trusted elsewhere, e.g.
        _get_atm_option_once) against the trading_symbol's own underlying
        token. Safe to take trading_symbol.split()[0] here specifically
        because this is Tradehull's own SPACE-separated trading_symbol
        format ("NATURALGAS 23 OCT 290 CALL"), not Dhan's raw hyphen-
        separated SEM_TRADING_SYMBOL - the underlying is always one
        whitespace token regardless of internal hyphens (e.g. "BAJAJ-AUTO
        23 SEP 10000 CALL" still splits correctly), unlike SEM_TRADING_
        SYMBOL's own hyphen-delimited shape which needs _underlying_from_
        trading_symbol's dedicated parsing instead. Cached per underlying
        via _mcx_underlying_cache - see that dict's own docstring for why
        (this is now called on every WS-cache LTP read, not just once per
        entry)."""
        underlying = trading_symbol.split()[0] if trading_symbol else ""
        if not underlying:
            return "NSE"
        cached = self._mcx_underlying_cache.get(underlying)
        if cached is not None:
            return cached
        result = "MCX" if self._is_mcx_commodity(underlying) else "NSE"
        self._mcx_underlying_cache[underlying] = result
        return result

    def is_mcx_commodity(self, underlying_symbol: str) -> bool:
        """Public, cached counterpart to _expected_exchange_for for callers
        that already have a bare underlying symbol (e.g. a Swing watchlist
        entry like "SILVER100"), not Tradehull's space-separated
        trading_symbol format _expected_exchange_for parses via .split()[0].
        Added 25 Sep 2026 so Swing/config.py's old MCX_SYMBOLS - a static
        set built once from SWING_MCX_SYMBOLS at process startup, requiring
        an .env edit AND a restart before a newly added MCX symbol's
        regime/Supertrend data would even start flowing - could be replaced
        with a real, always-current instrument-master answer: no config,
        no restart, and it can never drift from what Dhan can actually
        trade right now (see Swing/mcx_registry.py for the two things this
        genuinely CAN'T tell you - whether a symbol should always trade
        options regardless of the global BASKET_TYPE, and its real per-lot
        P&L multiplier - which stay manually configured there instead).
        Shares _mcx_underlying_cache with _expected_exchange_for since both
        answer the exact same static per-underlying question."""
        symbol = underlying_symbol.upper()
        cached = self._mcx_underlying_cache.get(symbol)
        if cached is not None:
            return cached == "MCX"
        result = "MCX" if self._is_mcx_commodity(symbol) else "NSE"
        self._mcx_underlying_cache[symbol] = result
        return result == "MCX"

    def subscribe_option_price(self, trading_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        # NSE_FNO or MCX subscription (MCX WS support added 23 Sep 2026 -
        # previously NSE_FNO-only, see Swing/trading_engine.py's own
        # now-updated guard). expected_exchange pins the lookup so a
        # symbol-string collision with an unrelated row on the other
        # exchange (see _instrument_meta's own docstring) can never win
        # here. _on_market_tick's own LTP-cache/price-tick-subscriber path
        # is already segment-agnostic (keyed purely by security_id, no
        # exchange check) - only the SUBSCRIBE side was ever NSE_FNO-only.
        expected_exchange = self._expected_exchange_for(trading_symbol)
        is_mcx = expected_exchange == "MCX"
        meta = self._instrument_meta(trading_symbol, expected_exchange=expected_exchange)
        self._security_id_to_symbol[meta["security_id"]] = trading_symbol
        segment = MarketFeed.MCX if is_mcx else MarketFeed.NSE_FNO
        instrument = (segment, meta["security_id"], MarketFeed.Ticker)
        # Update our OWN authoritative set first (21 Sep 2026 - see
        # _run_market_feed_forever's docstring) - this is what a FUTURE
        # reconnect resubscribes from, independent of whether a feed
        # instance currently exists. Then, if a feed happens to be live
        # right now, also push the subscription immediately rather than
        # waiting for the next reconnect cycle.
        with self._market_feed_lock:
            self._market_feed_instruments.add(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.subscribe_symbols([instrument])

    def unsubscribe_option_price(self, trading_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        expected_exchange = self._expected_exchange_for(trading_symbol)
        is_mcx = expected_exchange == "MCX"
        meta = self._instrument_meta(trading_symbol, expected_exchange=expected_exchange)
        security_id = meta["security_id"]
        self._security_id_to_symbol.pop(security_id, None)
        # Found + fixed 31 Aug 2026 (user request, "memory issues" audit):
        # this always cleaned the subscription itself, but _ltp_cache/
        # _ltp_cache_ts (populated by every tick/REST-fallback while this
        # symbol was subscribed) were never cleared - a real, if slow,
        # unbounded leak keyed by every distinct option contract ever
        # traded across the process's entire lifetime (strikes/expiries
        # change constantly, so this key space never naturally caps the
        # way _supertrend_cache's underlying-symbol keys do). Small in
        # absolute terms (confirmed: ~1-2MB/year at realistic trade
        # volumes) but a real bug, not just theoretical - worth closing
        # properly rather than leaving it to slowly accumulate forever on
        # a memory-constrained droplet.
        self._ltp_cache.pop(security_id, None)
        self._ltp_cache_ts.pop(security_id, None)
        segment = MarketFeed.MCX if is_mcx else MarketFeed.NSE_FNO
        instrument = (segment, security_id, MarketFeed.Ticker)
        with self._market_feed_lock:
            self._market_feed_instruments.discard(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.unsubscribe_symbols([instrument])

    def subscribe_equity_quote(self, underlying_symbol: str) -> None:
        """Equity-segment counterpart to subscribe_option_price (added 21
        Sep 2026 for underlying_candle_feed.py) - subscribes underlying_
        symbol's own NSE cash-segment security_id in Quote mode (volume-
        bearing, unlike option's Ticker mode) on the SAME shared market-
        feed WebSocket connection, going through the exact same
        reconnect-safe _market_feed_instruments/subscribe_symbols path as
        every option subscription already does. Idempotent - resubscribing
        an already-subscribed symbol is a harmless no-op (dhanhq's own
        subscribe_symbols de-dupes via a set)."""
        if not config.ENABLE_WS_FEED:
            return
        security_id = self._equity_security_id(underlying_symbol)
        self._equity_security_id_to_symbol[security_id] = underlying_symbol
        instrument = (MarketFeed.NSE, security_id, MarketFeed.Quote)
        with self._market_feed_lock:
            self._market_feed_instruments.add(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.subscribe_symbols([instrument])

    def unsubscribe_equity_quote(self, underlying_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        security_id = self._equity_security_id(underlying_symbol)
        self._equity_security_id_to_symbol.pop(security_id, None)
        instrument = (MarketFeed.NSE, security_id, MarketFeed.Quote)
        with self._market_feed_lock:
            self._market_feed_instruments.discard(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.unsubscribe_symbols([instrument])

    def subscribe_mcx_quote(self, underlying_symbol: str, security_id: str) -> None:
        """MCX-segment counterpart to subscribe_equity_quote above (added 23
        Sep 2026 for Swing/candle_feed.py). Takes security_id directly
        rather than resolving it internally - unlike an NSE equity symbol,
        an MCX symbol's tradeable instrument is a specific FUTURES CONTRACT
        that rolls monthly (see Swing/signals.py's own get_mcx_futures_
        contract/_underlying_reference), so the caller already has to
        resolve it fresh periodically and is better placed to detect a
        roll than this method would be. Routed through the SEPARATE
        _mcx_security_id_to_symbol dict, never the equity one - see that
        dict's own docstring for the real cross-segment collision this
        avoids."""
        if not config.ENABLE_WS_FEED:
            return
        self._mcx_security_id_to_symbol[security_id] = underlying_symbol
        instrument = (MarketFeed.MCX, security_id, MarketFeed.Quote)
        with self._market_feed_lock:
            self._market_feed_instruments.add(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.subscribe_symbols([instrument])

    def unsubscribe_mcx_quote(self, underlying_symbol: str, security_id: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        self._mcx_security_id_to_symbol.pop(security_id, None)
        instrument = (MarketFeed.MCX, security_id, MarketFeed.Quote)
        with self._market_feed_lock:
            self._market_feed_instruments.discard(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.unsubscribe_symbols([instrument])

    def subscribe_index_quote(self, underlying_symbol: str, security_id: str) -> None:
        """IDX_I-segment counterpart to subscribe_mcx_quote above (added 24
        Sep 2026, user request - "make it WS feeds based for IDX_I also",
        extending Swing/candle_feed.py's WS candle reconstruction to
        NIFTY/BANKNIFTY). Takes security_id directly rather than resolving
        it internally - same reasoning as subscribe_mcx_quote (the caller,
        Swing/signals.py's _underlying_reference, already resolves it via
        dhan_wrapper.index_security_id() for the REST path, no reason to
        resolve it twice). Routed through the SEPARATE _index_security_id_
        to_symbol dict, never the equity or MCX ones - see that dict's own
        docstring for the collision this avoids. MarketFeed.IDX is 0
        (confirmed against dhanhq's own marketfeed.py - get_exchange_segment
        maps IDX -> "IDX_I", the exact same segment string _underlying_
        reference's own REST path already uses), so this is a genuine
        equal-with, not an accidental falsy-int bug."""
        if not config.ENABLE_WS_FEED:
            return
        self._index_security_id_to_symbol[security_id] = underlying_symbol
        instrument = (MarketFeed.IDX, security_id, MarketFeed.Quote)
        with self._market_feed_lock:
            self._market_feed_instruments.add(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.subscribe_symbols([instrument])

    def unsubscribe_index_quote(self, underlying_symbol: str, security_id: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        self._index_security_id_to_symbol.pop(security_id, None)
        instrument = (MarketFeed.IDX, security_id, MarketFeed.Quote)
        with self._market_feed_lock:
            self._market_feed_instruments.discard(instrument)
            feed = self._market_feed
        if feed is not None:
            feed.unsubscribe_symbols([instrument])

    def get_cached_option_ltp(self, trading_symbol: str) -> Optional[float]:
        """Returns the last price pushed over the WebSocket feed for this
        option, or None if not subscribed yet / no tick has arrived yet /
        the feed is disabled (ENABLE_WS_FEED=false) / the cached tick is
        older than config.LTP_STALE_AFTER_SECONDS.

        The staleness check exists because a thinly-traded option can go
        minutes between real WebSocket ticks (no trade printing on it) while
        its underlying/premium keeps moving - confirmed live 28 Aug 2026:
        SAGILITY's MAX_LOSS_HIT overshot its Rs.1200 cap by Rs.600 because
        the cached LTP was stale for ~2 minutes and nothing forced a fresh
        REST check in the meantime. Treating a too-old cache entry as a miss
        forces _get_ltp()'s REST fallback to run, which then re-primes the
        cache via note_rest_ltp() - see its docstring for why that matters
        for rate-limit safety. A tick that's merely a little old (comfortably
        within LTP_STALE_AFTER_SECONDS) is still trusted as-is; this only
        catches genuinely stale/silent instruments."""
        if not config.ENABLE_WS_FEED:
            return None
        meta = self._instrument_meta(trading_symbol, expected_exchange=self._expected_exchange_for(trading_symbol))
        security_id = meta["security_id"]
        ltp = self._ltp_cache.get(security_id)
        if ltp is not None and config.LTP_STALE_AFTER_SECONDS > 0:
            last_update = self._ltp_cache_ts.get(security_id)
            age = (datetime.now(IST) - last_update).total_seconds() if last_update else None
            if age is not None and age > config.LTP_STALE_AFTER_SECONDS:
                self.stats["ltp_cache_stale"] += 1
                return None  # forces the caller's REST fallback
        if ltp is not None:
            self.stats["ltp_cache_hits"] += 1
        else:
            self.stats["ltp_cache_misses"] += 1
        return ltp

    def note_rest_ltp(self, trading_symbol: str, ltp: float) -> None:
        """Re-primes the WebSocket LTP cache with a REST-fetched price and a
        fresh timestamp, after get_cached_option_ltp() judged the previous
        tick too stale to trust (see its docstring). Without this, a
        silent/thinly-traded option would force a brand new REST call on
        *every* poll for as long as it stays quiet - once every
        MONITOR_INTERVAL_SECONDS (2s by default) - which risks Dhan's
        undocumented REST rate limit (see NOTES.md bug #5) once more than one
        position is stale at once. Re-priming means the next
        LTP_STALE_AFTER_SECONDS-sized window is served from this fresh
        value instead, so REST calls for a persistently-quiet option are
        naturally throttled to roughly once per LTP_STALE_AFTER_SECONDS,
        not once per poll."""
        try:
            meta = self._instrument_meta(trading_symbol, expected_exchange=self._expected_exchange_for(trading_symbol))
        except ValueError:
            return  # best-effort - a lookup failure here shouldn't break the exit check that called us
        self._ltp_cache[meta["security_id"]] = ltp
        self._ltp_cache_ts[meta["security_id"]] = datetime.now(IST)

    def _order_snapshot_from_cache(self, order_id: str) -> Optional[dict]:
        """Normalizes a socket order-update push to the same shape as
        _order_snapshot_from_rest(), or None if nothing has arrived yet.

        The order-update WebSocket payload's exact field names aren't
        documented (unlike the REST /orders/{id} schema) - dhanhq's own
        source only confirms "orderNo" and "status" exist on it. We check a
        few plausible REST-style key names too in case the push mirrors
        them, but treat REST as the authoritative source either way."""
        update = self._order_updates.get(str(order_id))
        if not update:
            return None
        status = update.get("orderStatus") or update.get("status") or ""
        return {
            "order_status": str(status).upper(),
            "remark": str(update.get("omsErrorDescription") or update.get("remark") or ""),
            "average_fill_price": update.get("averageTradedPrice") or update.get("avgPrice") or 0,
            "filled_quantity": update.get("filledQty") or update.get("filled_qty") or 0,
        }

    def _order_snapshot_from_rest(self, order_id: str) -> dict:
        resp = self.client.Dhan.get_order_by_id(order_id)
        if resp.get("status") != "success":
            return {"order_status": "", "remark": str(resp.get("remarks", "")),
                     "average_fill_price": 0, "filled_quantity": 0}
        data = resp.get("data")
        order = data[0] if isinstance(data, list) and data else (data or {})
        return {
            "order_status": order.get("orderStatus", ""),
            "remark": str(order.get("omsErrorDescription") or ""),
            "average_fill_price": order.get("averageTradedPrice") or 0,
            "filled_quantity": order.get("filledQty") or 0,
        }

    @staticmethod
    def _order_result_from_snapshot(order_id: str, snapshot: dict, is_amo: bool) -> OrderResult:
        return OrderResult(
            order_id=order_id,
            status=snapshot.get("order_status") or OrderStatus.TRANSIT,
            remark=snapshot.get("remark", ""),
            fill_price=float(snapshot.get("average_fill_price") or 0),
            filled_quantity=int(snapshot.get("filled_quantity") or 0),
            is_amo=is_amo,
        )

    # ------------------------------------------------------------------ #
    # ATM option selection
    # ------------------------------------------------------------------ #
    def get_atm_option(self, underlying_symbol: str, option_type: str) -> AtmOption:
        """Delegates ATM strike selection to Tradehull's own
        ATM_Strike_Selection (nearest expiry via Expiry=0), then looks up
        security_id/lot_size/expiry for the chosen leg from the instrument
        master. Retried (see _retry) - this is on the critical entry path
        and Dhan's market-data calls can transiently rate-limit-fail.

        Rolls forward to the NEXT listed expiry (Expiry=1) if the nearest
        one expires today - Dhan blocks new positions in a stock option on
        its own expiry day regardless of product type (confirmed live on
        25 Aug 2026, see NOTES.md bug #28: every stock-option order that
        day was RMS-rejected because it's a monthly-expiry Tuesday and
        stock options only have a monthly series). Rolling to next month's
        contract instead of just skipping keeps the strategy trading
        through what would otherwise be a dead day every month. Falls back
        to the nearest contract as-is if the roll itself still lands on
        today (e.g. only one expiry currently listed) - the caller's own
        expiry-day guard is the last line of defense in that edge case."""
        atm = _retry(self._get_atm_option_once, underlying_symbol, option_type, 0)
        if atm.expiry_date == datetime.now(IST).date():
            logger.info(
                "%s: nearest contract (%s) expires today - rolling to next month's expiry instead",
                underlying_symbol, atm.trading_symbol,
            )
            rolled = _retry(self._get_atm_option_once, underlying_symbol, option_type, 1)
            if rolled.expiry_date != atm.expiry_date:
                return rolled
            logger.warning(
                "%s: rolled-forward contract (%s) still expires today - no further expiry listed yet",
                underlying_symbol, rolled.trading_symbol,
            )
        return atm

    def _get_atm_option_once(self, underlying_symbol: str, option_type: str, expiry_index: int = 0) -> AtmOption:
        result = self.client.ATM_Strike_Selection(Underlying=underlying_symbol, Expiry=expiry_index)
        if not result:
            raise ValueError(f"Could not determine ATM strike for {underlying_symbol}")
        ce_symbol, pe_symbol, strike = result
        trading_symbol = ce_symbol if option_type == "CE" else pe_symbol
        if not trading_symbol:
            raise MissingOptionLegError(underlying_symbol, option_type, strike)

        expected_exchange = "MCX" if self._is_mcx_commodity(underlying_symbol) else "NSE"
        meta = self._instrument_meta(trading_symbol, expected_exchange=expected_exchange)
        return AtmOption(
            trading_symbol=trading_symbol,
            strike=float(strike),
            option_type=option_type,
            lot_size=meta["lot_size"],
            security_id=meta["security_id"],
            expiry_date=meta["expiry_date"],
        )

    def get_liquid_atm_option(self, underlying_symbol: str, option_type: str) -> Optional[AtmOption]:
        """Global common entry-point for every live-trading package's
        contract resolution (Options/Futures/Luxury/Swing's own entries) -
        added 18 Sep 2026 after a real incident: ATHERENERG 29 SEP 1540
        PUT's broker-side stop-loss was REJECTED with "EXCH:17181:
        Contract not traded. Market order not allowed" - the natural ATM
        strike had never printed a single trade before the position was
        already opened via AMO, so nothing could validate a protective
        order against it once the market opened, and the position then
        fell back to an unprotected poll loop that couldn't keep up with
        a fast opening move (see trading-skills' incident writeup for the
        full sequence).

        Resolves the natural ATM strike via get_atm_option() first, then
        requires it to pass TWO independent checks before using it:
          1. Current-session activity - the existing zero-volume-bars
             signal (refresh_liquidity_signal/get_cached_illiquid,
             unchanged, config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS).
          2. Prior-sessions' real trading activity (NEW) -
             get_daily_volume_sum over the last config.
             LIQUID_CONTRACT_LOOKBACK_DAYS calendar days must total at
             least config.LIQUID_CONTRACT_MIN_PRIOR_SESSION_VOLUME. This
             is the check that would have caught ATHERENERG: a contract
             with zero prior-session history is exactly the shape that
             can get a protective order rejected the moment it finally
             trades.

        If the ATM strike fails either check, walks outward to the
        nearest strikes on both sides (up to config.
        LIQUID_CONTRACT_MAX_STRIKE_SEARCH each way, closest first) for
        the SAME resolved expiry/option_type/underlying, returning the
        first candidate that passes both checks instead of just blocking
        the entry - a nearby, actively-traded substitute strike on the
        same underlying.

        Returns None if no candidate within the search window passes -
        callers MUST treat that as "skip this entry" and never fall back
        to the plain ATM pick themselves, or this gate is pointless.
        Disabled entirely via config.LIQUID_CONTRACT_GATE_ENABLED
        (returns the plain get_atm_option() result immediately when off,
        same as today's behavior).

        MCX commodities (Swing's Copper) run through the SAME two checks
        and the SAME nearby-strike search as NSE (extended 18 Sep 2026,
        same day, user follow-up request) - see _is_contract_liquid_and_
        active/_nearby_option_candidates for the real, empirically-
        confirmed MCX segment codes this uses (not guessed).

        Same "not authenticated" bypass as check_option_liquidity_sync
        (self._client is None) - a unit test that mocks get_atm_option
        directly, without ever authenticating, gets the plain ATM pick
        straight through with no attempt at a real liquidity/volume
        fetch, exactly like every existing test's own install_all_dhan_
        mocks() already expects.

        MCX commodities (Swing's Copper) - extended to this gate 18 Sep
        2026 (same day, user follow-up request) using the real,
        empirically-confirmed MCX segment codes (SEM_EXCH_INSTRUMENT_TYPE
        = "OPTFUT" for every real MCX option row, exchange_segment
        "MCX_COMM" - the same constant Swing's own order placement
        already uses), not guessed - the two checks and the nearby-strike
        search all work identically for MCX, just with MCX's own
        exchange/segment/instrument-type strings instead of NSE's.

        MissingOptionLegError handling (added 22 Sep 2026, real incident:
        COPPER CE at strike 1415 - Tradehull's ATM_Strike_Selection
        computed the strike but had no ce_symbol for it, so get_atm_option
        raised BEFORE any of the nearby-strike search below ever ran,
        defeating this whole function's purpose for exactly the failure
        shape it exists to route around). When that happens and the gate
        is enabled, this now seeds the SAME nearby-strike search from the
        strike Tradehull DID compute plus an expiry read directly from our
        own instrument master (_nearest_listed_expiry - independent of
        Tradehull's per-leg resolution, so it works even when Tradehull's
        own lookup for this specific leg came back empty). Gate-disabled
        callers keep the old behavior exactly (the exception still
        propagates - see the gate-check below, unchanged position)."""
        try:
            atm = self.get_atm_option(underlying_symbol, option_type)
        except MissingOptionLegError as exc:
            if not config.LIQUID_CONTRACT_GATE_ENABLED or self._client is None:
                raise  # gate disabled - preserve the old "let it raise" behavior exactly
            atm = None
            missing_leg_strike = exc.strike
        else:
            missing_leg_strike = None
        # _client check MUST come before _is_mcx_commodity - that call
        # touches self.instruments() -> self.client, which lazily
        # authenticates for real if _client is still None. Ordering this
        # any other way defeats the whole "not authenticated" bypass and
        # was a real regression caught by this session's own test run
        # (26 unrelated tests failing on a genuine Dhan login attempt).
        if not config.LIQUID_CONTRACT_GATE_ENABLED or self._client is None:
            return atm
        is_mcx = self._is_mcx_commodity(underlying_symbol)
        is_index = (not is_mcx) and self._is_index_underlying(underlying_symbol)

        if atm is not None:
            reference = atm
        else:
            expiry_date = self._nearest_listed_expiry(underlying_symbol, option_type, is_mcx)
            if expiry_date is None:
                logger.warning(
                    "%s: no listed %s expiry found at all (ATM strike %s had no valid leg either) - skipping entry",
                    underlying_symbol, option_type, missing_leg_strike,
                )
                return None
            # Placeholder seed for _nearby_option_candidates' distance-sort
            # only - trading_symbol/security_id/lot_size are never trusted
            # or returned; a real instrument-master row always replaces
            # this before anything reaches the caller (filtered below).
            reference = AtmOption(trading_symbol="", strike=missing_leg_strike, option_type=option_type,
                                   lot_size=0, security_id="", expiry_date=expiry_date)

        candidates = [
            c for c in self._nearby_option_candidates(
                underlying_symbol, option_type, reference, config.LIQUID_CONTRACT_MAX_STRIKE_SEARCH, is_mcx,
            )
            if c.trading_symbol   # drop the placeholder itself if it ever comes back as its own "candidate"
        ]
        for candidate in candidates:
            if self._is_contract_liquid_and_active(candidate, is_mcx, is_index):
                if atm is None or candidate.trading_symbol != atm.trading_symbol:
                    logger.info(
                        "%s: ATM strike %s %s %s - substituted nearby strike %s %s instead",
                        underlying_symbol, reference.strike, option_type,
                        "had no valid contract" if atm is None else "was illiquid/untraded",
                        candidate.strike, option_type,
                    )
                return candidate
        logger.warning(
            "%s: no liquid, actively-traded %s contract found within %d strikes of %.2f - skipping entry",
            underlying_symbol, option_type, config.LIQUID_CONTRACT_MAX_STRIKE_SEARCH, reference.strike,
        )
        return None

    def _is_contract_liquid_and_active(self, candidate: "AtmOption", is_mcx: bool = False, is_index: bool = False) -> bool:
        """The two checks get_liquid_atm_option requires of every
        candidate - see that function's own docstring for the full
        rationale of each. is_index (added 24 Sep 2026, real bug fix -
        see _is_index_underlying's own docstring) picks NSE_FNO/OPTIDX
        instead of NSE_FNO/OPTSTK for both checks below - the wrong
        instrument_type string was making even NIFTY/BANKNIFTY's own ATM
        strike look illiquid to get_daily_volume_sum's real Dhan query."""
        if is_mcx:
            self.refresh_liquidity_signal(
                candidate.trading_symbol, expected_exchange="MCX",
                exchange_segment="MCX_COMM", instrument_type="OPTFUT",
            )
        elif is_index:
            self.refresh_liquidity_signal(candidate.trading_symbol, instrument_type="OPTIDX")
        else:
            self.refresh_liquidity_signal(candidate.trading_symbol)
        if self.get_cached_illiquid(candidate.trading_symbol):
            return False
        if is_mcx:
            exchange_segment, instrument_type = "MCX_COMM", "OPTFUT"
        elif is_index:
            exchange_segment, instrument_type = "NSE_FNO", "OPTIDX"
        else:
            exchange_segment, instrument_type = "NSE_FNO", "OPTSTK"
        volume_sum = self.get_daily_volume_sum(
            candidate.security_id, exchange_segment, instrument_type, config.LIQUID_CONTRACT_LOOKBACK_DAYS,
        )
        if volume_sum is None or volume_sum < config.LIQUID_CONTRACT_MIN_PRIOR_SESSION_VOLUME:
            return False
        return True

    def _nearest_listed_expiry(self, underlying_symbol: str, option_type: str, is_mcx: bool) -> Optional[date]:
        """Nearest (>= today) listed expiry for underlying_symbol/
        option_type, read directly from the instrument master - added 22
        Sep 2026 for get_liquid_atm_option's MissingOptionLegError
        fallback (see that function's own docstring). Deliberately
        independent of Tradehull's own ATM_Strike_Selection, which is what
        failed to resolve a specific leg in the first place - the expiry
        itself is still perfectly discoverable from our own data even
        when Tradehull's per-leg lookup came back empty. Same filter
        columns/logic as _nearby_option_candidates below, just without an
        already-known expiry_date to filter by (that's the whole point -
        this is how it gets discovered).

        Two-stage filter (25 Sep 2026 perf fix, see trading-skills'
        memory-audit findings): the full instrument master is ~206k rows /
        ~125MB in memory (measured live) - copying and date-parsing the
        WHOLE thing before filtering down to one underlying's handful of
        rows was a real, avoidable multi-hundred-KB-to-125MB allocation on
        every call, on a ~960MB droplet. Filters on the cheap, already-
        typed columns (exchange/symbol-prefix/option-type - none need
        parsing) FIRST to shrink to a tiny subset, copies THAT, then only
        date-parses the subset. AND is order-independent, so the final
        row set and values are byte-identical to before - this only
        changes how much gets copied/parsed to reach them."""
        exchange = "MCX" if is_mcx else "NSE"
        full = self.instruments()
        prefilter = (
            (full["SEM_EXM_EXCH_ID"] == exchange)
            & (full["SEM_CUSTOM_SYMBOL"].str.startswith(f"{underlying_symbol.upper()} "))
            & (full["SEM_OPTION_TYPE"] == option_type)
        )
        if is_mcx and "SM_SYMBOL_NAME" in full.columns:
            prefilter = prefilter & (full["SM_SYMBOL_NAME"] == underlying_symbol.upper())
        df = full[prefilter].copy()
        if df.empty:
            return None
        df["ContractExpiration"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce").dt.date
        today = datetime.now(IST).date()
        rows = df[df["ContractExpiration"] >= today]
        if rows.empty:
            return None
        return rows["ContractExpiration"].min()

    def _nearby_option_candidates(
        self, underlying_symbol: str, option_type: str, atm: "AtmOption", max_search: int, is_mcx: bool = False,
    ) -> list:
        """Every other strike, same underlying/expiry/option_type, sorted
        by distance from the ATM strike - the ATM itself always comes
        first (distance 0). Queries the instrument master directly rather
        than Dhan's option-chain endpoint: that endpoint's response has no
        trading_symbol/security_id field at all (strike+type only), so a
        second instrument-master lookup per strike would be needed either
        way - this skips the extra REST call entirely.

        SEM_CUSTOM_SYMBOL.startswith(f"{underlying_symbol} ") (WITH the
        trailing space) rather than Tradehull's own bare .startswith
        (see ATM_Strike_Selection in the vendored Dhan_Tradehull package)
        - CUSTOM_SYMBOL is always "SYMBOL DD MON STRIKE TYPE"
        space-separated (confirmed for MCX too - "COPPER 23 SEP 1400
        CALL" - the same format as an NSE stock option), so the trailing
        space avoids a prefix collision (e.g. "TATA" matching
        "TATAPOWER") that Tradehull's own matching doesn't bother to
        guard against.

        For MCX, ALSO requires SM_SYMBOL_NAME == underlying_symbol -
        Tradehull's own ATM_Strike_Selection adds this exact extra
        condition only for its MCX branch (commodity naming needs the
        disambiguation the NSE stock/index branches don't), so this
        mirrors that rather than inventing a new filter.

        Two-stage filter (25 Sep 2026 perf fix) - same reasoning as
        _nearest_listed_expiry above: filter on the cheap columns first to
        shrink from ~206k rows to one underlying's handful before copying
        or date-parsing anything. Byte-identical result, far less copied."""
        exchange = "MCX" if is_mcx else "NSE"
        full = self.instruments()
        prefilter = (
            (full["SEM_EXM_EXCH_ID"] == exchange)
            & (full["SEM_CUSTOM_SYMBOL"].str.startswith(f"{underlying_symbol.upper()} "))
            & (full["SEM_OPTION_TYPE"] == option_type)
        )
        if is_mcx and "SM_SYMBOL_NAME" in full.columns:
            prefilter = prefilter & (full["SM_SYMBOL_NAME"] == underlying_symbol.upper())
        df = full[prefilter].copy()
        if df.empty:
            return [atm]
        df["ContractExpiration"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce").dt.date
        rows = df[df["ContractExpiration"] == atm.expiry_date].copy()
        if rows.empty:
            return [atm]
        rows["SEM_STRIKE_PRICE"] = rows["SEM_STRIKE_PRICE"].astype(float)
        rows = rows.drop_duplicates(subset=["SEM_STRIKE_PRICE"])
        rows["_dist"] = (rows["SEM_STRIKE_PRICE"] - atm.strike).abs()
        rows = rows.sort_values("_dist").head(max_search * 2 + 1)
        candidates = [
            AtmOption(
                trading_symbol=str(r["SEM_CUSTOM_SYMBOL"]), strike=float(r["SEM_STRIKE_PRICE"]),
                option_type=option_type, lot_size=int(float(r["SEM_LOT_UNITS"])),
                security_id=str(int(r["SEM_SMST_SECURITY_ID"])), expiry_date=atm.expiry_date,
            )
            for _, r in rows.iterrows()
        ]
        return candidates or [atm]

    def get_daily_volume_sum(
        self, security_id: str, exchange_segment: str, instrument_type: str, lookback_days: int = 7,
    ) -> Optional[float]:
        """Sums an option contract's own daily traded volume over the last
        `lookback_days` CALENDAR days (wide enough to cover a weekend/
        holiday and still catch a couple of real trading sessions) - a
        genuinely separate Dhan endpoint (/charts/historical, daily bars)
        from the 1-min/5-min intraday series refresh_liquidity_signal/
        refresh_supertrend_signal use. Built for get_liquid_atm_option's
        own prior-session-activity check.

        Returns None (not an exception, not zero) on any fetch failure -
        a genuinely zero-volume contract and "couldn't check" must never
        be conflated: the caller excludes a confirmed zero from this
        specific candidate and moves to the next one either way, but they
        mean very different things if this ever needs debugging."""
        try:
            to_date = datetime.now(IST).strftime("%Y-%m-%d")
            from_date = (datetime.now(IST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            resp = _retry(
                self.client.Dhan.historical_daily_data, security_id=security_id,
                exchange_segment=exchange_segment, instrument_type=instrument_type,
                from_date=from_date, to_date=to_date,
            )
            data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
            volumes = data.get("volume") or []
            return float(sum(volumes)) if volumes else 0.0
        except Exception:  # noqa: BLE001
            logger.exception("Could not fetch daily volume history for security_id %s", security_id)
            return None

    def get_futures_contract(self, underlying_symbol: str) -> FuturesContract:
        """Finds the nearest-expiry FUTSTK (stock futures) contract for
        underlying_symbol - genuinely new capability, added 31 Aug 2026 for
        the Swing package (the first strategy in this codebase to trade a
        real futures contract rather than buying an ATM option as a
        placeholder for one). No Tradehull helper exists for this the way
        ATM_Strike_Selection exists for options, so this reads the
        instrument master directly, mirroring K01's _fetch_fno_universe /
        this file's own get_atm_option for the filtering/rolling pattern.

        Retried (see _retry) - same rationale as get_atm_option: Dhan's
        market-data-adjacent calls can transiently rate-limit-fail. Rolls
        forward to the next listed expiry if the nearest one expires today
        - same rationale as get_atm_option's identical guard (Dhan blocks
        new positions in a contract on its own expiry day)."""
        contract = _retry(self._get_futures_contract_once, underlying_symbol, 0)
        if contract.expiry_date == datetime.now(IST).date():
            logger.info(
                "%s: nearest futures contract (%s) expires today - rolling to next month's expiry instead",
                underlying_symbol, contract.trading_symbol,
            )
            rolled = _retry(self._get_futures_contract_once, underlying_symbol, 1)
            if rolled.expiry_date != contract.expiry_date:
                return rolled
            logger.warning(
                "%s: rolled-forward futures contract (%s) still expires today - no further expiry listed yet",
                underlying_symbol, rolled.trading_symbol,
            )
        return contract

    def _get_futures_contract_once(self, underlying_symbol: str, expiry_index: int = 0) -> FuturesContract:
        df = self.instruments()
        futs = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "FUTSTK")]
        # Match by the underlying DERIVED from each row's own trading
        # symbol, not a naive prefix match - a prefix match on "RELIANCE"
        # would also wrongly match "RELIANCEPOWER"'s own futures contract.
        matches = futs[futs["SEM_TRADING_SYMBOL"].apply(
            lambda s: self._underlying_from_trading_symbol(str(s)) == underlying_symbol
        )]
        if matches.empty:
            raise ValueError(f"No futures contract found for {underlying_symbol}")
        matches = matches.sort_values("SEM_EXPIRY_DATE")
        if expiry_index >= len(matches):
            expiry_index = len(matches) - 1
        row = matches.iloc[expiry_index]
        return FuturesContract(
            trading_symbol=str(row["SEM_CUSTOM_SYMBOL"]),
            security_id=str(int(row["SEM_SMST_SECURITY_ID"])),
            lot_size=int(float(row["SEM_LOT_UNITS"])),
            expiry_date=pd.to_datetime(row["SEM_EXPIRY_DATE"], errors="coerce").date(),
        )

    def get_mcx_futures_contract(self, underlying_symbol: str) -> FuturesContract:
        """MCX commodity counterpart to get_futures_contract (added 12 Sep
        2026, Swing v2's Copper support) - a SIBLING function, not a
        parameterization of the NSE one, so the existing NSE path stays
        byte-identical (same convention as place_equity_market_order vs
        place_market_order elsewhere in this file). Resolves the FUTCOM
        contract for underlying_symbol, rolling forward the same way
        get_futures_contract does if the nearest one expires today or has
        fewer than config.MCX_MIN_DAYS_TO_EXPIRY days left - ported from
        the now-deleted CopperOptions/paper_engine.py's _resolve_expiry_
        cycle (that package never placed a real order, so this reuses its
        proven READ-ONLY resolution logic only).

        Used as the regime/Supertrend signal reference for an MCX symbol
        (there's no continuous "spot" for a commodity, only its futures
        contract) - see Swing/signals.py. NOT used to open a real futures
        position: Swing only trades Copper via BASKET_TYPE=="options" for
        now (explicit user scope restriction, 12 Sep 2026) - see
        Swing/trading_engine.py's enter_position_for_stock."""
        contract = _retry(self._get_mcx_futures_contract_once, underlying_symbol, 0)
        if contract.expiry_date == datetime.now(IST).date():
            logger.info(
                "%s: nearest MCX futures contract (%s) expires today - rolling to next cycle instead",
                underlying_symbol, contract.trading_symbol,
            )
            rolled = _retry(self._get_mcx_futures_contract_once, underlying_symbol, 1)
            if rolled.expiry_date != contract.expiry_date:
                return rolled
            logger.warning(
                "%s: rolled-forward MCX futures contract (%s) still expires today - no further cycle listed yet",
                underlying_symbol, rolled.trading_symbol,
            )
        return contract

    def _get_mcx_futures_contract_once(self, underlying_symbol: str, expiry_index: int = 0) -> FuturesContract:
        from Swing import config as swing_config  # local import - avoids a
        # module-level dependency from this shared file onto a specific
        # strategy package, same reasoning as any other cross-package
        # config read in this codebase being kept local to the function
        # that actually needs it.
        df = self.instruments()
        futs = df[
            (df["SEM_EXM_EXCH_ID"] == "MCX")
            & (df["SEM_INSTRUMENT_NAME"] == "FUTCOM")
            & (df["SEM_TRADING_SYMBOL"].str.startswith(underlying_symbol + "-"))
        ]
        if futs.empty:
            raise ValueError(f"No MCX futures contract found for {underlying_symbol}")
        futs = futs.sort_values("SEM_EXPIRY_DATE")
        min_days = getattr(swing_config, "MCX_MIN_DAYS_TO_EXPIRY", 3)
        today = datetime.now(IST).date()
        eligible = futs[futs["SEM_EXPIRY_DATE"].apply(
            lambda d: (pd.Timestamp(d).date() - today).days >= min_days
        )]
        candidates = eligible if not eligible.empty else futs
        if expiry_index >= len(candidates):
            expiry_index = len(candidates) - 1
        row = candidates.iloc[expiry_index]
        return FuturesContract(
            trading_symbol=str(row["SEM_CUSTOM_SYMBOL"]),
            security_id=str(int(row["SEM_SMST_SECURITY_ID"])),
            lot_size=int(float(row["SEM_LOT_UNITS"])),
            expiry_date=pd.to_datetime(row["SEM_EXPIRY_DATE"], errors="coerce").date(),
        )

    # ------------------------------------------------------------------ #
    # Live data
    # ------------------------------------------------------------------ #
    def get_day_change_pct(self, symbol: str) -> float:
        """% change on the day for a cash-segment equity/index symbol.
        Assumes Dhan's documented OHLC response shape: {"last_price": ...,
        "ohlc": {"open":..., "high":..., "low":..., "close":...}}.
        Retried (see _retry) - rank_and_pick_top_stocks() calls this once
        per stock in the alert back-to-back, which is exactly the kind of
        rapid-fire pattern that trips Dhan's rate limit."""
        return _retry(self._get_day_change_pct_once, symbol)

    def _get_day_change_pct_once(self, symbol: str) -> float:
        data = self.client.get_ohlc_data(names=[symbol])
        values = data.get(symbol)
        if not values:
            raise ValueError(f"No OHLC data returned for {symbol}")
        prev_close = float(values.get("ohlc", {}).get("close") or 0)
        if not prev_close:
            raise ValueError(f"No previous close returned for {symbol}")
        ltp = float(values.get("last_price") or 0)
        if not ltp:
            ltp_data = self.client.get_ltp_data(names=[symbol])
            ltp = float(ltp_data.get(symbol) or 0)
        return (ltp - prev_close) / prev_close * 100

    def get_today_open_and_prev_close(self, symbol: str) -> tuple[float, float]:
        """Today's opening price and the previous session's closing price
        for a cash-segment equity/index symbol - added 31 Aug 2026 for
        Swing's "today's open > yesterday's close" entry gate. Reuses the
        exact same OHLC quote get_day_change_pct() already fetches
        (Dhan's own response bundles both today's `ohlc.open` and the
        prior close as `ohlc.close` in one call) rather than a second,
        redundant REST call - just returns the two raw values instead of
        deriving a % from them. Retried (see _retry) - same rationale as
        every other Dhan market-data call here."""
        return _retry(self._get_today_open_and_prev_close_once, symbol)

    def _get_today_open_and_prev_close_once(self, symbol: str) -> tuple[float, float]:
        data = self.client.get_ohlc_data(names=[symbol])
        values = data.get(symbol)
        if not values:
            raise ValueError(f"No OHLC data returned for {symbol}")
        ohlc = values.get("ohlc") or {}
        today_open = float(ohlc.get("open") or 0)
        prev_close = float(ohlc.get("close") or 0)
        if not today_open or not prev_close:
            raise ValueError(f"Missing open/prev_close in OHLC data for {symbol}: {values}")
        return today_open, prev_close

    def get_option_ltp(self, trading_symbol: str) -> float:
        """REST LTP fallback, used by _get_ltp() whenever the WebSocket
        cache is stale/missing (see that function's own docstring) - on
        the exit-monitoring critical path, called once per position per
        monitor tick when the cache misses. Retried (see _retry) - found
        + fixed 31 Aug 2026 (user request, a lag audit of a live trading
        day found 46 unretried "Could not fetch LTP" failures spread
        across nearly every held position that day, each one silently
        skipping that position's exit-check for a single ~2s tick before
        self-healing on the next one). This call had no retry wrapper at
        all before this fix, unlike get_atm_option/get_day_change_pct/
        get_open_fno_positions, which already had one for the identical
        reason ("Dhan's market-data calls can transiently rate-limit-
        fail" - see _retry's own docstring)."""
        return _retry(self._get_option_ltp_once, trading_symbol)

    def _get_option_ltp_once(self, trading_symbol: str) -> float:
        data = self.client.get_ltp_data(names=[trading_symbol])
        ltp = data.get(trading_symbol)
        if ltp is None:
            raise ValueError(f"No LTP returned for {trading_symbol}")
        return float(ltp)

    def get_margin_required(
        self, security_id: str, exchange_segment: str, transaction_type: str,
        quantity: int, product_type: str, price: float,
    ) -> dict:
        """Public, retried wrapper around Dhan's real, read-only
        `/margincalculator` endpoint - added 1 Sep 2026 for Swing's paper
        trading (user request: "make sure we would also be logging real
        margin and funds required during paper trading so that we can do
        analysis also"). Places NO order - answers "what would THIS ONE
        order, by itself, cost in margin right now."

        Uses the RAW dhanhq call (`self.client.Dhan.margin_calculator`),
        not Tradehull's own `margin_calculator()` wrapper - that wrapper
        collapses any failure (bad symbol, transient error, anything) down
        to a silent `return 0`, which would be indistinguishable from a
        genuine ₹0 margin figure. This raises instead, so a fetch failure
        is always visible as "no data" rather than a misleadingly precise
        zero.

        See the separate trading-skills repo's own
        `basket-order-feasibility.md`/`mtf-eligibility-detection.md` for
        the full investigation this is built on - confirmed there that
        this endpoint has ZERO combo-awareness (it can't tell you what a
        futures+PE hedge would cost TOGETHER, only each leg standalone),
        and that `status: "success"` alone doesn't guarantee a meaningful
        result (the MTF-on-a-BE-series-stock gotcha) - callers here only
        ever read `totalMargin`, which is unaffected by that specific
        gotcha (it only affects the `leverage` field on MTF requests)."""
        return _retry(
            self._get_margin_required_once, security_id, exchange_segment,
            transaction_type, quantity, product_type, price,
        )

    def _get_margin_required_once(
        self, security_id: str, exchange_segment: str, transaction_type: str,
        quantity: int, product_type: str, price: float,
    ) -> dict:
        response = self.client.Dhan.margin_calculator(
            security_id=str(security_id), exchange_segment=exchange_segment,
            transaction_type=transaction_type, quantity=int(quantity),
            product_type=product_type, price=float(price),
        )
        if response.get("status") != "success":
            raise ValueError(f"margin_calculator failed for security_id={security_id}: {response}")
        return response.get("data") or {}

    def get_fund_limits(self) -> dict:
        """Public, retried wrapper around Dhan's real `/fundlimit`
        endpoint - the account's current balance/margin-utilization
        snapshot, added 1 Sep 2026 alongside get_margin_required() so a
        paper trade's logged margin requirement can be checked against
        what funds were actually available at that same moment, not just
        the requirement viewed in isolation. Returns the full `data` dict
        as-is (field names/casing exactly as Dhan sends them, including
        its own `availabelBalance` typo) rather than picking out specific
        keys - this is for offline analysis, not a decision this bot
        makes anything on, so nothing is lost by keeping the whole
        breakdown."""
        return _retry(self._get_fund_limits_once)

    def _get_fund_limits_once(self) -> dict:
        response = self.client.Dhan.get_fund_limits()
        if response.get("status") == "failure":
            raise ValueError(f"get_fund_limits failed: {response}")
        return response.get("data") or {}

    # ------------------------------------------------------------------ #
    # Continuous intraday candle fetch - the single place every intraday
    # indicator in the codebase gets its bars from (added 10 Sep 2026).
    # ------------------------------------------------------------------ #
    def fetch_continuous_intraday(
        self, security_id: str, exchange_segment: str, instrument_type: str, interval_minutes: int,
        lookback_days_override: Optional[int] = None,
    ) -> dict:
        """Intraday candles spanning the last
        config.INTRADAY_CONTINUOUS_LOOKBACK_DAYS calendar days THROUGH today -
        one continuous multi-session series, not today-only. Every recursive
        indicator computed on it (Supertrend / EMA / RSI / ATR) is therefore
        fully warm from the very first bar of today's session, with no
        daily-reset warm-up lag - the way a charting platform's intraday
        indicators run. Dhan's intraday_minute_data returns a continuous
        series across sessions (verified: a 7-day 1-min request spans ~5
        trading days with no synthetic overnight bars).

        `lookback_days_override` (added 12 Sep 2026, Swing v2's 200-period
        EMA regime signal) - the global INTRADAY_CONTINUOUS_LOOKBACK_DAYS
        (default 7) is nowhere near enough for a 200-period EMA on 15-min
        bars: 7 days is only ~125 fifteen-minute bars, below the 200 needed
        for _compute_ema to even return a first value. Rather than widen
        the shared global (which would silently change every OTHER live
        signal - Supertrend/EMA-cross/RSI - for Options/Futures/Luxury too),
        this optional per-call override lets one caller ask for a longer
        window without touching anyone else's. Confirmed via a live spike
        (12 Sep 2026) that Dhan serves a 45-day/15-min request in one call
        (795 bars returned, no chunking needed) - if a future caller needs
        a window Dhan won't serve in one request, this is the parameter to
        extend with real chunking, not a reason to raise the global.

        Returns the raw resp["data"] dict (open/high/low/close/volume/
        timestamp lists), or {} on failure - callers apply their own
        still-forming-last-candle drop and minimum-length checks. Wrapped in
        _retry for Dhan's intermittent rate-limit failures on back-to-back
        market-data calls, and in _throttle_market_data_call for the
        account-wide DH-904 pacing/backoff shared by every package (see
        config.MARKET_DATA_MIN_INTERVAL_SECONDS's own docstring). A skipped
        call (still-active shared cooldown) returns {} exactly like any
        other failure - deliberately NOT distinguished from a genuine empty
        response, since every caller already treats {} as "fall back to
        cache," which is exactly the right behavior here too."""
        days = lookback_days_override or config.INTRADAY_CONTINUOUS_LOOKBACK_DAYS
        from_date = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
        to_date = datetime.now(IST).strftime("%Y-%m-%d")
        if not self._throttle_market_data_call():
            logger.info(
                "fetch_continuous_intraday(security_id=%s, segment=%s, interval=%s) skipped - "
                "shared account-wide DH-904 cooldown still active (%.1fs remaining)",
                security_id, exchange_segment, interval_minutes,
                self._market_data_cooldown_until - time.monotonic(),
            )
            return {}
        resp = _retry(
            self.client.Dhan.intraday_minute_data,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
            interval=interval_minutes,
        )
        self._note_market_data_rate_limit_outcome(resp)
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        if not data.get("close"):
            # Diagnostic only (added 22 Sep 2026) - callers already treat an
            # empty result as a fetch failure and fall back to cache; this
            # just captures WHAT Dhan actually said instead of only "empty",
            # since the generic {} return here is indistinguishable between
            # a rate limit, a genuinely out-of-range request (e.g. a request
            # predating an MCX contract's own listing date), and anything
            # else - see the still-open question in
            # incidents/2026-09-22-swing-signal-cache-never-throttled-on-failure.md.
            logger.warning(
                "fetch_continuous_intraday(security_id=%s, segment=%s, instrument=%s, "
                "interval=%s, from=%s, to=%s) returned no close data - raw response: %s",
                security_id, exchange_segment, instrument_type, interval_minutes,
                from_date, to_date, resp,
            )
        return data

    def _throttle_market_data_call(self) -> bool:
        """Returns False (caller must skip the call entirely, no REST
        attempt) if the shared account-wide DH-904 cooldown is still
        active - a FAIL-FAST skip, never a sleep-and-wait for it. This is
        the one deliberate asymmetry in the whole design (24 Sep 2026,
        real finding): this method is reached from inside Swing/Options/
        Futures/Luxury's own per-position exit-check poll loop, which
        checks every open position SEQUENTIALLY in one tick - blocking
        here for the cooldown's full duration (up to
        config.MARKET_DATA_RATE_LIMIT_COOLDOWN_MAX_SECONDS) would delay
        every position checked AFTER this one in the same tick, including
        a completely unrelated position's PRIMARY LTP-based MAX_LOSS/
        TARGET/STOP_LOSS exit check for any symbol with no WebSocket fast
        path (equity-only positions - see Swing/trading_engine.py's
        on_price_tick docstring). That would reintroduce real risk to
        exactly the safety net this codebase already deliberately made
        fail-open (see trading-skills' 2026-09-22-swing-signal-cache-
        never-throttled-on-failure.md: "no incorrect order was placed...
        the real impact is staleness" - a design choice, not an oversight).
        Skipping preserves that: a cooldown-covered symbol just falls back
        to its last cached signal exactly as it always has, no new delay.

        The steady PACING floor (config.MARKET_DATA_MIN_INTERVAL_SECONDS,
        0.5s default) is the one part that still blocks - small and
        bounded enough (versus a 5s+ poll tick) to accept, and it's what
        actually prevents a same-tick burst from ever reaching Dhan's
        limit in the first place; the cooldown only matters once Dhan has
        already said no, at which point waiting it out is this account's
        job collectively, not any single caller's job to block on."""
        with self._market_data_lock:
            now = time.monotonic()
            if now < self._market_data_cooldown_until:
                return False
            wait = self._market_data_next_allowed_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._market_data_next_allowed_at = now + config.MARKET_DATA_MIN_INTERVAL_SECONDS
            return True

    def _note_market_data_rate_limit_outcome(self, resp: object) -> None:
        """Arms/clears the shared backoff cooldown based on what Dhan
        actually said - detected from the response envelope itself (Dhan
        returns DH-904 as a normal `{"status": "failure", ...}` payload,
        never a raised exception - same detection idiom as shadow_
        evaluator.py's own throttle). A hit doubles the cooldown from
        config.MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS, capped at
        MARKET_DATA_RATE_LIMIT_COOLDOWN_MAX_SECONDS, and pushes every OTHER
        package's next fetch_continuous_intraday call out to match - not
        just this caller's own retry. A clean (non-rate-limited) response,
        including a genuine {} for an unrelated reason, resets the streak
        so the NEXT hit starts back at the base cooldown rather than
        wherever a stale streak left off."""
        remarks = str(resp.get("remarks")) if isinstance(resp, dict) else ""
        if not ("DH-904" in remarks or "Rate_Limit" in remarks or "Too many" in remarks):
            # Same lock as the increment below - audit finding, CODE_AUDIT_
            # 2026-09-25.md (round 2): this reset used to run unlocked
            # while called concurrently from executor threads across all
            # four packages, racing the increment's own read-modify-write
            # and potentially corrupting the shared streak count (too
            # short or stale-doubled cooldown).
            with self._market_data_lock:
                self._market_data_consecutive_rate_limit_hits = 0
            return
        with self._market_data_lock:
            self._market_data_consecutive_rate_limit_hits += 1
            cooldown_seconds = min(
                config.MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS * (2 ** (self._market_data_consecutive_rate_limit_hits - 1)),
                config.MARKET_DATA_RATE_LIMIT_COOLDOWN_MAX_SECONDS,
            )
            self._market_data_cooldown_until = time.monotonic() + cooldown_seconds
        logger.warning(
            "fetch_continuous_intraday hit DH-904 (consecutive hit #%s) - "
            "backing off the shared account-wide call budget for %.1fs",
            self._market_data_consecutive_rate_limit_hits, cooldown_seconds,
        )

    # ------------------------------------------------------------------ #
    # Supertrend exit signal (computed on the underlying stock, not the
    # option's own premium - see config.ENABLE_SUPERTREND_EXIT)
    # ------------------------------------------------------------------ #
    def refresh_supertrend_signal(self, underlying_symbol: str) -> None:
        """Fetches the underlying's 5-min candles and recomputes whether its
        last fully-closed candle's close is below the 5-min Supertrend - a
        trend-reversal exit signal. Cached (see get_cached_supertrend_bearish)
        and only re-fetched every config.SUPERTREND_REFRESH_SECONDS.

        Candles come from fetch_continuous_intraday - a continuous
        multi-session series - so the Supertrend line/bands are fully warm
        from the first bar of today's session, no early-morning dead window
        or unreliable "reads bearish on everything" warm-up period (which is
        what a today-only fetch used to cause until ~10:10 IST).

        Blocking (REST call) - call via run_in_executor from async code, and
        only from the poll loop, not from the WebSocket tick path (which
        must stay non-blocking); the poll loop already runs every few
        seconds, which keeps the cache fresh enough for the tick path to
        read synchronously without its own network call."""
        cached = self._supertrend_cache.get(underlying_symbol)
        if cached and (datetime.now(IST) - cached[0]).total_seconds() < config.SUPERTREND_REFRESH_SECONDS:
            return
        try:
            security_id = self._equity_security_id(underlying_symbol)
            data = self.fetch_continuous_intraday(
                security_id, "NSE_EQ", "EQUITY", config.SUPERTREND_INTERVAL_MINUTES,
            )
            highs = data.get("high") or []
            lows = data.get("low") or []
            closes = data.get("close") or []
            timestamps = data.get("timestamp") or []
            period = config.SUPERTREND_PERIOD
            if len(closes) < period + 1:
                logger.info("Not enough %d-min candles yet for %s Supertrend (%d bars)",
                            config.SUPERTREND_INTERVAL_MINUTES, underlying_symbol, len(closes))
                return

            # Drop the current, still-forming candle if Dhan included one -
            # only a fully-closed candle's close should drive the signal
            # ("5 min close crossed below 5 min supertrend", not a
            # mid-candle wick). A candle starting at timestamps[-1] is
            # closed once interval minutes have actually elapsed since then.
            # timestamps is trimmed in lockstep so its last entry always
            # matches the candle that actually produced closes[-1] - callers
            # (see get_cached_supertrend_candle_start) rely on that to tell
            # whether a position's own entry candle is the one being read.
            if timestamps:
                last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
                if datetime.now(IST) < last_candle_start + timedelta(minutes=config.SUPERTREND_INTERVAL_MINUTES):
                    highs, lows, closes, timestamps = highs[:-1], lows[:-1], closes[:-1], timestamps[:-1]
            if len(closes) < period + 1:
                return

            # The extra minimum-candles warmup gate this used to have (NOTES.md
            # bug #10/#16's fix - SUPERTREND_MIN_WARMUP_CANDLES) was removed
            # entirely by user request 27 Aug 2026: the first computable value
            # (right at period+1 candles) has no prior trend/band history to
            # seed from, so it can read "bearish" on ~every underlying
            # regardless of actual trend - a known, accepted tradeoff now,
            # not gated against. See NOTES.md's design-decision entry for the
            # full history of what this gate used to be tuned to.
            supertrend = _compute_supertrend(highs, lows, closes, period=period,
                                              multiplier=config.SUPERTREND_MULTIPLIER)
            last_st = supertrend[-1]
            if last_st is None:
                return
            is_bearish = closes[-1] < last_st
            candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None
            self._supertrend_cache[underlying_symbol] = (datetime.now(IST), is_bearish, candle_start, closes[-1])
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh Supertrend signal for %s", underlying_symbol)

    def get_cached_supertrend_bearish(self, underlying_symbol: str) -> Optional[bool]:
        """Synchronous, cache-only read - safe to call from the WebSocket
        tick path without blocking the event loop. None means no signal has
        been computed yet (e.g. right after startup, before the poll loop's
        first refresh) - callers should treat that as "no exit signal", not
        force an exit on missing data."""
        cached = self._supertrend_cache.get(underlying_symbol)
        return cached[1] if cached else None

    def get_cached_supertrend_candle_start(self, underlying_symbol: str) -> Optional[datetime]:
        """Start timestamp (IST) of the fully-closed candle the cached
        signal is based on. Used to skip a Supertrend exit while it's still
        reading the same candle a position was entered on (see
        Position.supertrend_entry_candle_start) - a same-candle read means
        the signal hasn't had a chance to confirm anything since entry, it's
        just re-describing the same breakout candle that triggered the
        entry in the first place."""
        cached = self._supertrend_cache.get(underlying_symbol)
        return cached[2] if cached else None

    def get_cached_supertrend_underlying_close(self, underlying_symbol: str) -> Optional[float]:
        """The underlying's own last-closed-candle price the cached
        Supertrend signal was computed from (added 18 Sep 2026 for the
        minimum-underlying-move confirmation gate - see
        reversal_filters.check_underlying_move_confirms_exit). Synchronous,
        cache-only - safe from the WebSocket tick path."""
        cached = self._supertrend_cache.get(underlying_symbol)
        return cached[3] if cached else None

    # ------------------------------------------------------------------ #
    # EMA-cross exit signal (added 10 Sep 2026 for Futures - see
    # config.ENABLE_EMA_CROSS_EXIT). Computed on the UNDERLYING's 5-min
    # closes: EMA(EMA_CROSS_FAST_PERIOD) vs EMA(EMA_CROSS_SLOW_PERIOD).
    # "crossed_this_candle" is True when the sign of (fast - slow) flipped
    # between the last two fully-closed candles - that's the "crossed
    # below/above" edge the exit acts on, not a plain "fast is under slow"
    # state. Candles come from fetch_continuous_intraday (a continuous
    # multi-session series), so both EMAs are fully warm from the first bar
    # of the day.
    # ------------------------------------------------------------------ #
    def refresh_ema_cross_signal(self, underlying_symbol: str) -> None:
        """Fetches the underlying's 5-min candles and recomputes the fast/slow
        EMA relationship on the last fully-closed candle, plus whether a
        crossover happened on that candle. Cached (see
        get_cached_ema_cross_*) and only re-fetched every
        config.EMA_CROSS_REFRESH_SECONDS.

        Blocking (REST) - call via run_in_executor from the poll loop only,
        never the WebSocket tick path; the poll loop keeps the cache warm
        enough for the tick path to read synchronously. Same threading /
        still-forming-candle-drop rules as refresh_supertrend_signal."""
        cached = self._ema_cross_cache.get(underlying_symbol)
        if cached and (datetime.now(IST) - cached[0]).total_seconds() < config.EMA_CROSS_REFRESH_SECONDS:
            return
        try:
            security_id = self._equity_security_id(underlying_symbol)
            # Continuous multi-session series (fetch_continuous_intraday) so
            # both EMAs are fully warm from the first bar of today's session -
            # a today-only fetch left them unusable until ~13 candles had
            # closed (~10:20 IST). The series runs across the overnight gap
            # exactly like a charting platform's, so a crossover on today's
            # first bar (vs the prior session's last) is a real crossover and
            # is treated as one - the caller's entry-candle skip
            # (_ema_cross_signal_for) is what stops a brand-new intraday
            # entry being whipsawed on its own entry bar.
            data = self.fetch_continuous_intraday(
                security_id, "NSE_EQ", "EQUITY", config.EMA_CROSS_INTERVAL_MINUTES,
            )
            closes = list(data.get("close") or [])
            timestamps = list(data.get("timestamp") or [])

            # Drop the still-forming candle if present - only fully-closed
            # candles drive the signal (identical logic to
            # refresh_supertrend_signal, kept in lockstep so timestamps[-1]
            # always matches closes[-1]).
            if timestamps:
                last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
                if datetime.now(IST) < last_candle_start + timedelta(minutes=config.EMA_CROSS_INTERVAL_MINUTES):
                    closes, timestamps = closes[:-1], timestamps[:-1]

            slow = config.EMA_CROSS_SLOW_PERIOD
            if len(closes) < slow + 2:
                logger.info("Not enough %d-min candles yet for %s EMA cross (%d bars)",
                            config.EMA_CROSS_INTERVAL_MINUTES, underlying_symbol, len(closes))
                return

            fast_ema = _compute_ema(closes, config.EMA_CROSS_FAST_PERIOD)
            slow_ema = _compute_ema(closes, slow)
            if fast_ema[-1] is None or slow_ema[-1] is None or fast_ema[-2] is None or slow_ema[-2] is None:
                return

            fast_below_slow = fast_ema[-1] < slow_ema[-1]
            prev_fast_below_slow = fast_ema[-2] < slow_ema[-2]
            crossed_this_candle = fast_below_slow != prev_fast_below_slow
            candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None
            self._ema_cross_cache[underlying_symbol] = (
                datetime.now(IST), fast_below_slow, crossed_this_candle, candle_start, closes[-1],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh EMA cross signal for %s", underlying_symbol)

    def get_cached_ema_cross_bearish(self, underlying_symbol: str) -> Optional[bool]:
        """Synchronous cache-only read: True if the fast EMA is below the slow
        EMA on the last fully-closed candle. None = not computed yet - treat
        as "no exit signal", never force an exit on missing data."""
        cached = self._ema_cross_cache.get(underlying_symbol)
        return cached[1] if cached else None

    def get_cached_ema_cross_crossed(self, underlying_symbol: str) -> Optional[bool]:
        """True if the fast/slow EMA relationship actually FLIPPED on the last
        fully-closed candle (a genuine crossover edge, not a standing state)."""
        cached = self._ema_cross_cache.get(underlying_symbol)
        return cached[2] if cached else None

    def get_cached_ema_cross_candle_start(self, underlying_symbol: str) -> Optional[datetime]:
        """Start timestamp (IST) of the fully-closed candle the cached EMA
        signal is based on - used the same way as
        get_cached_supertrend_candle_start (skip an exit still reading the
        position's own entry candle)."""
        cached = self._ema_cross_cache.get(underlying_symbol)
        return cached[3] if cached else None

    def get_cached_ema_cross_underlying_close(self, underlying_symbol: str) -> Optional[float]:
        """The underlying's own last-closed-candle price the cached EMA-cross
        signal was computed from - see get_cached_supertrend_underlying_close
        for why this exists."""
        cached = self._ema_cross_cache.get(underlying_symbol)
        return cached[4] if cached else None

    def get_cached_underlying_close(self, underlying_symbol: str) -> Optional[float]:
        """Best-effort current underlying price for the minimum-underlying-
        move confirmation gate (added 18 Sep 2026 - see
        reversal_filters.check_underlying_move_confirms_exit). Tries the
        Supertrend cache first, falls back to the EMA-cross cache - either
        signal being enabled and warm is enough, and both are refreshed by
        the same poll loop so they're never more than one refresh cycle
        apart. Synchronous, cache-only - safe from the WebSocket tick path.
        None means neither cache has data yet; callers must fail open on
        that, never treat missing data as a confirmed move."""
        px = self.get_cached_supertrend_underlying_close(underlying_symbol)
        if px is not None:
            return px
        return self.get_cached_ema_cross_underlying_close(underlying_symbol)

    # ------------------------------------------------------------------ #
    # Same-day RSI-gated loss re-entry block (added 11 Sep 2026, replacing
    # the old time-based LOSS_COOLDOWN_ENABLED/LOSS_COOLDOWN_MINUTES -
    # user request: "Remove this cooldown period logic from everywhere and
    # all strategies, instead create another global common function which
    # checks if RSI of 5 min candle is greater than number 88 or if RSI of
    # current candle is lesser than previous 5 min candle (means RSI is
    # falling), then don't take a trade for that stock in same day if
    # MAX_LOSS_HIT is already hit earlier for that day". One shared
    # computation (like Supertrend/EMA-cross above), consumed identically
    # by each package's own trading_engine.py, which combines this purely
    # market-data signal with its own trade_history.loss_exit_count_today
    # check - see Options/trading_engine.py's _process_one_entry for the
    # combined gate.
    # ------------------------------------------------------------------ #
    def refresh_rsi_signal(self, underlying_symbol: str) -> None:
        """Fetches the underlying's 5-min candles (continuous multi-session
        series - see fetch_continuous_intraday) and recomputes RSI(config.
        RSI_LOSS_REENTRY_PERIOD), caching the current and previous fully-
        closed candle's RSI value. Cached (see get_cached_rsi/get_cached_
        prev_rsi) and only re-fetched every config.RSI_LOSS_REENTRY_
        REFRESH_SECONDS - same cache-then-poll-refresh shape as
        refresh_supertrend_signal.

        Blocking (REST call) - call via run_in_executor from async code,
        same calling convention as refresh_supertrend_signal/refresh_ema_
        cross_signal."""
        cached = self._rsi_cache.get(underlying_symbol)
        if cached and (datetime.now(IST) - cached[0]).total_seconds() < config.RSI_LOSS_REENTRY_REFRESH_SECONDS:
            return
        try:
            security_id = self._equity_security_id(underlying_symbol)
            data = self.fetch_continuous_intraday(
                security_id, "NSE_EQ", "EQUITY", config.RSI_LOSS_REENTRY_INTERVAL_MINUTES,
            )
            closes = data.get("close") or []
            timestamps = data.get("timestamp") or []
            period = config.RSI_LOSS_REENTRY_PERIOD
            # Drop a still-forming last candle, same reasoning as
            # refresh_supertrend_signal - only a fully-closed candle's
            # close should drive this signal.
            if timestamps:
                last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
                if datetime.now(IST) < last_candle_start + timedelta(minutes=config.RSI_LOSS_REENTRY_INTERVAL_MINUTES):
                    closes, timestamps = closes[:-1], timestamps[:-1]
            # Need period+1 closes for the first RSI value, plus one more
            # confirmed bar so both a "current" and "previous" RSI exist.
            if len(closes) < period + 2:
                logger.info("Not enough %d-min candles yet for %s RSI (%d bars)",
                            config.RSI_LOSS_REENTRY_INTERVAL_MINUTES, underlying_symbol, len(closes))
                return
            rsi = _compute_rsi(closes, period)
            current_rsi, prev_rsi = rsi[-1], rsi[-2]
            candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None
            self._rsi_cache[underlying_symbol] = (datetime.now(IST), current_rsi, prev_rsi, candle_start)
        except Exception:  # noqa: BLE001
            logger.exception("RSI refresh failed for %s - keeping the last cached value, if any.", underlying_symbol)

    def get_cached_rsi(self, underlying_symbol: str) -> Optional[float]:
        """Synchronous cache-only read: RSI on the last fully-closed candle.
        None = not computed yet - treat as "don't block on missing data",
        same fail-open philosophy as every other cached signal here."""
        cached = self._rsi_cache.get(underlying_symbol)
        return cached[1] if cached else None

    def get_cached_prev_rsi(self, underlying_symbol: str) -> Optional[float]:
        """RSI on the candle immediately before the last fully-closed one -
        used to detect "RSI is falling" (current < previous)."""
        cached = self._rsi_cache.get(underlying_symbol)
        return cached[2] if cached else None

    def is_rsi_loss_reentry_blocked(self, underlying_symbol: str) -> bool:
        """Refreshes and evaluates the full RSI condition in one call - True
        if the current RSI is overbought (> config.RSI_LOSS_REENTRY_
        OVERBOUGHT) OR falling (current < previous confirmed candle's RSI).
        Always reads the threshold from Options.config regardless of which
        package calls this, same as every other shared signal here - keeps
        Futures/Luxury's own trading_engine.py from needing their own copy
        of RSI_LOSS_REENTRY_OVERBOUGHT/INTERVAL_MINUTES (they only need
        their own ENABLE_RSI_LOSS_REENTRY_BLOCK on/off switch).

        Fails OPEN (returns False, i.e. "don't block") when RSI isn't
        computed yet (not enough candles) - a data gap must never itself
        be the reason a stock stays blocked, same philosophy as every
        other cached signal in this class."""
        self.refresh_rsi_signal(underlying_symbol)
        rsi = self.get_cached_rsi(underlying_symbol)
        prev_rsi = self.get_cached_prev_rsi(underlying_symbol)
        if rsi is None or prev_rsi is None:
            return False
        return rsi > config.RSI_LOSS_REENTRY_OVERBOUGHT or rsi < prev_rsi

    def rsi_loss_reentry_reason(self, underlying_symbol: str) -> Optional[str]:
        """"overbought" or "falling" - whichever condition is_rsi_loss_
        reentry_blocked's True verdict was based on, for callers' log
        messages only (Futures/Luxury's own trading_engine.py don't carry
        their own copy of RSI_LOSS_REENTRY_OVERBOUGHT - this keeps that
        threshold fully encapsulated here, same as the block decision
        itself). Reads the SAME cached values is_rsi_loss_reentry_blocked
        just populated - call this right after it, not standalone (no
        fresh refresh here). None if neither condition holds or data is
        missing."""
        rsi = self.get_cached_rsi(underlying_symbol)
        prev_rsi = self.get_cached_prev_rsi(underlying_symbol)
        if rsi is None or prev_rsi is None:
            return None
        if rsi > config.RSI_LOSS_REENTRY_OVERBOUGHT:
            return "overbought"
        if rsi < prev_rsi:
            return "falling"
        return None

    # ------------------------------------------------------------------ #
    # Nifty50 open gap-down / sharp-fall CE cool-off (see config.py's own
    # "Nifty50 open gap-down / sharp-fall CE cool-off" comment block for
    # the full rationale). One market-wide fact, computed here ONCE and
    # shared by every strategy - same reasoning as refresh_supertrend_
    # signal always reading Options.config regardless of caller. Only ever
    # gates CE entries (see should_delay_ce_entry); PE is untouched.
    # ------------------------------------------------------------------ #
    # NSE index (spot), IDX_I segment - same confirmed IDs IndexScalping/
    # config.py's own INDEX_SECURITY_ID already uses in production. Shared
    # here (added 24 Sep 2026, Swing's own index-watchlist-symbol support
    # - see Swing/signals.py's _underlying_reference and index_security_id
    # below) so there's one canonical source instead of each caller
    # hardcoding its own copy of the same two IDs.
    INDEX_SECURITY_ID = {"NIFTY": "13", "BANKNIFTY": "25"}
    NIFTY_SECURITY_ID = INDEX_SECURITY_ID["NIFTY"]

    def index_security_id(self, underlying_symbol: str) -> str:
        """Resolves a known index's own IDX_I-segment security_id (for
        Supertrend/regime candle fetches - see Swing/signals.py's
        _underlying_reference) - the index counterpart to
        _equity_security_id above, which can never resolve one itself
        (Dhan's instrument master has no SEM_INSTRUMENT_NAME=="EQUITY" row
        for an index, so that lookup always raises for NIFTY/BANKNIFTY -
        the real gap this method closes). Deliberately a small, explicit
        dict rather than an instrument-master query - unlike an equity or
        option leg, an index has no per-symbol instrument-master row to
        look up in the first place; NIFTY/BANKNIFTY's own security_ids are
        well-known, fixed constants, already proven correct via
        should_delay_ce_entry's own real production use of the same IDs."""
        sid = self.INDEX_SECURITY_ID.get(underlying_symbol.upper())
        if sid is None:
            raise ValueError(f"No known index security_id for {underlying_symbol!r} - only "
                              f"{sorted(self.INDEX_SECURITY_ID)} are supported")
        return sid

    def evaluate_nifty_open_condition(self, now: Optional[datetime] = None) -> dict:
        """The GAP/FALL judgment itself is computed ONCE per trading day -
        "did Nifty gap down or open falling", a one-shot fact about the
        open - by whichever CE webhook alert is first to ask, then cached
        for the rest of the day. What callers DO with that fact is not
        one-shot, though: should_delay_ce_entry() re-checks live whether
        Nifty has recovered yet (is_nifty_recovering) every time it's
        called, so the actual hold length isn't decided here.

        Reads Nifty50's own continuous multi-session 1-min series (see
        fetch_continuous_intraday) to get:
          - prev_close: yesterday's last confirmed close
          - today_open: today's first bar's open
          - latest_close: the most recent confirmed close so far today
        and from those:
          - gap_points = today_open - prev_close;
            gap_down = gap_points <= -config.GAP_DOWN_THRESHOLD_POINTS
          - fall_pct = (today_open - latest_close) / today_open;
            sharp_falling = fall_pct >= config.GAP_DOWN_SHARP_FALL_PCT

        Returns a dict; "evaluated" is False (and nothing is cached yet,
        so the next call retries) if the fetch failed or today's series
        doesn't have at least one confirmed bar yet - fails OPEN (never
        blocks CE entries on missing data)."""
        now = now or datetime.now(IST)
        today = now.date()
        cached = self._nifty_open_condition_cache
        if cached is not None and cached["date"] == today:
            return cached

        not_yet = {"date": today, "evaluated": False, "delay_ce": False, "delay_until": None}
        try:
            data = self.fetch_continuous_intraday(self.NIFTY_SECURITY_ID, "IDX_I", "INDEX", 1)
        except Exception:  # noqa: BLE001
            logger.exception("Nifty open-gap check: intraday fetch failed - not blocking CE entries on this.")
            return not_yet

        timestamps = data.get("timestamp") or []
        opens = data.get("open") or []
        closes = data.get("close") or []
        if not timestamps or not opens or not closes:
            return not_yet

        bars = list(zip(timestamps, opens, closes))
        today_bars = [b for b in bars if datetime.fromtimestamp(b[0], tz=IST).date() == today]
        prior_bars = [b for b in bars if datetime.fromtimestamp(b[0], tz=IST).date() < today]
        # A still-forming last bar is fine to use here (unlike an exit
        # signal, we only need SOME confirmed print for today, and using
        # the freshest one makes the sharp-fall read more current, not
        # less accurate).
        if not today_bars or not prior_bars:
            return not_yet

        prev_close = prior_bars[-1][2]
        today_open = today_bars[0][1]
        latest_close = today_bars[-1][2]
        if not today_open or not prev_close:
            return not_yet

        gap_points = today_open - prev_close
        gap_down = gap_points <= -config.GAP_DOWN_THRESHOLD_POINTS
        fall_pct = (today_open - latest_close) / today_open
        sharp_falling = fall_pct >= config.GAP_DOWN_SHARP_FALL_PCT
        delay_ce = gap_down or sharp_falling

        # SCALED minimum delay - only the actual gap-down magnitude scales
        # it (a sharp-fall-only trigger has no comparable "gap size", so it
        # always gets the plain base minutes). See config.py's own comment
        # block for the formula and the real gap-down day it's tuned from.
        if gap_down:
            excess_points = max(0.0, abs(gap_points) - config.GAP_DOWN_THRESHOLD_POINTS)
            scaled_minutes = min(
                config.GAP_DOWN_MAX_DELAY_MINUTES,
                config.GAP_DOWN_CE_DELAY_MINUTES
                + config.GAP_DOWN_EXTRA_DELAY_MINUTES_PER_100_POINTS * (excess_points / 100.0),
            )
        else:
            scaled_minutes = min(config.GAP_DOWN_MAX_DELAY_MINUTES, config.GAP_DOWN_CE_DELAY_MINUTES)

        market_open_dt = datetime.combine(today, dtime.fromisoformat(config.MARKET_OPEN_TIME), tzinfo=IST)
        result = {
            "date": today, "evaluated": True,
            "prev_close": prev_close, "today_open": today_open, "latest_close": latest_close,
            "gap_points": round(gap_points, 2), "gap_down": gap_down,
            "fall_pct": round(fall_pct * 100, 3), "sharp_falling": sharp_falling,
            "delay_ce": delay_ce,
            "scaled_delay_minutes": round(scaled_minutes, 1) if delay_ce else None,
            "delay_until": (market_open_dt + timedelta(minutes=scaled_minutes)) if delay_ce else None,
            "hard_cap_until": (market_open_dt + timedelta(minutes=config.GAP_DOWN_MAX_DELAY_MINUTES)) if delay_ce else None,
        }
        self._nifty_open_condition_cache = result
        if delay_ce:
            logger.warning(
                "Nifty50 open condition: gap=%.1f pts (open=%.2f prev_close=%.2f) fall=%.2f%% from open "
                "-> CE entries delayed at least until %s (scaled %.1f min)%s, hard cap %s",
                gap_points, today_open, prev_close, fall_pct * 100,
                result["delay_until"].strftime("%H:%M"), scaled_minutes,
                " + wait for Nifty to turn green" if config.ENABLE_NIFTY_RECOVERY_GATE else "",
                result["hard_cap_until"].strftime("%H:%M"),
            )
        else:
            logger.info(
                "Nifty50 open condition: gap=%.1f pts (open=%.2f prev_close=%.2f) fall=%.2f%% from open "
                "-> no CE delay",
                gap_points, today_open, prev_close, fall_pct * 100,
            )
        return result

    def is_nifty_recovering(self, today_open: float, now: Optional[datetime] = None) -> bool:
        """True if Nifty50's still-forming daily candle has turned GREEN -
        its latest close back at or above `today_open` - the "wait until
        it starts recovering" half of the gap-down cool-off (see should_
        delay_ce_entry). Re-fetched at most every config.NIFTY_RECOVERY_
        REFRESH_SECONDS (this gets polled on every CE webhook alert while
        a delay is pending, unlike evaluate_nifty_open_condition's
        once-a-day cache).

        Fails OPEN (returns True, i.e. "treat as recovered, don't extend
        the delay on this") on a fetch failure - a data hiccup must never
        itself be the reason CE stays blocked, same philosophy as every
        other signal in this class."""
        now = now or datetime.now(IST)
        cached = self._nifty_recovery_cache
        if cached and (now - cached[0]).total_seconds() < config.NIFTY_RECOVERY_REFRESH_SECONDS:
            return cached[1]
        try:
            data = self.fetch_continuous_intraday(self.NIFTY_SECURITY_ID, "IDX_I", "INDEX", 1)
        except Exception:  # noqa: BLE001
            logger.exception("Nifty recovery check: intraday fetch failed - treating as recovered (fail open).")
            return True

        timestamps = data.get("timestamp") or []
        closes = data.get("close") or []
        today = now.date()
        today_closes = [c for t, c in zip(timestamps, closes) if datetime.fromtimestamp(t, tz=IST).date() == today]
        if not today_closes or not today_open:
            return True

        recovering = today_closes[-1] >= today_open
        self._nifty_recovery_cache = (now, recovering)
        return recovering

    def should_delay_ce_entry(self, now: Optional[datetime] = None) -> bool:
        """Generic, strategy-agnostic gate for use at every CE webhook entry
        point (Options/Futures/Luxury): True while Nifty50's open condition
        (see evaluate_nifty_open_condition) called for a delay AND we
        haven't cleared it yet. Clearing requires BOTH:
          1. Past the scaled minimum delay (result["delay_until"]) - a
             bigger gap always waits at least proportionally longer,
             regardless of how fast price bounces right after open.
          2. If config.ENABLE_NIFTY_RECOVERY_GATE is on, Nifty's own daily
             candle has turned green (is_nifty_recovering) - otherwise
             the delay keeps extending, capped at result["hard_cap_until"]
             (past that, CE always resumes regardless of Nifty's color).
        PE entries must never call this - a falling Nifty is exactly when
        a PE-buying alert should act."""
        now = now or datetime.now(IST)
        result = self.evaluate_nifty_open_condition(now)
        if not result.get("delay_ce") or result.get("delay_until") is None:
            return False
        if now >= result["hard_cap_until"]:
            return False
        if now < result["delay_until"]:
            return True
        if not config.ENABLE_NIFTY_RECOVERY_GATE:
            return False
        return not self.is_nifty_recovering(result["today_open"], now)

    # ------------------------------------------------------------------ #
    # Global gap-down block (added 24 Sep 2026, user request - see
    # nifty_market_guard.py's own module docstring for the full spec
    # history: originally a 3-tier "block/PE-only/allow" design with a
    # 3-consecutive-falling-day trigger, simplified twice down to this
    # single rule). Deliberately SEPARATE from should_delay_ce_entry
    # above - that one only ever delays CE for a short (~30min .env-
    # configured) window and auto-recovers; this one blocks EVERY entry
    # (CE and PE, Options/Futures/Luxury) for the WHOLE trading day, no
    # recovery/expiry, once set. Reuses evaluate_nifty_open_condition's
    # already-cached today_open/prev_close (same one-shot-per-day fetch
    # should_delay_ce_entry itself reuses) rather than a second fetch.
    # ------------------------------------------------------------------ #
    def should_block_all_entries_today(self, now: Optional[datetime] = None) -> bool:
        """True for the entire trading day once Nifty50 has opened with a
        gap DOWN of config.GAP_DOWN_THRESHOLD_POINTS (100) or more -
        never true on a gap UP of any size, and never true for a gap down
        under that threshold (see nifty_market_guard.classify_day's own
        docstring - binary, gap-down-only, nothing else considered).
        Fails OPEN (returns False) if evaluate_nifty_open_condition
        hasn't resolved yet (no fetch of its own - piggybacks entirely on
        that method's own cache/fetch/fail-open behavior)."""
        cond = self.evaluate_nifty_open_condition(now)
        if not cond.get("evaluated"):
            return False
        state = nifty_market_guard.NiftyDayState(
            trading_date=cond["date"], prev_close=cond["prev_close"], open=cond["today_open"],
        )
        return nifty_market_guard.classify_day(state) == "BLOCK_ALL"

    # ------------------------------------------------------------------ #
    # Liquidity guard (added 2 Sep 2026, see config.LIQUIDITY_GUARD_
    # ZERO_VOLUME_BARS's own docstring for the CHOLAFIN incident this
    # was built from). Computed on the OPTION's OWN candles - unlike
    # Supertrend above, illiquidity is a property of the specific
    # contract being held, not the underlying stock, so this is keyed by
    # option_trading_symbol.
    # ------------------------------------------------------------------ #
    def refresh_liquidity_signal(
        self, option_trading_symbol: str, expected_exchange: str = "NSE",
        exchange_segment: str = "NSE_FNO", instrument_type: str = "OPTSTK",
    ) -> None:
        """Fetches the OPTION's own 1-min candles (continuous multi-session
        series) and checks whether the last config.LIQUIDITY_GUARD_ZERO_
        VOLUME_BARS fully-closed bars ALL show exactly zero traded volume - a thinly-traded
        contract going quiet for several minutes straight, the precursor
        pattern behind an un-catchable price gap (confirmed live via a
        real 1-min replay of the CHOLAFIN MAX_LOSS_HIT overshoot, 3 Sep
        2026). Cached (see get_cached_illiquid) and only re-fetched every
        config.LIQUIDITY_GUARD_REFRESH_SECONDS - same throttling
        reasoning as refresh_supertrend_signal.

        `expected_exchange`/`exchange_segment`/`instrument_type` default to
        NSE stock options (every existing caller - Options/Futures/Luxury's
        own exit-time LIQUIDITY_GUARD_ENABLED checks are NSE-only and stay
        byte-identical). get_liquid_atm_option's own MCX candidates (added
        18 Sep 2026, extending the liquid-contract gate to Swing's Copper)
        pass "MCX"/"MCX_COMM"/"OPTFUT" instead - confirmed empirically
        against the real instrument master (SEM_EXCH_INSTRUMENT_TYPE=
        "OPTFUT" for every real MCX COPPER option row), not guessed.

        Blocking (REST call) - call via run_in_executor from async code,
        and only from the poll loop, never the WebSocket tick path (same
        restriction as refresh_supertrend_signal, for the same reason)."""
        cached = self._liquidity_cache.get(option_trading_symbol)
        if cached and (datetime.now(IST) - cached[0]).total_seconds() < config.LIQUIDITY_GUARD_REFRESH_SECONDS:
            return
        try:
            security_id = self._instrument_meta(
                option_trading_symbol, expected_exchange=expected_exchange,
            )["security_id"]
            # Continuous multi-session 1-min series (fetch_continuous_intraday)
            # rather than today-only: the "last N bars all zero volume" check
            # can then fire in the first N minutes of the session too, using
            # the prior session's tail - a contract that stopped trading
            # yesterday afternoon and still isn't trading at today's open is
            # exactly the quiet-then-gap pattern this guard exists to catch.
            data = self.fetch_continuous_intraday(security_id, exchange_segment, instrument_type, 1)
            volumes = data.get("volume") or []
            timestamps = data.get("timestamp") or []
            n = config.LIQUIDITY_GUARD_ZERO_VOLUME_BARS

            # Drop the current, still-forming candle if Dhan included one -
            # same guard refresh_supertrend_signal uses. A partially-formed
            # bar's own volume-so-far can look artificially low/zero simply
            # because the minute hasn't finished yet, not because the
            # contract is actually illiquid.
            if timestamps:
                last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
                if datetime.now(IST) < last_candle_start + timedelta(minutes=1):
                    volumes = volumes[:-1]

            if len(volumes) < n:
                # Not enough completed bars yet to judge (e.g. right after
                # market open, or right after entry) - treat as liquid
                # rather than guessing an illiquidity verdict from too
                # little data.
                is_illiquid = False
            else:
                is_illiquid = all(v == 0 for v in volumes[-n:])
            self._liquidity_cache[option_trading_symbol] = (datetime.now(IST), is_illiquid)
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh liquidity signal for %s", option_trading_symbol)

    def get_cached_illiquid(self, option_trading_symbol: str) -> Optional[bool]:
        """Synchronous, cache-only read - safe to call from the WebSocket
        tick path without blocking the event loop. None means no signal has
        been computed yet (e.g. right after entry, before the poll loop's
        first refresh) - callers should treat that as "not illiquid", not
        force an exit on missing data."""
        cached = self._liquidity_cache.get(option_trading_symbol)
        return cached[1] if cached else None

    def get_last_historical_close(
        self, option_trading_symbol: str, expected_exchange: str = "NSE",
        exchange_segment: str = "NSE_FNO", instrument_type: str = "OPTSTK",
    ) -> Optional[float]:
        """Fallback-of-the-fallback price source for when the LIVE LTP path
        (WebSocket + REST get_option_ltp) has been dead for a while - the
        option's own historical 1-min candles, which stay available even
        during a real live-quote outage (confirmed via the ICICIPRULI
        incident, 10 Sep 2026: a 384-bar historical read succeeded the
        whole day the live LTP feed was down - see trading-skills'
        incidents/2026-09-10-icicipruli-unmonitorable-position.md). Reuses
        the exact same fetch shape as refresh_liquidity_signal (security_id
        via _instrument_meta, 1-min interval).

        Originally scoped to forced-exit logging/pnl estimates only; also
        used (18 Sep 2026, ABB 29 SEP 7200 CALL incident) as _get_ltp's
        second-tier fallback for the regular poll-loop exit check itself
        when get_option_ltp fails - a name-based quote lookup can be flaky
        even on a genuinely liquid, actively-trading contract, and this
        security_id-based read keeps MAX_LOSS_HIT/etc. evaluating on SOME
        real price instead of going completely blind for minutes. Never
        something a real order depends on either way - a market SELL/BUY
        order needs no price input at all and fills at whatever the
        exchange's own current best price is regardless of what this
        function returns. Returns None (not an exception) on any failure -
        callers should fall back to something else (e.g. position.entry_price,
        or re-raising to let the caller's own staleness handling take over)
        rather than block on this being unavailable too.

        `expected_exchange`/`exchange_segment`/`instrument_type` default to
        NSE stock options - every existing NSE call site (Options/Futures/
        Luxury's own _get_ltp fallback) stays byte-identical. Extended to
        MCX 18 Sep 2026 (same day, user follow-up request after observing a
        real, consistent first-attempt LTP hiccup on COPPER/NATURALGAS
        during live MCX trading hours - self-healing via get_option_ltp's
        own retry so far, but with no fallback tier at all for the case
        where it doesn't) - pass "MCX"/"MCX_COMM"/"OPTFUT" instead, the
        same real, empirically-confirmed segment codes get_liquid_atm_
        option already uses (Swing/trading_engine.py's own _get_ltp and
        _handle_ltp_staleness now do exactly this for an MCX position)."""
        try:
            security_id = self._instrument_meta(
                option_trading_symbol, expected_exchange=expected_exchange,
            )["security_id"]
            data = self.fetch_continuous_intraday(security_id, exchange_segment, instrument_type, 1)
            closes = data.get("close") or []
            timestamps = data.get("timestamp") or []
            if timestamps:
                last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
                if datetime.now(IST) < last_candle_start + timedelta(minutes=1):
                    closes = closes[:-1]  # still-forming candle - same drop as refresh_liquidity_signal
            return float(closes[-1]) if closes else None
        except Exception:  # noqa: BLE001
            logger.exception("Could not fetch a historical fallback close for %s", option_trading_symbol)
            return None

    # ------------------------------------------------------------------ #
    # Portfolio (positions already open at the broker)
    # ------------------------------------------------------------------ #
    def _true_open_entry_price(
        self, security_id: str, trading_symbol: str, net_qty: int, fallback_avg_price: float,
    ) -> float:
        """The real cost basis of the CURRENTLY open `net_qty`, robust to a
        same-day close-and-reopen on this exact contract - unlike Dhan's own
        `buyAvg`/`costPrice` (used directly as every _get_open_*_positions_
        once's avg_price until 17 Sep 2026), which is a DAY-CUMULATIVE
        average across every buy fill today and does NOT reset when the
        position is fully squared off intraday.

        Real incident (17 Sep 2026): a Swing COPPER position was manually
        opened at 9.0, closed by the bot at 10.14 (PROFIT_PROTECTION_HIT),
        then manually reopened at 10.6 the same day. On the next restart,
        reconciliation reported entry_price=9.8 - exactly (9.0+10.6)/2, i.e.
        Dhan's buyAvg blending BOTH the already-closed lot and the new one -
        which put target_price/hard_stop_loss on the wrong basis (a wider,
        more permissive stop than the real 10.6 entry warranted). This
        function reconstructs the true basis directly from today's own
        filled orders instead of trusting that field.

        Algorithm: walk every filled order for this security today, in
        chronological order, tracking running net quantity and a pool of
        (price, qty) "lots" that make up the CURRENTLY open leg. A fill in
        the SAME direction as the running position adds a new lot; a fill in
        the OPPOSITE direction reduces the pool - proportionally shrinking
        every lot's quantity (mathematically exact for a pure weighted-
        average cost basis, since which specific lot is "reduced first"
        doesn't change the remaining average) if it's a partial reduction,
        or discarding the whole pool (keeping only genuine leftover, for a
        fill big enough to flip the position's direction) if it closes the
        leg entirely. Whatever remains once every fill is processed is, by
        construction, exactly the current open leg - its weighted average
        is the true entry price.

        Sanity-checked against the broker's own reported `net_qty`: if the
        reconstructed quantity doesn't match (a data gap, an order missing
        from the day's list, a non-day-boundary edge case), this falls back
        to `fallback_avg_price` (the old buyAvg-based value) rather than
        risk silently computing a WORSE answer than what reconciliation
        already had. Same fallback on any fetch/parse error - this must
        never block reconciliation itself."""
        try:
            resp = self.client.Dhan.get_order_list()
            if resp.get("status") != "success":
                raise RuntimeError(f"get_order_list failed: {resp.get('remarks')}")

            fills = []
            for o in (resp.get("data") or []):
                filled_qty = int(o.get("filledQty") or 0)
                if filled_qty <= 0:
                    continue
                symbol_matches = o.get("tradingSymbol") == trading_symbol
                security_matches = str(o.get("securityId", "")) == str(security_id)
                if not (symbol_matches or security_matches):
                    continue
                price = float(o.get("averageTradedPrice") or 0)
                if price <= 0:
                    continue
                transaction_type = o.get("transactionType")
                if transaction_type not in ("BUY", "SELL"):
                    continue
                signed_qty = filled_qty if transaction_type == "BUY" else -filled_qty
                fills.append((str(o.get("createTime") or ""), price, signed_qty))
            fills.sort(key=lambda f: f[0])

            running_qty = 0
            open_lots: list[list[float]] = []  # [price, qty] pairs, qty always positive
            for _, price, signed_qty in fills:
                if running_qty == 0:
                    open_lots = [[price, abs(signed_qty)]]
                elif (running_qty > 0) == (signed_qty > 0):
                    open_lots.append([price, abs(signed_qty)])
                else:
                    reduce_qty = abs(signed_qty)
                    held_qty = abs(running_qty)
                    if reduce_qty >= held_qty:
                        leftover = reduce_qty - held_qty
                        open_lots = [[price, leftover]] if leftover > 0 else []
                    else:
                        total_qty = sum(q for _, q in open_lots)
                        scale = (total_qty - reduce_qty) / total_qty if total_qty else 0.0
                        open_lots = [[p, q * scale] for p, q in open_lots]
                running_qty += signed_qty

            total_qty = sum(q for _, q in open_lots)
            if total_qty <= 0 or round(total_qty) != abs(net_qty):
                logger.warning(
                    "Reconstructed open quantity (%.2f) for %s doesn't match broker's own "
                    "net_qty (%d) - falling back to buyAvg/costPrice (%.4f) rather than trust "
                    "a possibly-incomplete reconstruction.", total_qty, trading_symbol, net_qty, fallback_avg_price,
                )
                return fallback_avg_price
            return sum(p * q for p, q in open_lots) / total_qty
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not reconstruct true entry price for %s from today's order list - "
                "falling back to buyAvg/costPrice (%.4f).", trading_symbol, fallback_avg_price,
            )
            return fallback_avg_price

    def get_open_fno_positions(self) -> list[dict]:
        """Every NSE F&O position currently open at Dhan (net quantity != 0).
        avg_price is Dhan's own reported average buy price for the position
        (buyAvg, falling back to costPrice), not something we computed.

        Retried (see _retry) - found + fixed 31 Aug 2026 (user request, a
        "dummy webhook call" health check surfaced the related paper_
        webhook.py pacing bug and prompted checking this too). This is
        called by has_open_position_for_underlying() on every entry
        attempt - now potentially CONCURRENTLY for up to 4 ranked stocks
        at once (entry #50's parallelized enter_positions_for_stocks) with
        no retry wrapper at all before this fix, unlike get_atm_option/
        get_day_change_pct which already had one for the identical reason
        ("Dhan's market-data calls can transiently rate-limit-fail" - see
        _retry's own docstring). A transient failure here previously meant
        that stock's entry was abandoned outright rather than retried."""
        return _retry(self._get_open_fno_positions_once)

    def _get_open_fno_positions_once(self) -> list[dict]:
        resp = self.client.Dhan.get_positions()
        if resp.get("status") != "success":
            raise RuntimeError(f"get_positions failed: {resp.get('remarks')}")

        open_positions = []
        for p in (resp.get("data") or []):
            net_qty = int(p.get("netQty") or 0)
            if net_qty == 0 or p.get("exchangeSegment") != "NSE_FNO":
                continue

            security_id = str(p.get("securityId", ""))
            try:
                # Keyed by security_id (unique), not Dhan's raw tradingSymbol
                # field - see _instrument_meta_by_security_id's docstring for
                # why matching by the symbol string is unreliable here.
                meta = self._instrument_meta_by_security_id(security_id)
            except ValueError:
                logger.warning(
                    "Open broker position security_id=%s (tradingSymbol=%s) not found "
                    "in instrument master; skipping it for reconciliation.",
                    security_id, p.get("tradingSymbol"),
                )
                continue

            drv_type = p.get("drvOptionType") or ""
            option_type = "CE" if drv_type == "CALL" else ("PE" if drv_type == "PUT" else "")

            open_positions.append({
                "trading_symbol": meta["trading_symbol"],
                "underlying_symbol": meta["underlying_symbol"],
                "option_type": option_type,
                "lot_size": meta["lot_size"],
                "quantity": net_qty,
                "avg_price": self._true_open_entry_price(
                    security_id, meta["trading_symbol"], net_qty, float(p.get("buyAvg") or p.get("costPrice") or 0),
                ),
                # MUST be preserved and used for the exit order later -
                # confirmed live that a mismatched product_type gets the
                # SELL RMS-rejected as a fresh naked short rather than
                # recognized as squaring off this position.
                "product_type": p.get("productType") or config.OPTIONS_PRODUCT,
            })
        return open_positions

    def has_open_position_for_underlying(self, underlying_symbol: str) -> bool:
        """Broker-side check (in addition to our own local dedup) that
        there's no existing open FNO position for this underlying - guards
        against duplicate entries from another process instance, a manual
        trade, or state we haven't reconciled yet."""
        return any(
            p["underlying_symbol"] == underlying_symbol
            for p in self.get_open_fno_positions()
        )

    def get_open_equity_positions(self) -> list[dict]:
        """Equity-segment counterpart to get_open_fno_positions above (added
        12 Sep 2026, Swing v2's equity basket-type). Necessary because
        get_positions() returns BOTH segments together and
        get_open_fno_positions hard-filters to "NSE_FNO" only - without
        this, an open equity position is invisible to every reconciliation
        path that reads "open positions," which would make
        get_broker_net_quantity(segment="NSE_EQ") wrongly report 0 (see
        that function's own docstring for why that's actively dangerous,
        not just incomplete)."""
        return _retry(self._get_open_equity_positions_once)

    def _get_open_equity_positions_once(self) -> list[dict]:
        resp = self.client.Dhan.get_positions()
        if resp.get("status") != "success":
            raise RuntimeError(f"get_positions failed: {resp.get('remarks')}")
        open_positions = []
        for p in (resp.get("data") or []):
            net_qty = int(p.get("netQty") or 0)
            if net_qty == 0 or p.get("exchangeSegment") != "NSE_EQ":
                continue
            trading_symbol = str(p.get("tradingSymbol", ""))
            open_positions.append({
                "trading_symbol": trading_symbol,
                "underlying_symbol": trading_symbol,
                "quantity": net_qty,
                "avg_price": self._true_open_entry_price(
                    str(p.get("securityId", "")), trading_symbol, net_qty,
                    float(p.get("buyAvg") or p.get("costPrice") or 0),
                ),
                "product_type": p.get("productType") or "CNC",
            })
        return open_positions

    def get_open_mcx_positions(self) -> list[dict]:
        """MCX-segment counterpart to get_open_equity_positions above (added
        12 Sep 2026, Swing v2's Copper options support). Necessary for the
        same reason equity needed its own: get_open_fno_positions hard-
        filters to "NSE_FNO" only, so a real open MCX position (Dhan's own
        exchangeSegment for it is "MCX_COMM" - the same string already
        used throughout this file's MCX market-data calls) is completely
        invisible without this, which would make
        get_broker_net_quantity(segment="MCX_COMM") wrongly report 0 -
        see that function's own docstring for why that's actively
        dangerous, not just incomplete."""
        return _retry(self._get_open_mcx_positions_once)

    def _get_open_mcx_positions_once(self) -> list[dict]:
        """Mirrors _get_open_fno_positions_once's own pattern exactly:
        keyed by security_id (unique) via _instrument_meta_by_security_id,
        not Dhan's raw tradingSymbol string (unreliable - see that
        function's own docstring), with option_type derived from the same
        drvOptionType field FUTCOM/OPTFUT positions report identically to
        FUTSTK/OPTSTK ones."""
        resp = self.client.Dhan.get_positions()
        if resp.get("status") != "success":
            raise RuntimeError(f"get_positions failed: {resp.get('remarks')}")

        open_positions = []
        for p in (resp.get("data") or []):
            net_qty = int(p.get("netQty") or 0)
            if net_qty == 0 or p.get("exchangeSegment") != "MCX_COMM":
                continue

            security_id = str(p.get("securityId", ""))
            try:
                meta = self._instrument_meta_by_security_id(security_id)
            except ValueError:
                logger.warning(
                    "Open MCX broker position security_id=%s (tradingSymbol=%s) not found "
                    "in instrument master; skipping it for reconciliation.",
                    security_id, p.get("tradingSymbol"),
                )
                continue

            drv_type = p.get("drvOptionType") or ""
            option_type = "CE" if drv_type == "CALL" else ("PE" if drv_type == "PUT" else "")

            open_positions.append({
                "trading_symbol": meta["trading_symbol"],
                "underlying_symbol": meta["underlying_symbol"],
                "option_type": option_type,
                "lot_size": meta["lot_size"],
                "quantity": net_qty,
                # security_id-based matching inside _true_open_entry_price is
                # what makes this work despite Dhan's own get_order_list()
                # echoing an MCX order's tradingSymbol in a different,
                # hyphenated format ("COPPER-23Sep2026-1400-CE") that never
                # equals meta["trading_symbol"]'s canonical space-separated
                # form - see get_pending_order_id's docstring for the same
                # format mismatch found independently.
                "avg_price": self._true_open_entry_price(
                    security_id, meta["trading_symbol"], net_qty, float(p.get("buyAvg") or p.get("costPrice") or 0),
                ),
                "product_type": p.get("productType") or "MARGIN",
            })
        return open_positions

    def get_broker_net_quantity(self, trading_symbol: str, segment: str = "NSE_FNO") -> int:
        """Net quantity currently held at the broker for this EXACT contract
        (matched on trading_symbol, not just underlying - a manual trade on
        a different strike for the same underlying shouldn't be confused
        with the specific leg we're trying to exit). 0 if flat or not found
        in the open-positions list at all. Used to reconcile a position we
        believe is still open against broker truth after repeated exit
        failures (see trading_engine._exit_position) - confirmed live on 26
        Aug 2026 that a user manually closing a position out-of-band (or a
        stuck RMS rejection resolving itself) can leave the bot blindly
        retrying a SELL for something that's already flat, burning API
        calls on doomed order placements instead of one cheap position
        check.

        `segment` (added 12 Sep 2026, Swing v2's equity basket-type;
        extended 12 Sep 2026 for MCX) - defaults to "NSE_FNO" so every
        existing Options/Futures/Luxury caller is byte-identical to
        before. Pass "NSE_EQ" for an equity position or "MCX_COMM" for an
        MCX commodity position: without branching on these, that leg
        would read as qty=0 against the FNO-only list even while
        genuinely open at the broker, and the exit-reconciliation logic
        in _exit_position would misread that as "already flat" and skip
        the real exit order entirely - silently orphaning a real position,
        not a cosmetic gap."""
        if segment == "NSE_EQ":
            positions = self.get_open_equity_positions()
        elif segment == "MCX_COMM":
            positions = self.get_open_mcx_positions()
        else:
            positions = self.get_open_fno_positions()
        for p in positions:
            if p["trading_symbol"] == trading_symbol:
                return p["quantity"]
        return 0

    def get_pending_order_id(
        self, trading_symbol: str, transaction_type: str, expected_exchange: Optional[str] = None,
    ) -> Optional[str]:
        """order_id of an existing non-terminal broker order for this EXACT
        contract + transaction_type, or None if there isn't one. Used to
        avoid placing a duplicate exit order on top of one already
        outstanding at the broker.

        expected_exchange (added 17 Sep 2026, real incident - a genuine
        resting COPPER SL-L order this scan needed to find for the first
        time): passed straight through to _instrument_meta, same as every
        other MCX-aware caller in this file. Without it, this scan's own
        security_id lookup below defaults to NSE-preferring resolution and
        can resolve an MCX symbol string to a WRONG, unrelated NSE row's
        security_id instead (confirmed live: "COPPER 23 SEP 1400 CALL"
        resolved to security_id 123831 with no hint, vs the real 574836
        with expected_exchange="MCX") - the exact "cross-exchange
        instrument-master collision" class of bug already fixed for ATM
        resolution (12 Sep 2026, commit c7ffaa3) but never ported to this
        function, since it predates Copper/MCX support entirely. Optional
        and defaults to None (today's exact behavior, zero risk) since
        Options/Futures/Luxury only ever call this for NSE symbols, where
        no such ambiguity exists - only Swing's own MCX call sites need to
        pass "MCX" here.

        Confirmed live on 26 Aug 2026 (BHARATFORG): our own in-memory
        pending_exit_order_id tracking is wiped by a restart, but a SELL
        order placed just before that restart can still be sitting PENDING
        at the broker. If a second SELL then gets placed against the same
        holding before the first resolves, Dhan's RMS doesn't necessarily
        net them together up front - it can price the second one as if it
        might create a fresh naked short and demand full margin for it,
        rejecting with "insufficient funds" even though the position is
        just being closed. This matches Dhan's own documented guidance
        ("check if you have any pending order - your margin is blocked for
        your pending order... cancel that order") - see NOTES.md's
        design-decision entry for the sources.

        Matches on EITHER the raw tradingSymbol string OR the resolved
        security_id (added 15 Sep 2026, real incident): this scan used to
        match on tradingSymbol alone, which silently missed JSWENERGY's
        genuinely-resting broker-side SL-L stop order at exit time - the
        position had been reconciled from the broker earlier that session
        (reconciliation has no way to discover a pre-existing resting
        order, so Position.stop_loss_order_id was empty, leaving THIS scan
        as the only remaining safety net), and Dhan's order-list API can
        echo tradingSymbol in a different format (SEM_TRADING_SYMBOL)
        than this codebase's own trading_symbol values (always SEM_CUSTOM_
        SYMBOL - see _instrument_meta's own docstring) - a plain string
        match can miss a real match that a security_id comparison would
        catch. _instrument_meta already resolves either format to the
        same security_id, so this reuses it rather than inventing a new
        lookup; a failure to resolve it here (e.g. the instrument isn't in
        today's master) just means falling back to the string match alone,
        never a hard failure of this safety check.

        Status check switched from an explicit allow-list (which
        included "TRIGGER_PENDING" - not actually one of DhanHQ's
        documented order statuses, see OrderStatus's own docstring) to
        OrderStatus.TERMINAL_STATUSES as a deny-list: ANY status that
        ISN'T definitively terminal (REJECTED/CANCELLED/TRADED/EXPIRED)
        means the order can still affect real money and must be treated
        as "still there," rather than trying to keep guessing every
        intermediate status string Dhan might actually use.

        Even with both improvements, this scan can still miss an order
        that is only seconds old (Dhan OMS lag - OIL, 10 Sep 2026), so a
        caller that already holds a specific id (Position.stop_loss_order_
        id) must prefer that over relying on this scan alone."""
        try:
            security_id = self._instrument_meta(trading_symbol, expected_exchange=expected_exchange).get("security_id")
        except Exception:  # noqa: BLE001
            security_id = None

        resp = self.client.Dhan.get_order_list()
        if resp.get("status") != "success":
            raise RuntimeError(f"get_order_list failed: {resp.get('remarks')}")
        for order in (resp.get("data") or []):
            symbol_matches = order.get("tradingSymbol") == trading_symbol
            security_matches = security_id is not None and str(order.get("securityId", "")) == security_id
            if not (symbol_matches or security_matches):
                continue
            if order.get("transactionType") != transaction_type:
                continue
            if order.get("orderStatus") in OrderStatus.TERMINAL_STATUSES:
                continue
            return order.get("orderId")
        return None

    def cancel_order(self, order_id: str) -> None:
        """Cancels a still-outstanding broker order. Raises if the cancel
        itself fails (e.g. the order already resolved by the time this
        runs) - the caller treats that as non-fatal and proceeds with a
        fresh order placement regardless, same as any other best-effort
        cleanup step in this file."""
        resp = self.client.Dhan.cancel_order(order_id)
        if resp.get("status") != "success":
            raise RuntimeError(f"cancel_order({order_id}) failed: {resp.get('remarks')}")

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def place_market_order(
        self, trading_symbol: str, quantity: int, transaction_type: str,
        tag: Optional[str] = None, product_type: Optional[str] = None,
    ) -> dict:
        """Places a MARKET order. Outside market hours this is placed as an
        AMO (Dhan requires the explicit afterMarketOrder flag - unlike
        Groww it does not auto-detect this from placement time).

        product_type MUST match whatever the position was actually opened
        under when this is an exit (SELL) - defaults to
        config.OPTIONS_PRODUCT, which is only correct for entries we placed
        ourselves. See Position.product_type's docstring."""
        is_amo = not self.is_market_open()
        product_type = product_type or config.OPTIONS_PRODUCT

        logger.info("Placing %s order: %s x%s (product=%s)%s", transaction_type, trading_symbol,
                    quantity, product_type, " (AMO)" if is_amo else "")
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange=config.DEFAULT_EXCHANGE,
            quantity=quantity,
            price=0,
            trigger_price=0,
            order_type="MARKET",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=is_amo,
            amo_time="OPEN",
            tag=tag,
        )
        if not order_id:
            # Tradehull's order_placement() swallows the underlying error
            # (prints it, returns None) instead of raising it to us - see
            # its own console/log output for the actual cause.
            raise RuntimeError(
                f"order_placement returned no order id for {transaction_type} {trading_symbol} "
                "- check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id), "is_amo": is_amo}

    def place_equity_market_order(
        self, trading_symbol: str, quantity: int, transaction_type: str,
        tag: Optional[str] = None, product_type: str = "CNC",
    ) -> dict:
        """Equity-segment counterpart to place_market_order above (added 12
        Sep 2026, Swing v2's equity basket-type) - NOT a reuse of that
        function with a parameter tweak, because place_market_order hard-
        codes exchange=config.DEFAULT_EXCHANGE ("NFO"), which is simply
        wrong for a plain NSE cash-segment order. Tradehull's own
        order_placement() maps a plain "NSE" exchange string to its cash
        segment internally (confirmed against the vendored library) -
        distinct from "NFO" (F&O). product_type defaults to "CNC" (real
        delivery), not "MIS", since Swing v2's equity basket-type is
        long-only and meant to carry for days - an intraday MIS product
        would auto-square-off same day regardless of the strategy's own
        exit signal, which would be silently wrong here."""
        is_amo = not self.is_market_open()
        logger.info("Placing equity %s order: %s x%s (product=%s)%s", transaction_type, trading_symbol,
                    quantity, product_type, " (AMO)" if is_amo else "")
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange="NSE",
            quantity=quantity,
            price=0,
            trigger_price=0,
            order_type="MARKET",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=is_amo,
            amo_time="OPEN",
            tag=tag,
        )
        if not order_id:
            raise RuntimeError(
                f"order_placement returned no order id for equity {transaction_type} {trading_symbol} "
                "- check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id), "is_amo": is_amo}

    def place_equity_stop_loss_limit_order(
        self, trading_symbol: str, quantity: int, transaction_type: str,
        trigger_price: float, limit_price: float,
        tag: Optional[str] = None, product_type: str = "CNC",
    ) -> dict:
        """Equity-segment counterpart to place_stop_loss_limit_order below -
        same STOPLIMIT mechanics and tick-rounding, but exchange="NSE" and
        looked up via _equity_instrument_meta (not _instrument_meta, which
        isn't safe for plain equity symbols - see that function's own
        docstring). See place_stop_loss_limit_order's docstring for why
        SL-L (not SL-M) and for the tick-rounding rationale in full."""
        try:
            tick_size = self._equity_instrument_meta(trading_symbol).get("tick_size")
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not look up the real tick size before placing the equity SL-L order - "
                "falling back to plain 2-decimal rounding, which may still get rejected for a "
                "tick-size mismatch", trading_symbol,
            )
            tick_size = None
        rounded_trigger = _round_to_tick(trigger_price, tick_size)
        rounded_limit = _round_to_tick(limit_price, tick_size)
        if rounded_trigger != trigger_price or rounded_limit != limit_price:
            logger.info(
                "%s: rounded equity SL-L prices to the real tick size (%.4f): trigger %.4f->%.4f, limit %.4f->%.4f",
                trading_symbol, tick_size or 0.0, trigger_price, rounded_trigger, limit_price, rounded_limit,
            )
        trigger_price, limit_price = rounded_trigger, rounded_limit
        logger.info(
            "Placing equity STOP-LOSS LIMIT order: %s %s x%s trigger=%.2f limit=%.2f (product=%s)",
            transaction_type, trading_symbol, quantity, trigger_price, limit_price, product_type,
        )
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange="NSE",
            quantity=quantity,
            price=limit_price,
            trigger_price=trigger_price,
            order_type="STOPLIMIT",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=False,
            tag=tag,
        )
        if not order_id:
            raise RuntimeError(
                f"order_placement returned no order id for equity STOPLIMIT {transaction_type} "
                f"{trading_symbol} - check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id)}

    def place_mcx_market_order(
        self, trading_symbol: str, quantity: int, transaction_type: str,
        tag: Optional[str] = None, product_type: str = "MARGIN",
    ) -> dict:
        """MCX-segment counterpart to place_market_order above (added 12
        Sep 2026, Swing v2's Copper options support) - NOT a reuse of that
        function, same reasoning as place_equity_market_order: place_
        market_order hardcodes exchange=config.DEFAULT_EXCHANGE ("NFO"),
        wrong for MCX. "MCX" confirmed as the correct exchange string via
        Tradehull's own internal script_exchange mapping (script_exchange
        = {"NSE":..., "NFO":..., "MCX": self.Dhan.MCX}, appears repeatedly
        throughout Dhan_Tradehull.py) - same "short exchange code" pairing
        already proven by "NSE"/"NFO" and "NSE"/"NSE_EQ" elsewhere in this
        file.

        `quantity` here is Dhan's MCX "number of lots" convention (1 = one
        full real-world lot), NOT the same thing as the real per-lot
        rupee/kg exposure - verified via a live margin-calculator spike
        (quantity=1 priced a real ~2,500kg Copper lot's actual margin;
        quantity=2500 priced an absurd ~76 crore). Callers must pass
        Swing/position_store.py's Position.quantity here (order-placement
        quantity), never Position.pnl_multiplier (P&L-math quantity) -
        see Position's own docstring for why these are two different
        numbers for an MCX position."""
        is_amo = not self.is_market_open(exchange_segment="MCX_COMM")
        logger.info("Placing MCX %s order: %s x%s (product=%s)%s", transaction_type, trading_symbol,
                    quantity, product_type, " (AMO)" if is_amo else "")
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange="MCX",
            quantity=quantity,
            price=0,
            trigger_price=0,
            order_type="MARKET",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=is_amo,
            amo_time="OPEN",
            tag=tag,
        )
        if not order_id:
            raise RuntimeError(
                f"order_placement returned no order id for MCX {transaction_type} {trading_symbol} "
                "- check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id), "is_amo": is_amo}

    def place_mcx_stop_loss_limit_order(
        self, trading_symbol: str, quantity: int, transaction_type: str,
        trigger_price: float, limit_price: float,
        tag: Optional[str] = None, product_type: str = "MARGIN",
    ) -> dict:
        """MCX-segment counterpart to place_stop_loss_limit_order below -
        same STOPLIMIT mechanics and tick-rounding, but exchange="MCX" and
        looked up via the now-MCX-capable _instrument_meta. NOT wired into
        Swing's live entry path yet (Swing's BROKER_STOP_LOSS_ENABLED
        stays off for Copper in this first rollout - see Swing/config.py)
        - built now so it exists once that's turned on. tick_size's /100
        paise convention is unverified for an MCX row specifically (see
        _instrument_meta's own docstring) - confirm against a real MCX
        tick before relying on this in production."""
        try:
            tick_size = self._instrument_meta(trading_symbol, expected_exchange="MCX").get("tick_size")
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not look up the real tick size before placing the MCX SL-L order - "
                "falling back to plain 2-decimal rounding, which may still get rejected for a "
                "tick-size mismatch", trading_symbol,
            )
            tick_size = None
        rounded_trigger = _round_to_tick(trigger_price, tick_size)
        rounded_limit = _round_to_tick(limit_price, tick_size)
        if rounded_trigger != trigger_price or rounded_limit != limit_price:
            logger.info(
                "%s: rounded MCX SL-L prices to the real tick size (%.4f): trigger %.4f->%.4f, limit %.4f->%.4f",
                trading_symbol, tick_size or 0.0, trigger_price, rounded_trigger, limit_price, rounded_limit,
            )
        trigger_price, limit_price = rounded_trigger, rounded_limit
        logger.info(
            "Placing MCX STOP-LOSS LIMIT order: %s %s x%s trigger=%.2f limit=%.2f (product=%s)",
            transaction_type, trading_symbol, quantity, trigger_price, limit_price, product_type,
        )
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange="MCX",
            quantity=quantity,
            price=limit_price,
            trigger_price=trigger_price,
            order_type="STOPLIMIT",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=False,
            tag=tag,
        )
        if not order_id:
            raise RuntimeError(
                f"order_placement returned no order id for MCX STOPLIMIT {transaction_type} "
                f"{trading_symbol} - check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id)}

    def place_stop_loss_market_order(
        self, trading_symbol: str, quantity: int, transaction_type: str, trigger_price: float,
        tag: Optional[str] = None, product_type: Optional[str] = None,
    ) -> dict:
        """Places a real STOP-LOSS MARKET (SL-M) order at the BROKER -
        added 8 Sep 2026 (user request: "broker-side stop order that fires
        instantly regardless of polling interval", Luxury's own real-money
        MAX_LOSS protection). Unlike place_market_order above, this order
        sits INACTIVE at the exchange until the LTP actually trades through
        `trigger_price`, at which point Dhan/NSE itself converts it to a
        market order and fills it - the exchange's own matching engine
        enforces this, not our own poll/tick-driven _check_one_position/
        on_price_tick, so it fires even if this process is slow, briefly
        disconnected, or simply hasn't had a tick land yet.

        `trigger_price` MUST be below the current LTP for a SELL (exiting
        a long CE/PE) - Dhan/NSE will reject an SL-M whose trigger is on
        the wrong side of the current price. `price=0` since "M" (market)
        means no limit price is needed once triggered - matches
        place_market_order's own price=0 convention.

        Does NOT set after_market_order - this is only ever placed
        immediately after a real intraday fill during market hours (never
        pre-market), unlike place_market_order which can legitimately run
        outside market hours for an AMO entry.

        CONFIRMED UNUSABLE for OPTIONS (9 Sep 2026) - NSE discontinued
        SL-M orders for index/stock OPTIONS exchange-wide back in Sep
        2021 (a freak-trade protection measure), across every NSE-
        registered broker, not just Dhan. Dhan's API doesn't surface a
        clean rejection for this - it silently lets the order through as
        something that behaves like an immediately-marketable LIMIT sell,
        confirmed via real live orders and a controlled live test (see
        NOTES.md entry #99 / trading-skills' incidents/2026-09-09-luxury-
        sl-m-orders-fill-as-limit.md). Still valid for FUTURES and EQUITY
        (the exchange ban is options-only) - kept here for that use (see
        Swing's own futures broker-stop-loss work). For OPTIONS, use
        place_stop_loss_limit_order below instead - the only broker-side
        conditional stop NSE still permits for that segment."""
        product_type = product_type or config.OPTIONS_PRODUCT
        logger.info(
            "Placing STOP-LOSS MARKET order: %s %s x%s trigger=%.2f (product=%s)",
            transaction_type, trading_symbol, quantity, trigger_price, product_type,
        )
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange=config.DEFAULT_EXCHANGE,
            quantity=quantity,
            price=0,
            trigger_price=trigger_price,
            order_type="STOPMARKET",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=False,
            tag=tag,
        )
        if not order_id:
            raise RuntimeError(
                f"order_placement returned no order id for STOPMARKET {transaction_type} {trading_symbol} "
                "- check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id)}

    def place_stop_loss_limit_order(
        self, trading_symbol: str, quantity: int, transaction_type: str,
        trigger_price: float, limit_price: float,
        tag: Optional[str] = None, product_type: Optional[str] = None,
    ) -> dict:
        """Places a real STOP-LOSS LIMIT (SL-L) order at the BROKER -
        added 9 Sep 2026, replacing place_stop_loss_market_order above for
        OPTIONS specifically, once that was confirmed unusable there (see
        its own docstring). This is the ONLY broker-side conditional stop
        NSE still permits for index/stock options - SL-M was banned
        exchange-wide for that segment in Sep 2021 specifically to stop
        "freak trade" exploitation of stop orders sitting in thin option
        order books (a real cited case: a Nifty CE premium spiking
        Rs.80->Rs.800 in one second wiped out every resting SL-M below
        it). SL-L exists exactly to bound that risk: TWO prices instead of
        one - `trigger_price` (where the order activates, same as SL-M)
        and `limit_price` (the WORST price you're willing to accept once
        triggered). The order sits inactive until LTP trades through
        `trigger_price`, then a plain LIMIT SELL at `limit_price`-or-
        better goes to the exchange - it can only ever fill AT or ABOVE
        `limit_price`, never below it, no matter how far price keeps
        falling.

        The real tradeoff, and why this is not a free upgrade over SL-M:
        if price gaps straight through `limit_price` before the order
        fills, it can sit UNFILLED while price keeps falling - the exact
        freak-trade protection working as intended, but it means a
        genuinely violent gap can leave a position open past its intended
        cap (where a working SL-M, if the exchange allowed one, would
        always find SOME fill price). Dhan's own guidance is a wider
        trigger-to-limit gap for illiquid names, tighter for liquid ones.
        `limit_price` here is `trigger_price` minus a configurable gap -
        the caller's choice, not fixed here (see each package's own
        config.BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE for the gap actually
        used by trading_engine.py's real callers: sized in rupees off the
        same MAX_LOSS_HIT cap used for trigger_price itself, not a flat %
        of price).

        Also unlike SL-M's `price=0` convention, SL-L REQUIRES a non-zero
        `price` (the limit) - Dhan's own v1 docs list `price` as
        "required" for STOP_LOSS specifically (only `trigger_price` is
        merely "conditionally required" the way it is for SL-M).

        Both `trigger_price` and `limit_price` are rounded to this
        contract's own real exchange tick size before submission (added 9
        Sep 2026, after a controlled live test's computed trigger/limit
        values got cleanly REJECTED with "EXCH:16283: The order price is
        not multiple of the tick size" - see _round_to_tick's own
        docstring for why this is a real, not theoretical, risk for
        COMPUTED prices specifically).

        Does NOT set after_market_order - same reasoning as
        place_stop_loss_market_order above (only ever placed immediately
        after a real intraday fill)."""
        product_type = product_type or config.OPTIONS_PRODUCT
        try:
            tick_size = self._instrument_meta(trading_symbol, expected_exchange="NSE").get("tick_size")
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not look up the real tick size before placing the SL-L order - falling "
                "back to plain 2-decimal rounding, which may still get rejected for a tick-size "
                "mismatch", trading_symbol,
            )
            tick_size = None
        rounded_trigger = _round_to_tick(trigger_price, tick_size)
        rounded_limit = _round_to_tick(limit_price, tick_size)
        if rounded_trigger != trigger_price or rounded_limit != limit_price:
            logger.info(
                "%s: rounded SL-L prices to the real tick size (%.4f): trigger %.4f->%.4f, limit %.4f->%.4f",
                trading_symbol, tick_size or 0.0, trigger_price, rounded_trigger, limit_price, rounded_limit,
            )
        trigger_price, limit_price = rounded_trigger, rounded_limit
        logger.info(
            "Placing STOP-LOSS LIMIT order: %s %s x%s trigger=%.2f limit=%.2f (product=%s)",
            transaction_type, trading_symbol, quantity, trigger_price, limit_price, product_type,
        )
        order_id = self.client.order_placement(
            tradingsymbol=trading_symbol,
            exchange=config.DEFAULT_EXCHANGE,
            quantity=quantity,
            price=limit_price,
            trigger_price=trigger_price,
            order_type="STOPLIMIT",
            transaction_type=transaction_type,
            trade_type=product_type,
            after_market_order=False,
            tag=tag,
        )
        if not order_id:
            raise RuntimeError(
                f"order_placement returned no order id for STOPLIMIT {transaction_type} {trading_symbol} "
                "- check Tradehull's console/log output for the underlying error."
            )
        return {"order_id": str(order_id)}

    def check_if_order_filled(self, order_id: str) -> Optional[OrderResult]:
        """Cheap, non-blocking check for whether `order_id` has ALREADY
        reached a terminal status - added 8 Sep 2026 alongside the broker-
        side stop-loss order above, so a symbol's own monitor tick can
        check "did my resting stop-loss order already fire?" on every
        tick without burning a REST call each time. Reads ONLY the
        WebSocket order-update cache (_order_snapshot_from_cache, already
        populated live by order_update_feed - see that property's own
        docstring) - returns None immediately if nothing has arrived for
        this order yet (still resting, unfired). Only once the cache
        itself shows a terminal status does this make the ONE authoritative
        REST call (refresh_order_status) to get the real fill price/
        quantity - the cache's own price/quantity fields are NOT
        trustworthy (see wait_for_order_result's own docstring for the
        live bug this exact pattern already fixed once), only its STATUS
        field is."""
        cached = self._order_snapshot_from_cache(order_id)
        if not cached or cached["order_status"] not in OrderStatus.TERMINAL_STATUSES:
            return None
        return self.refresh_order_status(order_id)

    def wait_for_order_result(
        self, order_id: str, is_amo: bool = False, retries: int = 6, delay: float = 1.0
    ) -> OrderResult:
        """Polls (WebSocket order-updates cache first, then REST
        get_order_by_id) until the order reaches a terminal status - see
        OrderStatus.TERMINAL_STATUSES - or retries are exhausted. Market
        orders on FNO settle almost immediately during market hours, so a
        terminal status is expected well within the default retry budget.

        For an AMO order, breaks out immediately instead of burning the
        whole retry budget - we already know (from is_amo) that it won't
        resolve until the next session dispatches it.

        BUG FOUND + FIXED 1 Sep 2026 (Swing's own first-ever live entry,
        APLAPOLLO): a terminal-status WS push was trusted directly,
        including its own `average_fill_price` - but
        `_order_snapshot_from_cache`'s own docstring already flagged that
        the WS payload's field names are UNDOCUMENTED/unverified
        (dhanhq's own source only confirms `orderNo`/`status` exist on
        it). Confirmed live: a genuine TRADED push carried no usable
        price field at all, silently defaulting to 0 and recording a
        REAL futures position's entry_price as ₹0 - the broker's own
        REST record showed the correct fill (₹2263.30) the whole time,
        confirmed via a direct, read-only `get_order_by_id` check. The
        WS cache is still used to quickly DETECT that an order has
        reached a terminal state (avoids waiting out the full retry
        delay) - but once it has, the actual DATA (price, filled
        quantity) always comes from the authoritative REST call, never
        the cache's own unverified fields. One extra REST call per
        order is a negligible cost for correctness on something this
        important - every real-money package (Options/Futures/Luxury/
        Swing) shares this function, so this was a live risk for all of
        them, not just Swing.

        Always returns whatever the last-seen status was; callers MUST
        check `.status`/`.is_queued_amo` rather than assuming the order
        filled just because this returned."""
        snapshot: dict = {}
        for attempt in range(retries):
            cached = self._order_snapshot_from_cache(order_id)
            if cached and cached["order_status"] in OrderStatus.TERMINAL_STATUSES:
                self.stats["order_status_cache_hits"] += 1
                # Terminal STATUS from the cache is trustworthy (dhanhq's
                # own source confirms status/orderNo are real fields) -
                # but the cache's own price/quantity fields are NOT (see
                # this function's own docstring for the live bug this
                # fixed) - always get those from the authoritative REST
                # snapshot instead of the cache's own copy.
                rest_snapshot = self._order_snapshot_from_rest(order_id)
                snapshot = rest_snapshot if rest_snapshot["order_status"] in OrderStatus.TERMINAL_STATUSES else cached
                break
            self.stats["order_status_rest_calls"] += 1
            snapshot = self._order_snapshot_from_rest(order_id)
            if snapshot["order_status"] in OrderStatus.TERMINAL_STATUSES:
                break
            if is_amo:
                break
            time.sleep(delay)
        else:
            logger.warning(
                "Order %s still not in a terminal status after %s retries (last status=%s)",
                order_id, retries, snapshot.get("order_status"),
            )

        return self._order_result_from_snapshot(order_id, snapshot, is_amo)

    def refresh_order_status(self, order_id: str, is_amo: bool = False) -> OrderResult:
        """One-shot REST status check (no retry loop) - for periodically
        re-syncing an order whose fate was still pending last time it was
        checked, e.g. a queued AMO order awaiting the next session."""
        snapshot = self._order_snapshot_from_rest(order_id)
        return self._order_result_from_snapshot(order_id, snapshot, is_amo)


dhan_wrapper = DhanWrapper()
