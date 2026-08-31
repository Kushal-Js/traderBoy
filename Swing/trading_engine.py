"""
Core strategy logic for the Swing package - user request 31 Aug 2026.

  - enter_basket_for_stock(): the all-or-nothing basket placement - buys
    1 lot of the underlying's nearest-expiry FUTURES contract, then 1 lot
    of its ATM PE option, as two independent real orders (Dhan has no
    native basket-order API - see the separate trading-skills repo's
    `basket-order-feasibility.md` for the full investigation this design
    is based on). "All-or-nothing" ("do both of these or neither" - the
    user's own framing) is enforced at the APPLICATION level via a
    compensating rollback, since no broker call gives real atomicity
    across two different instruments:
      - futures leg fails -> the PE leg is never attempted at all
        ("neither").
      - futures leg succeeds but the PE leg then fails -> the futures leg
        is immediately SOLD to unwind it ("neither", via a best-effort
        compensating transaction - the standard "saga" pattern for
        exactly this situation).
    A failed unwind is logged as an ERROR, loudly - it means a real,
    un-hedged futures position may still be open and needs manual
    handling; this code never pretends otherwise.
  - reconcile_broker_positions(): pairs up any FUTSTK + OPTSTK broker
    positions attributed to "Swing" (via
    trade_history.attribute_open_broker_position - same mechanism
    Options/Futures/Luxury already use to avoid double-tracking each
    other's real positions) back into a Basket at startup. Especially
    important here since Swing baskets are meant to carry across
    restarts/days by design (no EOD square-off, unlike every other
    package) - a restart must never lose track of a real, still-open
    basket. A leg found with no matching counterpart (one side attributed
    to Swing, the other missing) is logged as a clear warning and left
    alone rather than guessed at - same never-guess philosophy
    attribute_open_broker_position itself follows.
  - Entry/exit signal (added 31 Aug 2026, user request, same day as this
    package's own creation; ENTRY extended with a gap-up gate later the
    same day) - a daily gap check plus a dual-timeframe Supertrend
    crossover on the underlying's own STOCK price:
      ENTRY: today's open is greater than yesterday's close (checked via
      `_is_gap_up()`, at most once per symbol per trading day - see its
      own docstring for why this is cached differently from the
      Supertrend checks below), AND the 5-min close crosses ABOVE the
      5-min Supertrend, AND the 1-min close is above (or has itself just
      crossed above) the 1-min Supertrend - the two Supertrend conditions
      each read on their own most recently fully-closed candle (user's
      own wording: "Todays Open price is greater than yesterday's close
      price and when 5 min close cross above super trend with 1 min
      close greater than or crossed above 1 min super trend").
      EXIT: the 5-min close crosses BELOW the 5-min Supertrend ("5 min
      close price cross below super trend" - unchanged since first
      defined).
    `_fetch_supertrend_state()` is a SELF-CONTAINED dual-timeframe
    crossover detector, deliberately NOT built on top of
    Options/dhan_client.py's own single-timeframe Supertrend cache/
    refresh mechanism (`refresh_supertrend_signal`/
    `get_cached_supertrend_bearish`) - that cache is keyed by underlying
    only (one timeframe's state per symbol) and is already live,
    real-money exit protection for Options/Futures/Luxury; extending it
    to carry a second timeframe risked that shared, already-relied-upon
    path for no good reason. This file's own cache
    (`_supertrend_state_cache`) is entirely independent - reuses the same
    PURE `_compute_supertrend` function and the same
    `intraday_minute_data` REST call shape, just parameterized by
    interval and keeping the last TWO closed candles (not just one) so
    an actual crossover (a state CHANGE between consecutive candles) can
    be detected, not merely a current side. `_is_gap_up()` similarly
    reuses `Options/dhan_client.py`'s new `get_today_open_and_prev_close()`
    (the same OHLC quote `get_day_change_pct()` already fetches, just the
    two raw values instead of a derived %) rather than a new REST call
    shape.
    `_evaluate_watchlist_entry_signal()`/`_evaluate_basket_exit_signal()`
    apply the rules above; `monitor_loop()` calls them every tick
    once `config.STRATEGY_ENABLED` is on, entering/exiting exactly as
    described - a successful auto-entry also removes the symbol from the
    watchlist (no reason to keep evaluating a stock for entry once it has
    a live basket).

Participates in cross_strategy_registry.py the same way Options/Futures/
Luxury already do (claims the underlying for the full duration of a
basket entry attempt) - Swing places real orders on the same shared Dhan
account, so it's exposed to the exact same cross-strategy race that
registry exists to close.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from trade_history import attribute_open_broker_position
import cross_strategy_registry

from . import config
from .dhan_client import OrderStatus, _compute_supertrend, _retry, dhan_wrapper
from .position_store import Basket, Leg, basket_store
from .watchlist import watchlist_store

logger = logging.getLogger("swing_trading_engine")

IST = ZoneInfo(config.MARKET_TZ)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _gen_tag(prefix: str, symbol: str) -> str:
    """See Options/trading_engine.py's identical helper - same DH-905
    special-character rationale (GVT&D)."""
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol)
    suffix = "".join(random.choices(string.digits, k=6))
    return f"{prefix}-{safe_symbol[:6]}-{suffix}"[:25]


# --------------------------------------------------------------------------- #
# Order placement helper - shared by both legs and by the unwind/exit SELLs
# --------------------------------------------------------------------------- #
async def _place_leg(
    trading_symbol: str, quantity: int, transaction_type: str, product_type: str,
    tag_prefix: str, symbol: str,
) -> dict:
    """Places ONE real order and waits for its result. Never raises - the
    caller decides what a failure means for the basket as a whole (abort
    with no PE leg attempted, unwind an already-filled futures leg,
    etc.)."""
    loop = asyncio.get_running_loop()
    tag = _gen_tag(tag_prefix, symbol)
    try:
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order, trading_symbol, quantity, transaction_type, tag, product_type,
        )
        order_id = order_resp["order_id"]
        is_amo = order_resp["is_amo"]
        result = await loop.run_in_executor(None, dhan_wrapper.wait_for_order_result, order_id, is_amo)
        ok = result.status not in OrderStatus.REJECTED_STATUSES and result.status != OrderStatus.CANCELLED
        return {
            "ok": ok, "order_id": order_id, "status": result.status, "remark": result.remark,
            "fill_price": result.fill_price, "is_queued_amo": result.is_queued_amo,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s: %s order failed for %s", symbol, transaction_type, trading_symbol)
        return {"ok": False, "error": str(exc)}


async def _unwind_futures_leg(symbol: str, trading_symbol: str, quantity: int) -> dict:
    """Best-effort compensating SELL to close a futures leg that filled
    but must not survive alone (the PE hedge leg failed, or couldn't even
    be looked up). Never raises - logs loudly on failure since the caller
    has already decided the basket entry failed either way; this is
    cleanup, not something that can un-fail the outer operation. A failed
    unwind leaves a REAL, un-hedged futures position the user must handle
    manually - always logged as an ERROR, never silently swallowed."""
    result = await _place_leg(trading_symbol, quantity, "SELL", config.FUTURES_PRODUCT, "Unwind", symbol)
    if result["ok"]:
        logger.info("%s: futures leg unwound successfully (SELL %s filled)", symbol, trading_symbol)
    else:
        logger.error(
            "%s: COULD NOT UNWIND the futures leg (%s) after the PE leg failed - a REAL, "
            "un-hedged futures position may still be open at the broker. Manual intervention "
            "required. Detail: %s", symbol, trading_symbol, result,
        )
    return result


# --------------------------------------------------------------------------- #
# Basket entry - the all-or-nothing guarantee
# --------------------------------------------------------------------------- #
async def enter_basket_for_stock(symbol: str) -> dict:
    """The all-or-nothing basket entry - see this module's own docstring
    for the compensating-rollback design ("do both of these or
    neither")."""
    if not config.STRATEGY_ENABLED:
        return {"symbol": symbol, "status": "ignored", "reason": "strategy_disabled"}

    loop = asyncio.get_running_loop()

    # Cross-strategy claim held for this ENTIRE function - Options/
    # Futures/Luxury all place real orders on this same Dhan account, so
    # Swing is exposed to the exact same race cross_strategy_registry
    # exists to close (see its own module docstring).
    if not await cross_strategy_registry.try_claim(symbol, "Swing"):
        logger.info("%s: skipped - another strategy is currently entering it", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "claimed_by_another_strategy"}

    try:
        if not await basket_store.reserve_symbol(symbol):
            logger.info("%s: skipped - already open/in-flight, or no basket capacity", symbol)
            return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

        try:
            # Belt-and-suspenders: confirm the broker doesn't already show
            # an open FNO position for this underlying (a manual trade, a
            # position from before this logging, or state this process
            # hasn't reconciled yet) - our own reservation above only
            # guards duplicates within this process's in-memory state.
            already_open = await loop.run_in_executor(
                None, dhan_wrapper.has_open_position_for_underlying, symbol
            )
            if already_open:
                logger.warning("%s: skipped - broker already shows an open FNO position for it", symbol)
                await basket_store.release_symbol(symbol)
                return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker"}

            # --- Leg 1: futures contract ---
            try:
                fut = await loop.run_in_executor(None, dhan_wrapper.get_futures_contract, symbol)
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s: could not resolve futures contract - basket entry aborted", symbol)
                await basket_store.release_symbol(symbol)
                return {"symbol": symbol, "status": "error", "reason": f"futures_lookup_failed: {exc}"}

            fut_qty = fut.lot_size * config.QUANTITY_LOTS
            futures_result = await _place_leg(
                fut.trading_symbol, fut_qty, "BUY", config.FUTURES_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
            )
            if not futures_result["ok"]:
                logger.warning(
                    "%s: futures leg failed (%s) - basket entry aborted, PE leg never attempted (neither)",
                    symbol, futures_result.get("remark") or futures_result.get("error"),
                )
                await basket_store.release_symbol(symbol)
                return {
                    "symbol": symbol, "status": "rejected", "reason": "futures_leg_failed",
                    "detail": futures_result,
                }

            futures_fill_price = futures_result.get("fill_price") or 0.0

            # --- Leg 2: ATM PE option (the hedge) ---
            try:
                atm = await loop.run_in_executor(None, dhan_wrapper.get_atm_option, symbol, "PE")
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s: could not resolve ATM PE option - unwinding the futures leg", symbol)
                await _unwind_futures_leg(symbol, fut.trading_symbol, fut_qty)
                await basket_store.release_symbol(symbol)
                return {
                    "symbol": symbol, "status": "error",
                    "reason": f"pe_lookup_failed: {exc} - futures leg unwound",
                }

            option_qty = atm.lot_size * config.QUANTITY_LOTS
            option_result = await _place_leg(
                atm.trading_symbol, option_qty, "BUY", config.OPTIONS_PRODUCT, config.ORDER_TAG_PREFIX, symbol,
            )
            if not option_result["ok"]:
                logger.warning(
                    "%s: PE leg failed (%s) - unwinding the already-filled futures leg (neither)",
                    symbol, option_result.get("remark") or option_result.get("error"),
                )
                await _unwind_futures_leg(symbol, fut.trading_symbol, fut_qty)
                await basket_store.release_symbol(symbol)
                return {
                    "symbol": symbol, "status": "rejected", "reason": "pe_leg_failed_futures_unwound",
                    "detail": option_result,
                }

            option_fill_price = option_result.get("fill_price") or 0.0

            futures_leg = Leg(
                underlying_symbol=symbol, option_trading_symbol=fut.trading_symbol, option_type="FUT",
                quantity=fut_qty, lot_size=fut.lot_size, entry_price=futures_fill_price,
                order_id=futures_result["order_id"], product_type=config.FUTURES_PRODUCT,
                security_id=fut.security_id,
            )
            option_leg = Leg(
                underlying_symbol=symbol, option_trading_symbol=atm.trading_symbol, option_type="PE",
                quantity=option_qty, lot_size=atm.lot_size, entry_price=option_fill_price,
                order_id=option_result["order_id"], product_type=config.OPTIONS_PRODUCT,
                security_id=atm.security_id,
            )
            basket = Basket(underlying_symbol=symbol, futures_leg=futures_leg, option_leg=option_leg)
            await basket_store.add_basket(basket)

            logger.info(
                "%s: basket ENTERED - futures %s@%.2f, PE %s@%.2f",
                symbol, fut.trading_symbol, futures_fill_price, atm.trading_symbol, option_fill_price,
            )
            return {
                "symbol": symbol, "status": "entered",
                "futures_leg": {"trading_symbol": fut.trading_symbol, "entry_price": futures_fill_price, "quantity": fut_qty},
                "option_leg": {"trading_symbol": atm.trading_symbol, "entry_price": option_fill_price, "quantity": option_qty},
            }
        except Exception as exc:  # noqa: BLE001
            await basket_store.release_symbol(symbol)
            logger.exception("%s: unexpected error entering basket", symbol)
            return {"symbol": symbol, "status": "error", "reason": str(exc)}
    finally:
        await cross_strategy_registry.release_claim(symbol, "Swing")


# --------------------------------------------------------------------------- #
# Basket exit
# --------------------------------------------------------------------------- #
async def _exit_basket(symbol: str, basket: Basket, reason: str) -> None:
    """Closes BOTH legs of an already-open basket. Unlike entry, exit
    doesn't need the same all-or-nothing rollback in the same sense - a
    basket that's already live just needs both legs closed; if one SELL
    fails, the other leg is still attempted rather than aborting, since a
    stuck leg here costs nothing structurally the way a failed ENTRY
    would (see enter_basket_for_stock). Every failure is still logged
    loudly so a stuck leg is visible, never silently lost."""
    futures_exit = await _place_leg(
        basket.futures_leg.option_trading_symbol, basket.futures_leg.quantity, "SELL",
        basket.futures_leg.product_type, "Ext", symbol,
    )
    option_exit = await _place_leg(
        basket.option_leg.option_trading_symbol, basket.option_leg.quantity, "SELL",
        basket.option_leg.product_type, "Ext", symbol,
    )
    futures_exit_price = futures_exit.get("fill_price") or basket.futures_leg.entry_price
    option_exit_price = option_exit.get("fill_price") or basket.option_leg.entry_price

    if not futures_exit["ok"]:
        logger.error(
            "%s: futures leg SELL failed during basket exit (%s) - may still be open, check manually",
            symbol, futures_exit.get("remark") or futures_exit.get("error"),
        )
    if not option_exit["ok"]:
        logger.error(
            "%s: PE leg SELL failed during basket exit (%s) - may still be open, check manually",
            symbol, option_exit.get("remark") or option_exit.get("error"),
        )

    await basket_store.close_basket(symbol, futures_exit_price, option_exit_price, reason)


async def _square_off_all(reason: str) -> None:
    baskets = dict(basket_store.live_baskets)
    if not baskets:
        return
    logger.info("Swing square-off triggered (%s) for %d open basket(s)", reason, len(baskets))
    for symbol, basket in baskets.items():
        await _exit_basket(symbol, basket, reason)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Basket]:
    """Pairs up any FUTSTK + OPTSTK broker positions attributed to
    "Swing" back into Baskets at startup - see this module's own
    docstring for why this matters more here than for any other package
    (no EOD square-off, so a basket is meant to survive a restart)."""
    loop = asyncio.get_running_loop()
    broker_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    futures_by_underlying: dict[str, dict] = {}
    options_by_underlying: dict[str, dict] = {}
    for bp in broker_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning(
                "Skipping reconciliation for %s - broker reported no average price.",
                bp["trading_symbol"],
            )
            continue

        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Swing":
            logger.warning(
                "Skipping reconciliation for %s - attributed to %s (not Swing) by our own "
                "opened-position history. Real broker position is unaffected; this process just "
                "won't manage it. If this is wrong (e.g. a manually-placed position, or one that "
                "predates this logging), it needs manual handling.",
                bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        # FUTSTK rows don't carry a meaningful option_type - distinguish
        # the leg by trading_symbol shape instead (Dhan's own futures
        # SEM_CUSTOM_SYMBOL format always ends "... FUT").
        if bp["trading_symbol"].endswith("FUT"):
            futures_by_underlying[bp["underlying_symbol"]] = bp
        else:
            options_by_underlying[bp["underlying_symbol"]] = bp

    baskets: list[Basket] = []
    for symbol in set(futures_by_underlying) | set(options_by_underlying):
        fut_bp = futures_by_underlying.get(symbol)
        opt_bp = options_by_underlying.get(symbol)
        if fut_bp and opt_bp:
            futures_leg = Leg(
                underlying_symbol=symbol, option_trading_symbol=fut_bp["trading_symbol"], option_type="FUT",
                quantity=fut_bp["quantity"], lot_size=fut_bp["lot_size"], entry_price=fut_bp["avg_price"],
                order_id="", product_type=config.FUTURES_PRODUCT, reconciled=True,
            )
            option_leg = Leg(
                underlying_symbol=symbol, option_trading_symbol=opt_bp["trading_symbol"], option_type="PE",
                quantity=opt_bp["quantity"], lot_size=opt_bp["lot_size"], entry_price=opt_bp["avg_price"],
                order_id="", product_type=config.OPTIONS_PRODUCT, reconciled=True,
            )
            baskets.append(Basket(underlying_symbol=symbol, futures_leg=futures_leg, option_leg=option_leg))
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, fut_bp["trading_symbol"])
            await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, opt_bp["trading_symbol"])
        else:
            found, missing = ("futures", "PE option") if fut_bp else ("PE option", "futures")
            logger.warning(
                "%s: found a Swing-attributed %s leg at the broker with NO matching %s leg - "
                "this basket is UNHEDGED/incomplete. Not auto-reconciling a partial basket - "
                "needs manual review.", symbol, found, missing,
            )

    return baskets


# --------------------------------------------------------------------------- #
# Dual-timeframe Supertrend crossover signal (user request 31 Aug 2026)
# --------------------------------------------------------------------------- #
@dataclass
class SupertrendState:
    """The last TWO fully-closed candles' relationship to the Supertrend
    line for one (symbol, timeframe) - enough to detect an actual
    crossover (a state CHANGE), not just a current side. `is_above`/
    `prev_is_above` are None only if there weren't enough candles yet to
    compute a Supertrend value for that bar (see _fetch_supertrend_state)."""
    candle_start: Optional[datetime]
    close: float
    supertrend: float
    is_above: bool
    prev_close: float
    prev_supertrend: float
    prev_is_above: bool

    @property
    def crossed_above(self) -> bool:
        """True only on the candle where price flips from AT-OR-BELOW to
        ABOVE the Supertrend line - a real transition, not merely "is
        above right now" (which stays true for every candle of an
        established uptrend, not just the crossing one)."""
        return (not self.prev_is_above) and self.is_above

    @property
    def crossed_below(self) -> bool:
        """Mirror of crossed_above, for the downside."""
        return self.prev_is_above and not self.is_above


# (fetched_at, SupertrendState) per (symbol, interval_minutes) - entirely
# separate from Options/dhan_client.py's own single-timeframe
# _supertrend_cache (see this module's own docstring for why). Throttled
# by config.SUPERTREND_REFRESH_SECONDS, same rate-limit-avoidance
# rationale as that other cache.
_supertrend_state_cache: dict[tuple[str, int], tuple[datetime, Optional[SupertrendState]]] = {}


def _fetch_supertrend_state_once(symbol: str, interval_minutes: int) -> Optional[SupertrendState]:
    """Blocking (REST calls) - always call via run_in_executor. Fetches
    today's `interval_minutes` candles for `symbol` and computes the
    Supertrend line (via the shared, pure `_compute_supertrend`), keeping
    the last TWO fully-closed bars so a genuine crossover can be told
    apart from an established trend. Returns None if there isn't enough
    data yet (illiquid symbol, very early in the session, etc.) - callers
    treat that as "no signal", never as a false crossover."""
    security_id = dhan_wrapper._equity_security_id(symbol)
    today = _now_ist().strftime("%Y-%m-%d")
    resp = _retry(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=today, to_date=today, interval=interval_minutes,
    )
    data = resp.get("data") or {}
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    timestamps = data.get("timestamp") or []

    period = config.SUPERTREND_PERIOD
    # Drop a still-forming last candle - only a fully-closed candle's
    # close should ever drive a "crossed" signal (same guard Options/
    # dhan_client.py's own refresh_supertrend_signal uses).
    if timestamps:
        last_candle_start = datetime.fromtimestamp(timestamps[-1], tz=IST)
        if _now_ist() < last_candle_start + timedelta(minutes=interval_minutes):
            highs, lows, closes, timestamps = highs[:-1], lows[:-1], closes[:-1], timestamps[:-1]

    # Need period+1 candles for the FIRST computable Supertrend bar, one
    # more on top of that so there's a PREVIOUS bar to compare against for
    # an actual crossover (period+2 total).
    if len(closes) < period + 2:
        return None

    supertrend = _compute_supertrend(highs, lows, closes, period=period, multiplier=config.SUPERTREND_MULTIPLIER)
    if supertrend[-1] is None or supertrend[-2] is None:
        return None

    return SupertrendState(
        candle_start=datetime.fromtimestamp(timestamps[-1], tz=IST) if timestamps else None,
        close=closes[-1], supertrend=supertrend[-1], is_above=closes[-1] > supertrend[-1],
        prev_close=closes[-2], prev_supertrend=supertrend[-2], prev_is_above=closes[-2] > supertrend[-2],
    )


async def _fetch_supertrend_state(symbol: str, interval_minutes: int) -> Optional[SupertrendState]:
    """Cached, throttled wrapper around _fetch_supertrend_state_once - see
    config.SUPERTREND_REFRESH_SECONDS. Swallows and logs any fetch
    failure (transient Dhan REST hiccup, illiquid symbol, etc.) as "no
    signal" rather than raising into the monitor loop - one symbol's data
    problem must never stop every other symbol's own tick from running."""
    key = (symbol, interval_minutes)
    cached = _supertrend_state_cache.get(key)
    if cached and (_now_ist() - cached[0]).total_seconds() < config.SUPERTREND_REFRESH_SECONDS:
        return cached[1]
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _fetch_supertrend_state_once, symbol, interval_minutes)
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch %d-min Supertrend state", symbol, interval_minutes)
        state = None
    _supertrend_state_cache[key] = (_now_ist(), state)
    return state


# (as_of_date, is_gap_up) per symbol - today's own open never changes
# again once the session has started, so this is fetched at most ONCE PER
# SYMBOL PER TRADING DAY rather than re-checked every monitor tick the
# way the Supertrend signal is (that one can genuinely change candle to
# candle; this one can't). Self-invalidates on a new day simply because
# `cached[0] == today` stops matching - no separate day-reset call needed.
_gap_up_cache: dict[str, tuple[date, bool]] = {}


async def _is_gap_up(symbol: str) -> bool:
    """True if today's open is greater than yesterday's close (user's own
    wording, updated 31 Aug 2026: "Todays Open price is greater than
    yesterday's close price"). Cached per symbol per trading day - see
    this module's own cache comment above for why re-fetching every tick
    would be pointless REST traffic, not just wasteful."""
    today = _now_ist().date()
    cached = _gap_up_cache.get(symbol)
    if cached and cached[0] == today:
        return cached[1]

    loop = asyncio.get_running_loop()
    try:
        today_open, prev_close = await loop.run_in_executor(
            None, dhan_wrapper.get_today_open_and_prev_close, symbol
        )
        is_gap_up = today_open > prev_close
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not fetch today's open/previous close for the gap-up check", symbol)
        return False

    _gap_up_cache[symbol] = (today, is_gap_up)
    if is_gap_up:
        logger.info("%s: gap-up confirmed for today - open %.2f > previous close %.2f",
                    symbol, today_open, prev_close)
    return is_gap_up


async def _evaluate_watchlist_entry_signal(symbol: str) -> bool:
    """ENTRY rule (user's own wording, updated 31 Aug 2026): "Todays Open
    price is greater than yesterday's close price and when 5 min close
    cross above super trend with 1 min close greater than or crossed
    above 1 min super trend." The gap-up check runs FIRST and short-
    circuits the (more REST-expensive, two-timeframe) Supertrend checks
    entirely when it fails - it's also the cheaper, more cacheable check
    (see _is_gap_up's own docstring: at most one REST call per symbol per
    day, vs the Supertrend checks' own SWING_SUPERTREND_REFRESH_SECONDS-
    throttled but still much more frequent refresh).

    The 1-min half of the Supertrend condition is written as two explicit
    checks (`is_above` OR `crossed_above`) even though `crossed_above`
    already implies `is_above` - kept both so this reads as a direct,
    auditable translation of the stated rule rather than a logically-
    equivalent but less traceable shortcut."""
    if not await _is_gap_up(symbol):
        return False

    entry_tf = await _fetch_supertrend_state(symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES)
    if entry_tf is None or not entry_tf.crossed_above:
        return False

    confirm_tf = await _fetch_supertrend_state(symbol, config.SUPERTREND_CONFIRM_TIMEFRAME_MINUTES)
    if confirm_tf is None:
        return False
    confirmed = confirm_tf.is_above or confirm_tf.crossed_above

    if confirmed:
        logger.info(
            "%s: ENTRY signal - gap-up confirmed, %d-min close %.2f crossed above Supertrend %.2f, "
            "%d-min close %.2f %s its Supertrend %.2f",
            symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES, entry_tf.close, entry_tf.supertrend,
            config.SUPERTREND_CONFIRM_TIMEFRAME_MINUTES, confirm_tf.close,
            "crossed above" if confirm_tf.crossed_above else "is above", confirm_tf.supertrend,
        )
    return confirmed


async def _evaluate_basket_exit_signal(symbol: str, basket: Basket) -> Optional[str]:
    """EXIT rule (user's own wording, 31 Aug 2026): "5 min close price
    cross below super trend." Mutually exclusive with the entry rule by
    construction (a single candle can't be both a crossed-above and a
    crossed-below at once), so this can never immediately re-fire on the
    very candle that justified the basket's own entry - no extra
    entry-candle guard needed the way Options/Futures/Luxury's own
    SUPERTREND_EXIT feature requires for its own (differently-shaped)
    check."""
    state = await _fetch_supertrend_state(symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES)
    if state is None or not state.crossed_below:
        return None
    logger.info(
        "%s: EXIT signal - %d-min close %.2f crossed below Supertrend %.2f",
        symbol, config.SUPERTREND_ENTRY_TIMEFRAME_MINUTES, state.close, state.supertrend,
    )
    return "SUPERTREND_5MIN_EXIT"


async def monitor_loop() -> None:
    """Runs forever, ALWAYS - see config.py's own docstring for why this
    keeps running even when config.STRATEGY_ENABLED is False (so no
    restart is needed later to pick up the flag flipping), doing nothing
    at all in that case. Each tick: re-syncs the watchlist from
    data/watchlist (user request 31 Aug 2026 - a hand-edit to that file
    takes effect within one tick, no restart needed, same hot-reload UX
    choppy_stocks.py already established - runs regardless of
    config.STRATEGY_ENABLED, since populating the watchlist is inert on
    its own), then evaluates the entry signal for every watchlist symbol
    (removing it from the watchlist on a successful auto-entry - no
    reason to keep evaluating a stock once it has a live basket) and the
    exit signal for every live basket. Paced (a small sleep between
    watchlist symbols) the same way rank_and_pick_top_stocks() paces its
    own sequential Dhan calls elsewhere in this codebase - an unattended
    loop checking many symbols has no natural per-alert pacing boundary
    the way a webhook-triggered call does, so this provides its own."""
    logger.info("Swing monitor loop started. strategy_enabled=%s", config.STRATEGY_ENABLED)
    while True:
        try:
            await basket_store.maybe_reset_for_new_day()
            await watchlist_store.sync_from_file()
            if config.STRATEGY_ENABLED:
                watchlist_symbols = await watchlist_store.symbols()
                for i, symbol in enumerate(watchlist_symbols):
                    if i > 0:
                        await asyncio.sleep(0.35)
                    if await _evaluate_watchlist_entry_signal(symbol):
                        result = await enter_basket_for_stock(symbol)
                        if result.get("status") == "entered":
                            await watchlist_store.remove_symbol(symbol)
                for symbol, basket in list(basket_store.live_baskets.items()):
                    reason = await _evaluate_basket_exit_signal(symbol, basket)
                    if reason:
                        await _exit_basket(symbol, basket, reason)
        except Exception:  # noqa: BLE001
            logger.exception("Error in Swing monitor loop tick")
        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
