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

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from Dhan_Tradehull import Tradehull
from dhanhq import MarketFeed, OrderUpdate

from . import config

logger = logging.getLogger("dhan_client")

IST = ZoneInfo(config.MARKET_TZ)


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


class DhanWrapper:
    """Lazily-authenticated singleton wrapper around Tradehull + dhanhq's
    WebSocket classes."""

    def __init__(self) -> None:
        self._client: Optional[Tradehull] = None
        self._order_update: Optional[OrderUpdate] = None
        self._market_feed: Optional[MarketFeed] = None
        # order_id (str) -> latest order-update dict pushed over the socket
        self._order_updates: dict[str, dict] = {}
        # security_id (str) -> last LTP pushed over the socket
        self._ltp_cache: dict[str, float] = {}
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
        # underlying_symbol -> (fetched_at, is_bearish, candle_start) - see
        # refresh_supertrend_signal()/get_cached_supertrend_bearish().
        self._supertrend_cache: dict[str, tuple[datetime, bool, Optional[datetime]]] = {}
        # Observability: proves (or disproves) whether the WebSocket caches
        # are actually being used instead of REST, rather than assuming it.
        self.stats = {
            "ltp_cache_hits": 0,
            "ltp_cache_misses": 0,
            "order_status_cache_hits": 0,
            "order_status_rest_calls": 0,
            "price_ticks_received": 0,
        }

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def authenticate(self) -> None:
        mode = config.DHAN_AUTH_MODE
        if mode == "pin_totp":
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
        logger.info("Authenticated with Dhan (Tradehull, mode=%s)", mode)

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
        """Derives the underlying from Dhan's SEM_TRADING_SYMBOL format
        ("{UNDERLYING}-{Mon}{YYYY}-{STRIKE}-{CE|PE}"). A naive split("-")[0]
        breaks for underlyings that themselves contain a hyphen - confirmed
        live: BAJAJ-AUTO's contracts are "BAJAJ-AUTO-Aug2026-10000-CE",
        which split("-")[0] mangles to just "BAJAJ" (not a real symbol, and
        a REST lookup on it fails outright). Strip the last 3 segments
        (expiry, strike, option type) instead of keeping only the first."""
        parts = sem_trading_symbol.split("-")
        if len(parts) < 4:
            return parts[0]
        return "-".join(parts[:-3])

    def _instrument_meta(self, trading_symbol: str) -> dict:
        """Looks up an instrument by trading_symbol string. NOTE: the scrip
        master is not guaranteed unique on SEM_TRADING_SYMBOL (confirmed
        live - two different SBIN option contracts shared the exact same
        SEM_TRADING_SYMBOL with different SEM_SMST_SECURITY_ID). Only use
        this for symbols we just got directly from Tradehull (e.g.
        ATM_Strike_Selection's return) where there's no better key to match
        on; prefer _instrument_meta_by_security_id() whenever a security_id
        is already available (e.g. from a broker position record)."""
        df = self.instruments()
        row = df[
            ((df["SEM_TRADING_SYMBOL"] == trading_symbol) | (df["SEM_CUSTOM_SYMBOL"] == trading_symbol))
            & (df["SEM_EXM_EXCH_ID"] == "NSE")
        ]
        if row.empty:
            raise ValueError(f"No instrument found for trading_symbol {trading_symbol}")
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
        contracts' security_ids."""
        df = self.instruments()
        row = df[
            (df["SEM_EXM_EXCH_ID"] == "NSE")
            & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")
            & (df["SEM_TRADING_SYMBOL"] == underlying_symbol)
        ]
        if row.empty:
            raise ValueError(f"No NSE equity instrument found for {underlying_symbol}")
        return str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))

    # ------------------------------------------------------------------ #
    # Market hours (Dhan requires an explicit afterMarketOrder flag - unlike
    # Groww it does NOT auto-detect AMO from placement time)
    # ------------------------------------------------------------------ #
    def is_market_open(self) -> bool:
        now = datetime.now(IST).time()
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

    @property
    def market_feed(self) -> MarketFeed:
        if self._market_feed is None:
            feed = MarketFeed(self.client.dhan_context, [], version="v2", on_ticks=self._on_market_tick)
            feed.start()  # spawns its own background thread; non-blocking
            self._market_feed = feed
            logger.info("Dhan market-data WebSocket connecting in the background.")
        return self._market_feed

    def add_price_tick_subscriber(self, callback: Callable[[str, float], None]) -> None:
        """Registers a callback to fire on every price tick, as
        (trading_symbol, ltp) - see _on_price_tick_subscribers' docstring
        for why this is additive rather than a single-slot assignment."""
        self._on_price_tick_subscribers.append(callback)

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

        security_id = str(security_id)
        self._ltp_cache[security_id] = ltp_val
        self.stats["price_ticks_received"] += 1

        if self._on_price_tick_subscribers:
            trading_symbol = self._security_id_to_symbol.get(security_id)
            if trading_symbol:
                for callback in self._on_price_tick_subscribers:
                    try:
                        callback(trading_symbol, ltp_val)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_price_tick subscriber failed for %s", trading_symbol)

    def start_feed(self) -> None:
        """Eagerly opens both socket connections (otherwise they lazily open
        on first use). Call once at app startup."""
        if not config.ENABLE_WS_FEED:
            logger.info("WebSocket feed disabled (ENABLE_WS_FEED=false); running REST-only.")
            return
        _ = self.order_update_feed
        _ = self.market_feed

    def subscribe_option_price(self, trading_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        meta = self._instrument_meta(trading_symbol)
        self._security_id_to_symbol[meta["security_id"]] = trading_symbol
        self.market_feed.subscribe_symbols([(MarketFeed.NSE_FNO, meta["security_id"], MarketFeed.Ticker)])

    def unsubscribe_option_price(self, trading_symbol: str) -> None:
        if not config.ENABLE_WS_FEED:
            return
        meta = self._instrument_meta(trading_symbol)
        self._security_id_to_symbol.pop(meta["security_id"], None)
        self.market_feed.unsubscribe_symbols([(MarketFeed.NSE_FNO, meta["security_id"], MarketFeed.Ticker)])

    def get_cached_option_ltp(self, trading_symbol: str) -> Optional[float]:
        """Returns the last price pushed over the WebSocket feed for this
        option, or None if not subscribed yet / no tick has arrived yet /
        the feed is disabled (ENABLE_WS_FEED=false)."""
        if not config.ENABLE_WS_FEED:
            return None
        meta = self._instrument_meta(trading_symbol)
        ltp = self._ltp_cache.get(meta["security_id"])
        if ltp is not None:
            self.stats["ltp_cache_hits"] += 1
        else:
            self.stats["ltp_cache_misses"] += 1
        return ltp

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
            raise ValueError(f"No {option_type} leg found for {underlying_symbol} at strike {strike}")

        meta = self._instrument_meta(trading_symbol)
        return AtmOption(
            trading_symbol=trading_symbol,
            strike=float(strike),
            option_type=option_type,
            lot_size=meta["lot_size"],
            security_id=meta["security_id"],
            expiry_date=meta["expiry_date"],
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

    def get_option_ltp(self, trading_symbol: str) -> float:
        data = self.client.get_ltp_data(names=[trading_symbol])
        ltp = data.get(trading_symbol)
        if ltp is None:
            raise ValueError(f"No LTP returned for {trading_symbol}")
        return float(ltp)

    # ------------------------------------------------------------------ #
    # Supertrend exit signal (computed on the underlying stock, not the
    # option's own premium - see config.ENABLE_SUPERTREND_EXIT)
    # ------------------------------------------------------------------ #
    def refresh_supertrend_signal(self, underlying_symbol: str) -> None:
        """Fetches the underlying's 5-min candles and recomputes whether its
        last fully-closed candle's close is below the 5-min Supertrend - a
        trend-reversal exit signal. Cached (see get_cached_supertrend_bearish)
        and only re-fetched every config.SUPERTREND_REFRESH_SECONDS.

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
            today = datetime.now(IST).strftime("%Y-%m-%d")
            resp = _retry(
                self.client.Dhan.intraday_minute_data,
                security_id=security_id,
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=today,
                to_date=today,
                interval=config.SUPERTREND_INTERVAL_MINUTES,
            )
            data = resp.get("data") or {}
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

            # Fix for the 10:10 warmup-cluster bug (NOTES.md bug #10/#16):
            # gate the signal behind a longer minimum than the bare period
            # needs, independent of any position's own entry+grace gating
            # below. Doesn't fix the seed's inherent bias, just delays when
            # the (still-biased) first value can fire - see
            # config.SUPERTREND_MIN_WARMUP_CANDLES.
            if len(closes) < config.SUPERTREND_MIN_WARMUP_CANDLES:
                logger.info(
                    "Supertrend warmup not complete yet for %s (%d/%d candles) - not caching a signal",
                    underlying_symbol, len(closes), config.SUPERTREND_MIN_WARMUP_CANDLES,
                )
                return

            supertrend = _compute_supertrend(highs, lows, closes, period=period,
                                              multiplier=config.SUPERTREND_MULTIPLIER)
            last_st = supertrend[-1]
            if last_st is None:
                return
            is_bearish = closes[-1] < last_st
            candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None
            self._supertrend_cache[underlying_symbol] = (datetime.now(IST), is_bearish, candle_start)
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

    # ------------------------------------------------------------------ #
    # Portfolio (positions already open at the broker)
    # ------------------------------------------------------------------ #
    def get_open_fno_positions(self) -> list[dict]:
        """Every NSE F&O position currently open at Dhan (net quantity != 0).
        avg_price is Dhan's own reported average buy price for the position
        (buyAvg, falling back to costPrice), not something we computed."""
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
                "avg_price": float(p.get("buyAvg") or p.get("costPrice") or 0),
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

        Always returns whatever the last-seen status was; callers MUST
        check `.status`/`.is_queued_amo` rather than assuming the order
        filled just because this returned."""
        snapshot: dict = {}
        for attempt in range(retries):
            cached = self._order_snapshot_from_cache(order_id)
            if cached and cached["order_status"] in OrderStatus.TERMINAL_STATUSES:
                snapshot = cached
                self.stats["order_status_cache_hits"] += 1
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
