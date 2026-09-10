"""
Core strategy logic.

Flow:
  0. reconcile_broker_positions() - at startup, import positions already
     open at Dhan (e.g. left over from a previous run) so we don't lose
     track of them and re-enter.
  1. rank_and_pick_top_stocks()  - from the Chartink alert's stock list, pick
     the top-N by today's %change (highest first).
  2. enter_positions_for_stocks() - for each qualifying stock (not already
     traded today, capacity available), find the ATM option and place a
     BUY MARKET order (AMO outside market hours).
  3. monitor_loop() - background asyncio loop, polls every
     MONITOR_INTERVAL_SECONDS, and exits a leg when target / stop-loss /
     trailing stop-loss is hit, or force-squares-off everything at
     SQUARE_OFF_TIME. Also re-syncs any order still queued as AMO.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
import time
from datetime import datetime, time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

from trade_history import (
    attribute_open_broker_position, count_opened_today, loss_exit_count_today, minutes_since_last_loss_today,
)
import cross_strategy_registry
import fund_allocation

from . import config
from .dhan_client import OrderStatus, dhan_wrapper
from .position_store import EXIT_CLAIMED, OrderRecord, Position, position_store

logger = logging.getLogger("futures_trading_engine")

IST = ZoneInfo(config.MARKET_TZ)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_hhmm_today(hhmm: str) -> datetime:
    now = _now_ist()
    hour, minute = map(int, hhmm.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _todays_square_off_time() -> Optional[str]:
    """Returns the HH:MM cutoff that applies TODAY, or None if no square-
    off/entry-cutoff applies today at all (the NRML/carry-forward default -
    see NOTES.md's design-decision entry). config.ENABLE_SQUARE_OFF (every
    day, unconditional) takes priority if on. Otherwise, Friday gets its
    own carve-out via config.ENABLE_FRIDAY_SQUARE_OFF/FRIDAY_SQUARE_OFF_TIME
    REGARDLESS of ENABLE_SQUARE_OFF - so a position still can't be carried
    into the weekend even when weekday carry-forward is otherwise enabled.
    datetime.weekday(): Monday=0 ... Friday=4."""
    if config.ENABLE_SQUARE_OFF:
        return config.SQUARE_OFF_TIME
    if config.ENABLE_FRIDAY_SQUARE_OFF and _now_ist().weekday() == 4:
        return config.FRIDAY_SQUARE_OFF_TIME
    return None


def is_past_square_off_time() -> bool:
    """True once today's effective square-off cutoff (see
    _todays_square_off_time - SQUARE_OFF_TIME every day, or
    FRIDAY_SQUARE_OFF_TIME on Fridays specifically) has passed. Webhook
    entry handlers use this to refuse new positions past that point - see
    NOTES.md bug #25. monitor_loop's own square-off only fires once
    (squared_off_today_for), so any position entered after that one-time
    pass would otherwise sit with no further target/SL/square-off
    monitoring for the rest of the day - confirmed live on 24 Aug 2026: a
    webhook arrived seconds after square-off fired and entered a position
    that then had zero automated exit protection."""
    cutoff = _todays_square_off_time()
    if cutoff is None:
        return False
    return _now_ist() >= _parse_hhmm_today(cutoff)


def is_past_allowed_trading_time() -> bool:
    """True once today's config.ALLOWED_TRADING_TIME has passed, but only
    when config.ENABLE_TRADING_TIME_LIMIT is on - webhook entry handlers
    use this to refuse NEW positions past that point, the same way
    is_past_square_off_time() already does for SQUARE_OFF_TIME. When the
    flag is off, this always returns False - new entries are allowed all
    day up to market hours/SQUARE_OFF_TIME, same as before this feature
    existed. Existing open positions are unaffected either way - this only
    gates new entries, not exit monitoring.

    Superseded by config.ENABLE_TRADING_WINDOWS: when the multi-window
    schedule is on, that check (is_within_trading_windows) is the sole
    authority on entry timing and this one short-circuits to False."""
    if config.ENABLE_TRADING_WINDOWS:
        return False
    if not config.ENABLE_TRADING_TIME_LIMIT:
        return False
    return _now_ist() >= _parse_hhmm_today(config.ALLOWED_TRADING_TIME)


def _parse_trading_windows(spec: str) -> list[tuple[dt_time, dt_time]]:
    """"HH:MM-HH:MM,HH:MM-HH:MM" -> [(time, time), ...]. Silently skips a
    malformed chunk rather than raising - a bad window entry must never
    take the whole webhook path down; the worst case of an unparseable
    spec is an empty list, i.e. no window ever matches (fail CLOSED - no
    new entries - which is the safe direction for an entry gate)."""
    windows: list[tuple[dt_time, dt_time]] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        try:
            start_s, end_s = chunk.split("-", 1)
            sh, sm = (int(x) for x in start_s.strip().split(":"))
            eh, em = (int(x) for x in end_s.strip().split(":"))
            windows.append((dt_time(sh, sm), dt_time(eh, em)))
        except (ValueError, TypeError):
            logger.warning("Ignoring malformed trading-window chunk %r in %r", chunk, spec)
    return windows


def is_within_trading_windows(now: Optional[datetime] = None) -> bool:
    """True if config.ENABLE_TRADING_WINDOWS is off (no restriction), or if
    the current IST time falls inside one of config.TRADING_WINDOWS. Each
    window is [start, end) - start inclusive, end exclusive. Only gates
    NEW entries (called from the webhook handlers); open positions keep
    full exit monitoring regardless."""
    if not config.ENABLE_TRADING_WINDOWS:
        return True
    t = (now or _now_ist()).time()
    return any(start <= t < end for start, end in _parse_trading_windows(config.TRADING_WINDOWS))


def _is_before_risk_threshold_cutoff() -> bool:
    """True strictly before today's config.RISK_THRESHOLD_CUTOFF_TIME
    (default 11:30). Backing check for current_max_loss_per_trade_rs()/
    current_profit_protection_threshold_rs() below - factored out so both
    stay in lockstep on the exact same boundary instant."""
    return _now_ist() < _parse_hhmm_today(config.RISK_THRESHOLD_CUTOFF_TIME)


def current_max_loss_per_trade_rs() -> float:
    """MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF before config.
    RISK_THRESHOLD_CUTOFF_TIME, MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF from
    that instant on for the rest of the day (user request 31 Aug 2026) -
    see config.py's own comment for the rationale. Evaluated fresh on
    every _exit_reason_for() call (not cached), so a position that's been
    open since before the cutoff is still re-evaluated against the
    tighter afternoon cap once the clock crosses it, same as every other
    time-of-day gate in this file."""
    if _is_before_risk_threshold_cutoff():
        return config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF
    return config.MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF


def current_profit_protection_threshold_rs() -> float:
    """See current_max_loss_per_trade_rs() just above - identical before/
    after-cutoff split, for PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF/
    _AFTER_CUTOFF."""
    if _is_before_risk_threshold_cutoff():
        return config.PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF
    return config.PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF


def _gen_tag(prefix: str, symbol: str) -> str:
    """Dhan's correlationId rejects special characters (confirmed live:
    GVT&D's "&" caused a hard "Invalid correlationId" rejection on order
    placement, DH-905) - strip anything that isn't alphanumeric before
    embedding the symbol, since several real NSE tickers contain "&"
    (GVT&D, M&M, M&MFIN, ...)."""
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol)
    suffix = "".join(random.choices(string.digits, k=6))
    return f"{prefix}-{safe_symbol[:6]}-{suffix}"[:25]


# --------------------------------------------------------------------------- #
# Step 0: reconcile with positions already open at the broker (call once at
# startup) so a restart mid-day doesn't lose track of them and re-enter.
# --------------------------------------------------------------------------- #
async def reconcile_broker_positions() -> list[Position]:
    """Best-effort import of positions already open at Dhan (e.g. left open
    by a previous run of this process) into the local store. Target/stop-
    loss are computed off the broker's reported average price using the
    current config, since we don't have the original fill we'd normally use -
    these are marked Position.reconciled=True so that's visible downstream.

    Filtered by attribute_open_broker_position (trade_history.py) before
    import: Dhan's /positions data has NO per-strategy tag at all, and
    since Futures also places real orders for the identical instrument
    type (ATM options, see NOTES.md's design-decision entry), an open
    broker position genuinely cannot be assumed to be this strategy's own
    just because it's an open FNO position - it could be Futures'. A
    position this can't confidently attribute to "Futures" specifically
    (no record, or ambiguous) is skipped with a clear warning rather than
    imported - the real position/money at the broker is unaffected either
    way, this only controls whether THIS PROCESS starts trying to manage
    it. See Futures/trading_engine.py's own reconcile_broker_positions for
    the mirror-image filter."""
    loop = asyncio.get_running_loop()
    broker_positions = await loop.run_in_executor(None, dhan_wrapper.get_open_fno_positions)

    positions: list[Position] = []
    for bp in broker_positions:
        avg_price = bp["avg_price"]
        if not avg_price:
            logger.warning(
                "Skipping reconciliation for %s - broker reported no average price.",
                bp["trading_symbol"],
            )
            continue

        owner = await loop.run_in_executor(None, attribute_open_broker_position, bp["trading_symbol"])
        if owner != "Futures":
            logger.warning(
                "Skipping reconciliation for %s - attributed to %s (not Futures) by our own "
                "opened-position history. Real broker position is unaffected; this process just "
                "won't manage it. If this is wrong (e.g. a manually-placed position, or one that "
                "predates this logging), it needs manual handling.",
                bp["trading_symbol"], owner or "no strategy (no record found)",
            )
            continue

        positions.append(Position(
            underlying_symbol=bp["underlying_symbol"],
            option_trading_symbol=bp["trading_symbol"],
            option_type=bp["option_type"] or config.OPTION_TYPE,
            quantity=bp["quantity"],
            lot_size=bp["lot_size"],
            entry_price=avg_price,
            highest_price=avg_price,
            target_price=avg_price * (1 + config.TARGET_PCT),
            hard_stop_loss=avg_price * (1 - config.STOP_LOSS_PCT),
            order_id="",
            # NOT bp["product_type"]: the broker's positions API reports a
            # human-readable label ("INTRADAY"), not the product code our
            # own order_placement() call needs ("MIS") - passing the label
            # straight through made every exit attempt on a reconciled
            # position fail silently (Tradehull's order_placement()
            # swallows the mapping error and returns None, logged only as
            # "Got exception in place_order as 'INTRADAY'"). Confirmed live
            # on 24 Aug 2026: all 4 positions reconciled after a mid-day
            # restart got this wrong, and BIOCON's stop-loss couldn't
            # place a SELL at all until this was fixed - see NOTES.md bug
            # #23. This strategy only ever trades config.OPTIONS_PRODUCT
            # ("MIS") itself, so that's always the correct code here too.
            product_type=config.OPTIONS_PRODUCT,
            reconciled=True,
        ))

    for pos in positions:
        await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, pos.option_trading_symbol)

    return positions


# --------------------------------------------------------------------------- #
# Step 1: rank stocks from the webhook payload by today's % change
# --------------------------------------------------------------------------- #
def rank_and_pick_top_stocks(
    stock_symbols: list[str], top_n: int = config.TOP_N_STOCKS, prefer_highest: bool = True
) -> list[tuple[str, float]]:
    """Returns [(symbol, pct_change), ...] sorted by %change, top_n only.
    prefer_highest=True (the bullish /chartink/webhook) ranks the biggest
    gainers first; False (the bearish /chartink/webhook-sell) ranks the
    biggest decliners first - same "strongest signal among the alerted
    list" idea, just pointed the other way for a PE/bearish scan.
    Stocks that error out on the quote lookup are skipped (and logged).
    Paced with a small delay between calls - Dhan's market-data REST API
    intermittently rate-limit-fails on rapid back-to-back calls (confirmed
    live); get_day_change_pct() already retries individually, but pacing
    keeps that from being needed in the first place for a multi-stock alert.

    config.SELECT_BOTTOM_N_STOCKS (default True, set 26 Aug 2026) then
    decides which END of that ranking actually gets selected: True takes
    the bottom top_n (the weakest-confirming names - for CE the weakest
    gainers, for PE the weakest decliners - a contrarian/laggard bet);
    False restores the original top-N/strongest-mover selection. Only
    matters when more than top_n candidates were ranked - with top_n or
    fewer, both ends are the same slice."""
    scored: list[tuple[str, float]] = []
    for i, symbol in enumerate(stock_symbols):
        if i > 0:
            time.sleep(0.35)
        try:
            pct = dhan_wrapper.get_day_change_pct(symbol)
            scored.append((symbol, pct))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s - could not fetch day change: %s", symbol, exc)

    scored.sort(key=lambda t: t[1], reverse=prefer_highest)
    if config.SELECT_BOTTOM_N_STOCKS:
        return scored[-top_n:] if top_n > 0 else []
    return scored[:top_n]



# --------------------------------------------------------------------------- #
# Step 2: enter positions
# --------------------------------------------------------------------------- #
async def _process_one_entry(symbol: str, option_type: str) -> dict:
    """One ranked stock's full reserve -> broker-dedup-check -> enter
    sequence - factored out of enter_positions_for_stocks (31 Aug 2026,
    user request/resilience audit) so multiple ranked stocks' entries can
    run CONCURRENTLY via asyncio.gather instead of one-after-another.
    Previously up to TOP_N_STOCKS (4) real order-placement round-trips
    queued sequentially per alert; now they fire together, so stock #4 in
    a ranked list doesn't wait on stocks #1-3's own order-placement/fill-
    confirmation latency first.

    Safe to run concurrently as-is, nothing here changed structurally:
    reserve_symbol() was already atomically locked (guards two DIFFERENT
    symbols' concurrent calls same as it always guarded near-simultaneous
    duplicate webhook deliveries for the SAME symbol), and every exception
    path here was already caught locally and turned into a result dict
    rather than left to propagate - asyncio.gather over these coroutines
    can't have one symbol's failure affect another's or crash the whole
    batch.

    Also claims `symbol` in cross_strategy_registry for the ENTIRE
    duration of this function (user request 31 Aug 2026, see that
    module's own docstring) - closes a real race window this file's own
    reserve_symbol()/has_open_position_for_underlying() can't: Options,
    Futures, and Luxury each have their own independent PositionStore and
    share one broker account, so near-simultaneous alerts for the same
    underlying across two of these packages could otherwise both pass
    their own broker check before either order settles, and both place a
    real order for the same stock."""
    loop = asyncio.get_running_loop()

    # Daily re-entry cap (user request 1 Sep 2026: "only allow entry into
    # same trade max 3 times a day") - independent of MAX_LIVE_POSITIONS_
    # CE/_PE (that caps how many can be LIVE at once; this caps how many
    # times the SAME underlying can be entered across the whole day, even
    # after each prior entry has already been exited). Counted regardless
    # of CE/PE ("same trade" = same underlying). Checked here, before the
    # cross_strategy_registry claim, so a stock that's already hit its cap
    # today doesn't even bother claiming the cross-package lock. See
    # trade_history.count_opened_today's own docstring for why this reads
    # the durable on-disk log rather than an in-memory counter.
    entries_today = await loop.run_in_executor(None, count_opened_today, "Futures", symbol)
    if entries_today >= config.MAX_DAILY_ENTRIES_PER_SYMBOL:
        logger.info(
            "%s: skipped - already entered %d time(s) today, at the daily cap of %d",
            symbol, entries_today, config.MAX_DAILY_ENTRIES_PER_SYMBOL,
        )
        return {"symbol": symbol, "status": "skipped", "reason": "daily_reentry_cap_reached"}

    # Same-day loss cooldown - ported from Luxury's identical check (see
    # config.LOSS_COOLDOWN_ENABLED's own docstring for the full
    # MAHABANK/PHOENIXLTD/GVT&D incidents this was built from, on that
    # package). Independent of the daily re-entry cap above - that one is
    # a COUNT limit (at most N times all day); this one is a TIMING limit
    # (not immediately after a loss on this same symbol).
    if config.LOSS_COOLDOWN_ENABLED:
        minutes_since_loss = await loop.run_in_executor(
            None, minutes_since_last_loss_today, "Futures", symbol, datetime.now()
        )
        if minutes_since_loss is not None and minutes_since_loss < config.LOSS_COOLDOWN_MINUTES:
            logger.info(
                "%s: skipped - stopped out %.1f minute(s) ago today, inside the %s-minute loss cooldown",
                symbol, minutes_since_loss, config.LOSS_COOLDOWN_MINUTES,
            )
            return {"symbol": symbol, "status": "skipped", "reason": "loss_cooldown_active"}

    # Repeat-loss same-day block - ported from Luxury's identical check
    # (see config.LOSS_REPEAT_BLOCK_ENABLED's own docstring for how this
    # differs from both guards above). Unlike the timing-based cooldown
    # just above, this one never expires today once tripped - only a
    # genuine loss-designated exit reason counts (a symbol that's merely
    # had several trades, or even several small losing-by-a-hair
    # TRAILING_SL_HIT exits, is not blocked by this - only real MAX_LOSS_
    # HIT/STOP_LOSS_HIT hits are).
    if config.LOSS_REPEAT_BLOCK_ENABLED:
        loss_count = await loop.run_in_executor(
            None, loss_exit_count_today, "Futures", symbol, config.LOSS_REPEAT_BLOCK_EXIT_REASONS, datetime.now(),
        )
        if loss_count >= config.LOSS_REPEAT_BLOCK_COUNT:
            logger.info(
                "%s: skipped - already hit a loss-based exit %d time(s) today (limit %d), "
                "blocked for the rest of the day",
                symbol, loss_count, config.LOSS_REPEAT_BLOCK_COUNT,
            )
            return {"symbol": symbol, "status": "skipped", "reason": "loss_repeat_block_active"}

    if not await cross_strategy_registry.try_claim(symbol, "Futures"):
        logger.info("%s: skipped - another strategy is currently entering it", symbol)
        return {"symbol": symbol, "status": "skipped", "reason": "claimed_by_another_strategy"}

    try:
        # Atomically checks dedup + capacity and claims the symbol in one
        # locked step, so two near-simultaneous calls for the same symbol
        # (e.g. a duplicate Chartink webhook delivery) can't both pass a
        # check-then-act race and both end up placing an order.
        if not await position_store.reserve_symbol(symbol, option_type):
            logger.info("%s: skipped - already open/in-flight, or no capacity", symbol)
            return {"symbol": symbol, "status": "skipped", "reason": "duplicate_or_capacity_full"}

        try:
            # Belt-and-suspenders: confirm the broker doesn't already show
            # an open FNO position for this underlying (another process
            # instance, a manual trade, or state from before this run) -
            # our own reservation above only guards duplicates within this
            # process's in-memory state, and the cross_strategy_registry
            # claim above only guards concurrent ENTRY ATTEMPTS in this
            # same process, not a position that predates either.
            already_open = await loop.run_in_executor(
                None, dhan_wrapper.has_open_position_for_underlying, symbol
            )
            if already_open:
                logger.warning("%s: skipped - broker already shows an open FNO position for it", symbol)
                await position_store.release_symbol(symbol)
                return {"symbol": symbol, "status": "skipped", "reason": "already_open_at_broker"}

            entry_result = await _enter_single_position(symbol, option_type)
            # "entered", "amo_placed", and "pending_confirmation" all
            # correspond to a real order that's now live/queued for this
            # stock - keep the reservation so a repeat alert today is
            # treated as a duplicate, and so this stock's slot still counts
            # against MAX_LIVE_POSITIONS_CE/_PE. Any other outcome
            # (rejected, etc.) frees the symbol up to retry.
            # "pending_confirmation" (bug #22's fix) was missing from this
            # allow-list until now - _enter_single_position deliberately
            # calls release_order_ownership (not release_symbol) to keep
            # this same reservation alive when it defers a slow-filling
            # order to _sync_pending_orders, but this caller was undoing
            # that by releasing the symbol anyway, right after. Confirmed
            # live on 24 Aug 2026: let a 3rd CE position (PETRONET) through
            # past the 2-position cap once its deferred order filled for
            # real - see NOTES.md bug #24.
            if entry_result.get("status") not in ("entered", "amo_placed", "pending_confirmation"):
                await position_store.release_symbol(symbol)
            return entry_result
        except Exception as exc:  # noqa: BLE001
            await position_store.release_symbol(symbol)
            logger.exception("Failed to enter position for %s", symbol)
            return {"symbol": symbol, "status": "error", "reason": str(exc)}
    finally:
        await cross_strategy_registry.release_claim(symbol, "Futures")


async def enter_positions_for_stocks(
    ranked_stocks: list[tuple[str, float]], option_type: str = config.OPTION_TYPE
) -> list[dict]:
    """Places ATM-option BUY orders for as many of the ranked stocks as
    capacity and dedup rules allow. Returns a per-stock result log, in the
    same order as ranked_stocks (asyncio.gather preserves input order).
    option_type is "CE" for the bullish webhook, "PE" for the bearish
    /chartink/webhook-sell one - same entry/exit machinery either way,
    just a different ATM leg.

    Runs all ranked stocks' entries CONCURRENTLY (see _process_one_entry's
    own docstring for why this is safe) - previously sequential, one
    stock's entry waiting on the prior one's full order-placement/fill-
    confirmation round-trip first."""
    return await asyncio.gather(*[
        _process_one_entry(symbol, option_type) for symbol, _pct_change in ranked_stocks
    ])


async def _enter_single_position(symbol: str, option_type: str = config.OPTION_TYPE) -> dict:
    loop = asyncio.get_running_loop()

    atm = await loop.run_in_executor(
        None, dhan_wrapper.get_atm_option, symbol, option_type
    )

    if atm.expiry_date == _now_ist().date():
        # dhan_wrapper.get_atm_option() already tries to roll forward to
        # next month's contract when the nearest one expires today (Dhan
        # blocks new stock-option positions on their own expiry day - see
        # NOTES.md bug #28), so reaching here means even the rolled-forward
        # contract still expires today (e.g. no further expiry listed yet
        # for this stock) - there's truly nothing tradeable for it right
        # now. Skip before placing (or even subscribing to) anything,
        # rather than burn an API call and a noisy rejection log line on an
        # order that would always fail.
        logger.info(
            "%s: skipped - %s expires today and no later expiry is available yet",
            symbol, atm.trading_symbol,
        )
        return {
            "symbol": symbol,
            "status": "skipped_expiry_day",
            "option_trading_symbol": atm.trading_symbol,
            "expiry_date": str(atm.expiry_date),
        }

    quantity = atm.lot_size * config.QUANTITY_LOTS
    tag = _gen_tag(config.ORDER_TAG_PREFIX, symbol)

    # Proactive funds check (added 1 Sep 2026) - see fund_allocation.py's
    # own module docstring for the 2-bucket design. Checks against the
    # shared SECONDARY bucket (Options/Futures/Luxury together), never
    # the whole account's own residual balance. Fails open (proceeds) on
    # its own errors or when config.FUNDS_CHECK_ENABLED is off - the
    # broker's own reactive RMS rejection remains the final safety net
    # either way.
    if config.FUNDS_CHECK_ENABLED:
        try:
            option_ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, atm.trading_symbol)
            sufficient = await fund_allocation.has_sufficient_bucket_funds(
                "secondary", symbol, [(atm.security_id, config.OPTIONS_PRODUCT, quantity, option_ltp)],
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not price the leg for the funds check - proceeding optimistically", symbol)
            sufficient = True
        if not sufficient:
            return {
                "symbol": symbol, "status": "skipped", "reason": "insufficient_funds",
                "option_trading_symbol": atm.trading_symbol,
            }

    # Subscribe to the option's live price over the WebSocket feed as early
    # as possible so ticks are already flowing by the time we start monitoring.
    await loop.run_in_executor(None, dhan_wrapper.subscribe_option_price, atm.trading_symbol)

    order_resp = await loop.run_in_executor(
        None, dhan_wrapper.place_market_order, atm.trading_symbol, quantity, "BUY", tag,
    )
    order_id = order_resp["order_id"]
    is_amo = order_resp["is_amo"]

    await position_store.record_order(OrderRecord(
        order_id=order_id,
        underlying_symbol=symbol,
        trading_symbol=atm.trading_symbol,
        transaction_type="BUY",
        quantity=quantity,
        status=OrderStatus.TRANSIT,
        is_amo=is_amo,
        lot_size=atm.lot_size,
        option_type=atm.option_type,
    ))

    result = await loop.run_in_executor(
        None, dhan_wrapper.wait_for_order_result, order_id, is_amo
    )
    await position_store.update_order_status(order_id, result.status, result.remark)

    if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, atm.trading_symbol)
        logger.warning(
            "BUY order %s for %s rejected: status=%s remark=%s",
            order_id, symbol, result.status, result.remark,
        )
        return {
            "symbol": symbol,
            "status": "rejected",
            "order_status": result.status,
            "remark": result.remark,
            "option_trading_symbol": atm.trading_symbol,
            "order_id": order_id,
        }

    if result.is_queued_amo:
        # Placed outside market hours - queued as an AMO rather than filled
        # now. Keep the symbol reserved (so a repeat alert for it today is
        # still treated as a duplicate) but don't create a live Position
        # yet - nothing has actually been bought. Hand off ownership so
        # monitor_loop's _sync_pending_orders() is now allowed to pick this
        # order up and promote it to a Position once the next session
        # actually fills it - before this point (NEW/ACKED/etc., still
        # being resolved right here), it must not touch it.
        await position_store.release_order_ownership(order_id)
        logger.info(
            "BUY order %s for %s queued as AMO - will confirm fill next session.",
            order_id, symbol,
        )
        return {
            "symbol": symbol,
            "status": "amo_placed",
            "order_status": result.status,
            "option_trading_symbol": atm.trading_symbol,
            "quantity": quantity,
            "order_id": order_id,
        }

    if result.status not in OrderStatus.TERMINAL_STATUSES:
        # Didn't reach a terminal status within the poll budget - seen live
        # during a Dhan-side slow-fill period where a plain market order
        # took well over a minute to settle (NOTES.md bug #22). Not
        # rejected/cancelled and not AMO, so the order is still genuinely
        # live at the broker and may yet fill (or reject) - guessing a
        # price via LTP here would let target/SL be computed off a value
        # that can diverge materially from the real eventual fill price
        # (confirmed live: LTP fallback of 0.59 vs a real average fill of
        # 0.46 on the same order - a false stop-loss trigger and a wrong
        # P&L). Deferring to _sync_pending_orders's existing AMO-style
        # polling instead - it already re-checks non-terminal BUY orders
        # every monitor tick and promotes to a Position using the REAL
        # fill price once Dhan confirms it, or releases the reservation if
        # it ends up rejected/cancelled. release_order_ownership (not
        # release_symbol) keeps this symbol reserved in the meantime, so a
        # repeat alert can't also enter it while its fate is still open.
        await position_store.release_order_ownership(order_id)
        logger.warning(
            "BUY order %s for %s still %s after poll budget - deferring to "
            "background sync instead of guessing a fill price.",
            order_id, symbol, result.status,
        )
        return {
            "symbol": symbol,
            "status": "pending_confirmation",
            "order_status": result.status,
            "option_trading_symbol": atm.trading_symbol,
            "quantity": quantity,
            "order_id": order_id,
        }

    fill_price = result.fill_price
    if not fill_price:
        # Order reached a terminal "filled" status but the fill price field
        # hasn't populated yet (rare) - fall back to LTP so target/SL levels
        # aren't computed off zero.
        fill_price = await loop.run_in_executor(
            None, dhan_wrapper.get_option_ltp, atm.trading_symbol
        )

    entry_candle_start = await _capture_supertrend_entry_candle(loop, symbol)

    # Broker-side stop-loss order - ported from Luxury's identical
    # mechanism (see config.BROKER_STOP_LOSS_ENABLED's own docstring for
    # the full SL-M->SL-L story and rollout discipline). Placed here,
    # right after the real fill_price is known, using whichever MAX_LOSS
    # cutoff value is active RIGHT NOW (same "computed once at entry"
    # convention as target_price/hard_stop_loss below). A failure to
    # place this is logged loudly but never blocks the entry itself -
    # stop_loss_order_id simply stays None, and the position is exactly
    # as protected as it always was via the existing poll/tick-driven
    # MAX_LOSS_HIT check.
    stop_loss_order_id = None
    if config.BROKER_STOP_LOSS_ENABLED:
        trigger_price = fill_price - (current_max_loss_per_trade_rs() / quantity)
        limit_price = trigger_price * (1 - config.BROKER_STOP_LOSS_LIMIT_BUFFER_PCT)
        try:
            stop_tag = _gen_tag("SL", symbol)
            stop_resp = await loop.run_in_executor(
                None, dhan_wrapper.place_stop_loss_limit_order,
                atm.trading_symbol, quantity, "SELL", trigger_price, limit_price, stop_tag, config.OPTIONS_PRODUCT,
            )
            stop_loss_order_id = stop_resp["order_id"]
            logger.info(
                "%s: broker-side SELL stop-loss LIMIT order %s placed for %s, trigger=%.2f limit=%.2f",
                symbol, stop_loss_order_id, atm.trading_symbol, trigger_price, limit_price,
            )
            await position_store.record_order(OrderRecord(
                order_id=stop_loss_order_id,
                underlying_symbol=symbol,
                trading_symbol=atm.trading_symbol,
                transaction_type="SELL",
                quantity=quantity,
                status=OrderStatus.PENDING,
                is_amo=False,
                lot_size=atm.lot_size,
                option_type=atm.option_type,
            ))
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not place the broker-side stop-loss order for %s (trigger would have been "
                "%.2f, limit %.2f) - proceeding without it, the existing poll/tick-driven MAX_LOSS_HIT "
                "check still protects this position exactly as before",
                symbol, atm.trading_symbol, trigger_price, limit_price,
            )

    position = Position(
        underlying_symbol=symbol,
        option_trading_symbol=atm.trading_symbol,
        option_type=atm.option_type,
        quantity=quantity,
        lot_size=atm.lot_size,
        entry_price=fill_price,
        highest_price=fill_price,
        target_price=fill_price * (1 + config.TARGET_PCT),
        hard_stop_loss=fill_price * (1 - config.STOP_LOSS_PCT),
        order_id=order_id,
        product_type=config.OPTIONS_PRODUCT,
        supertrend_entry_candle_start=entry_candle_start,
        stop_loss_order_id=stop_loss_order_id,
    )
    await position_store.add_position(position)

    logger.info(
        "BUY order %s FILLED for %s (%s): qty=%s entry_price=%s target=%.2f sl=%.2f",
        order_id, symbol, atm.trading_symbol, quantity, fill_price,
        position.target_price, position.hard_stop_loss,
    )

    return {
        "symbol": symbol,
        "status": "entered",
        "order_status": result.status,
        "option_trading_symbol": atm.trading_symbol,
        "quantity": quantity,
        "entry_price": fill_price,
        "order_id": order_id,
    }


# --------------------------------------------------------------------------- #
# Step 3: monitoring / exits
# --------------------------------------------------------------------------- #
async def _exit_position(symbol: str, position: Position, exit_price: float, reason: str) -> None:
    """Caller MUST have already claimed the exit via
    position_store.try_start_exit(symbol) before calling this - it does not
    check/claim itself, since exit checks now fire from two places (the
    poll loop and event-driven WebSocket ticks) and the claim has to happen
    atomically before either one decides to act.

    The whole body is wrapped in a catch-all as a safety net: any
    unexpected exception here (not just a failed placement) must still
    release the claim via record_exit_failure(), or the position would be
    stuck forever unable to exit - try_start_exit() would keep seeing a
    non-empty pending_exit_order_id and refuse every future attempt,
    including from /square-off-now.

    After ANY prior exit failure (exit_failure_count >= 1; record_exit_
    failure's own backoff already spaces these out - see NOTES.md's
    design-decision entry), reconciles with the broker BEFORE attempting
    another SELL: if this exact contract is already flat there (closed
    manually, by another process, or a stuck rejection that actually
    resolved), closes it locally instead of retrying - avoiding a doomed
    order-placement API call AND the unintended fresh short if the broker's
    state diverged. Tightened from ">= 2" to ">= 1" on 10 Sep 2026 after
    TECHM: a MAX_LOSS exit was RMS-rejected for margin (attempt 1), the
    position was then sold manually, and the attempt-2 retry - still under
    the old 2-failure threshold - filled into a real -600 naked short. One
    extra get_broker_net_quantity call on the retry path is a negligible
    cost for closing that gap; a genuinely transient failure (position
    still open at the broker) just sees broker_qty == position.quantity
    and retries exactly as before.

    Before EVERY placement attempt (not gated by exit_failure_count, since
    the scenario this guards against can happen on the very first
    post-restart attempt), checks for an already-outstanding SELL order at
    the broker for this exact contract and cancels it first if found - see
    dhan_wrapper.get_pending_order_id's docstring for why a stale pending
    order surviving a restart can otherwise get a fresh SELL rejected for
    "insufficient funds" it doesn't actually need.

    Broker-quantity reconciliation after a stale-order cancel (ported
    from Luxury's identical fix, added there 9 Sep 2026 alongside the
    SL-L broker stop-loss - see config.BROKER_STOP_LOSS_ENABLED's own
    docstring): whenever a stale resting SELL order is found and
    cancelled below, position.quantity gets RE-DERIVED from the broker's
    own real net quantity before any fresh SELL is placed. A genuine
    SL-L order can PARTIALLY fill: some quantity sold at the limit
    price, the remainder left resting. If that remainder is still
    outstanding when a DIFFERENT exit reason fires through the normal
    reactive path, blindly placing a fresh SELL for the full stored
    position.quantity would try to sell MORE than is actually still
    held - an unintended naked short. Re-checking real broker truth
    here, every time a stale order is found, closes that gap regardless
    of which broker-side order type ever gets used for the resting one -
    every reference to position.quantity below (the fresh SELL's own
    quantity, its OrderRecord, and the final pnl calc) picks up the
    corrected value for free since Position is mutated in place."""
    loop = asyncio.get_running_loop()

    try:
        stale_order_id = await loop.run_in_executor(
            None, dhan_wrapper.get_pending_order_id, position.option_trading_symbol, "SELL"
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not check for an already-outstanding SELL order before placing a new one - "
            "proceeding anyway", symbol,
        )
        stale_order_id = None

    # get_pending_order_id scans the broker order book by symbol + status,
    # so it can MISS an order that's only seconds old (Dhan OMS lag) or one
    # sitting in a status the scan doesn't list. That is exactly how a
    # broker-side SL-L was orphaned on Luxury for OIL on 10 Sep 2026: the
    # position hit PROFIT_PROTECTION_HIT 13 seconds after entry, this scan
    # returned None, the position closed via a fresh SELL, and the SL-L was
    # left resting at the broker with no position behind it - a naked short
    # waiting for its trigger. Options shares this exact code, so it would
    # do the same once BROKER_STOP_LOSS_ENABLED is turned on here. We hold
    # the SL-L's real order_id on the Position, so fall back to it: the
    # cancel + get_broker_net_quantity reconciliation below then handle
    # every case (still resting -> just cancelled; already fired -> broker
    # flat -> close at its own fill price; partial fill -> sell only the
    # real remainder).
    if not stale_order_id and config.BROKER_STOP_LOSS_ENABLED and position.stop_loss_order_id:
        stale_order_id = position.stop_loss_order_id

    if stale_order_id:
        logger.warning(
            "%s: found an already-outstanding SELL order %s for %s (a stale order surviving a "
            "restart, or this position's own broker-side SL-L) - cancelling it before placing a "
            "fresh exit order.",
            symbol, stale_order_id, position.option_trading_symbol,
        )
        try:
            await loop.run_in_executor(None, dhan_wrapper.cancel_order, stale_order_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not cancel stale SELL order %s - proceeding with a new order anyway",
                              symbol, stale_order_id)

        try:
            broker_qty = await loop.run_in_executor(
                None, dhan_wrapper.get_broker_net_quantity, position.option_trading_symbol
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not reconcile broker quantity after cancelling stale order %s - proceeding "
                "with the stored quantity (%d), which WOULD oversell into a naked short if that order "
                "had actually partially filled", symbol, stale_order_id, position.quantity,
            )
            broker_qty = None
        if broker_qty is not None and broker_qty != position.quantity:
            if broker_qty == 0:
                logger.warning(
                    "%s: broker shows this position already FLAT after cancelling stale order %s - it "
                    "must have fully filled (e.g. our own broker-side stop-loss) right before/during "
                    "the cancel race. Reconciling as closed using that order's own real fill price "
                    "instead of placing a fresh SELL.", symbol, stale_order_id,
                )
                try:
                    stale_result = await loop.run_in_executor(
                        None, dhan_wrapper.refresh_order_status, stale_order_id
                    )
                    final_exit_price = stale_result.fill_price or exit_price
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "%s: could not fetch stale order %s's own fill price - using %.2f instead",
                        symbol, stale_order_id, exit_price,
                    )
                    final_exit_price = exit_price
                await position_store.close_position(symbol, final_exit_price, reason)
                await loop.run_in_executor(
                    None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol
                )
                return
            logger.warning(
                "%s: broker shows only %d qty left (stored position says %d) after cancelling stale "
                "order %s - a PARTIAL fill happened on that resting order. Selling only the real "
                "remaining %d qty instead of the stale %d to avoid an unintended naked short. NOTE: "
                "the pnl logged for this exit covers only this remaining leg, not a blended figure "
                "across both fills - check /positions' own orders_today (or Dhan's real order record "
                "for %s) for the earlier partial fill's own price/quantity if an exact total is "
                "needed.", symbol, broker_qty, position.quantity, stale_order_id, broker_qty,
                position.quantity, stale_order_id,
            )
            position.quantity = broker_qty

    if position.exit_failure_count >= 1:
        try:
            broker_qty = await loop.run_in_executor(
                None, dhan_wrapper.get_broker_net_quantity, position.option_trading_symbol
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: could not reconcile broker position before retrying exit (attempt %d) - "
                "proceeding with the retry anyway", symbol, position.exit_failure_count,
            )
            broker_qty = None  # unknown - fall through to a normal retry rather than guess
        if broker_qty == 0:
            logger.warning(
                "%s: broker shows this position already flat after %d exit failure(s) - reconciling "
                "locally as closed instead of retrying (a retry here would sell into a naked short).",
                symbol, position.exit_failure_count,
            )
            mark_price = exit_price or position.highest_price
            await position_store.close_position(symbol, mark_price, "RECONCILED_ALREADY_FLAT")
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
            return

    tag = _gen_tag("Ext", symbol)
    try:
        order_resp = await loop.run_in_executor(
            None, dhan_wrapper.place_market_order,
            position.option_trading_symbol, position.quantity, "SELL", tag, position.product_type,
        )
    except Exception:  # noqa: BLE001
        logger.exception("SELL order failed for %s (%s) - backing off before retrying",
                          symbol, position.option_trading_symbol)
        await position_store.record_exit_failure(symbol)
        return  # leave it live; next eligible retry is gated by next_exit_retry_at

    try:
        order_id = order_resp["order_id"]
        is_amo = order_resp["is_amo"]
        await position_store.record_order(OrderRecord(
            order_id=order_id,
            underlying_symbol=symbol,
            trading_symbol=position.option_trading_symbol,
            transaction_type="SELL",
            quantity=position.quantity,
            status=OrderStatus.TRANSIT,
            is_amo=is_amo,
        ))
        # Replaces the try_start_exit() placeholder with the real order_id.
        await position_store.set_pending_exit_order(symbol, order_id, reason)

        result = await loop.run_in_executor(
            None, dhan_wrapper.wait_for_order_result, order_id, is_amo
        )
        await position_store.update_order_status(order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            # The SELL was rejected/cancelled by the exchange - the position
            # is still genuinely open at the broker. Clear the pending
            # marker and back off before retrying - confirmed live that a
            # rejection reason like insufficient margin doesn't resolve
            # within one tick, and without backoff this retries every 5s
            # indefinitely.
            logger.warning(
                "SELL order %s for %s rejected: status=%s remark=%s - backing off before retrying",
                order_id, symbol, result.status, result.remark,
            )
            await position_store.set_pending_exit_order(symbol, None)
            await position_store.record_exit_failure(symbol)
            return

        await position_store.clear_exit_failure(symbol)

        if result.is_queued_amo:
            # Placed outside market hours - queued as an AMO rather than
            # filled now. Leave the position live with pending_exit_order_id
            # set (the real order_id, already applied above);
            # _sync_pending_orders() will close it once the next session
            # actually fills this order.
            logger.info(
                "SELL order %s for %s queued as AMO - will confirm fill next session.",
                order_id, symbol,
            )
            return

        final_exit_price = result.fill_price or exit_price
        await position_store.close_position(symbol, final_exit_price, reason)
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
        pnl = (final_exit_price - position.entry_price) * position.quantity
        logger.info(
            "SELL order %s FILLED for %s (%s): reason=%s entry=%s exit=%s qty=%s pnl=%.2f",
            order_id, symbol, position.option_trading_symbol, reason,
            position.entry_price, final_exit_price, position.quantity, pnl,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error resolving SELL order for %s (%s) - backing off before retrying",
                          symbol, position.option_trading_symbol)
        await position_store.record_exit_failure(symbol)


async def _get_ltp(trading_symbol: str) -> Optional[float]:
    """Prefers the WebSocket feed's cached LTP (near-instant); falls back to
    a REST call if no tick has arrived yet (e.g. subscription just made) OR
    the cached tick is older than config.LTP_STALE_AFTER_SECONDS (see
    get_cached_option_ltp's docstring - a thinly-traded option can otherwise
    go minutes without a fresh price). The REST result is fed back into the
    cache via note_rest_ltp so a persistently-quiet option isn't re-fetched
    on every single poll - see that method's docstring.

    The REST fallback itself is gated by dhan_wrapper.ltp_rest_fallback_
    semaphore (shared with Futures - same underlying rate-limit budget) -
    caps how many of these can be in flight at once if several positions
    go stale in the same poll tick, without slowing down the (much more
    common) cache-hit path at all."""
    loop = asyncio.get_running_loop()
    ltp = await loop.run_in_executor(None, dhan_wrapper.get_cached_option_ltp, trading_symbol)
    if ltp is not None:
        return ltp
    async with dhan_wrapper.ltp_rest_fallback_semaphore:
        ltp = await loop.run_in_executor(None, dhan_wrapper.get_option_ltp, trading_symbol)
        await loop.run_in_executor(None, dhan_wrapper.note_rest_ltp, trading_symbol, ltp)
    return ltp


def _exit_on_cooldown(position: Position) -> bool:
    return bool(position.next_exit_retry_at and datetime.now() < position.next_exit_retry_at)


async def _capture_supertrend_entry_candle(loop, underlying_symbol: str) -> Optional[datetime]:
    """Best-effort snapshot of the underlying's current Supertrend candle
    boundary, taken once at entry and stored on Position.
    supertrend_entry_candle_start - see _supertrend_signal_for(). A fetch
    failure here shouldn't block an entry that's already filled; returning
    None just means the exit check won't have an entry-candle baseline to
    compare against (treated as "not blocked" - see _supertrend_signal_for)."""
    if not config.ENABLE_SUPERTREND_EXIT:
        return None
    await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, underlying_symbol)
    return dhan_wrapper.get_cached_supertrend_candle_start(underlying_symbol)


def _supertrend_signal_for(position: Position) -> bool:
    """Cache-only, synchronous - safe to call from both the poll loop and
    the WebSocket tick path. Returns whether the underlying's Supertrend
    has turned against the position's own direction AND the cached signal
    is reading a candle PAST the one the position was entered on - i.e.
    immediate action on the very next candle, no extra grace window (the
    former SUPERTREND_ENTRY_GRACE_MINUTES tuning knob was removed entirely
    by user request 27 Aug 2026 - see config.py's removal note).

    Direction depends on option_type: a CE (long call) profits when the
    underlying rises, so a *bearish* crossover is the reversal-against-it
    signal. A PE (long put) profits when the underlying falls, so the
    reversal-against-it signal is the opposite - a *bullish* crossover,
    not a bearish one. Using the same "bearish = exit" check for both
    would exit CE positions correctly but PE positions exactly backwards
    (treating the move that confirms the PE thesis as the exit trigger).

    The ONE delay deliberately kept, per explicit user confirmation when
    the grace period was removed: never act on the EXACT SAME candle the
    position was entered on. Confirmed live before this existed at all:
    without the entry-candle skip, this was cutting winning trades flat at
    breakeven the instant they were entered - the entry candle's own
    Supertrend read can still reflect the pre-breakout state, not yet the
    move that justified entering."""
    is_bearish = dhan_wrapper.get_cached_supertrend_bearish(position.underlying_symbol)
    if is_bearish is None:
        return False  # no signal yet - never force an exit on missing data
    against_position = is_bearish if position.option_type == "CE" else (not is_bearish)
    if not against_position:
        return False
    candle_start = dhan_wrapper.get_cached_supertrend_candle_start(position.underlying_symbol)
    entry_candle_start = position.supertrend_entry_candle_start
    if candle_start is None or entry_candle_start is None:
        return True  # no entry-candle baseline captured - don't block on it
    return candle_start > entry_candle_start


def _exit_reason_for(
    position: Position, ltp: float, supertrend_against_position: bool = False,
    liquidity_guard_triggered: bool = False,
) -> Optional[str]:
    """Shared target/stop-loss/Supertrend evaluation - used by both the poll
    loop and the event-driven WebSocket tick handler so the two paths can't
    drift apart from each other. supertrend_against_position reflects the
    underlying's 5-min close crossing to the wrong side of its 5-min
    Supertrend for this position's direction (see
    trading_engine._supertrend_signal_for - CE vs. PE flips which crossover
    counts) - caller's responsibility to fetch/pass it, since that read can
    involve I/O and this function stays synchronous.

    Checks current_max_loss_per_trade_rs() first, ahead of every other exit
    condition - an absolute per-trade rupee-loss cap independent of
    STOP_LOSS_PCT, since a large-quantity/low-premium position can still
    lose more than this cap in rupee terms before its percentage stop-loss
    fires. Applies identically to CE and PE - both are long-premium
    positions (this strategy only ever buys options, never sells), so a
    loss is (entry_price - ltp) * quantity either way, no direction-
    specific logic needed. The cap itself tightens after
    config.RISK_THRESHOLD_CUTOFF_TIME (user request 31 Aug 2026) - see
    current_max_loss_per_trade_rs()'s own docstring.

    The fixed +TARGET_PCT exit is gated by config.ENABLE_TARGET_EXIT
    (default on). With it off (Futures, 10 Sep 2026) a winner is never
    closed just for touching target_price - it rides on to the checks
    below instead. target_price is still stored/used for display.

    After TARGET_HIT, checks current_profit_protection_threshold_rs() - the
    mirror image on the upside, same before/after-cutoff split. Once the
    position's PEAK unrealized profit ((highest_price - entry_price) *
    quantity) has exceeded this threshold, exits once price has retraced
    at least config.PROFIT_PROTECTION_GIVEBACK_PCT off that peak
    (ltp < highest_price * (1 - PROFIT_PROTECTION_GIVEBACK_PCT)). The
    give-back buffer defaults to 0.0 - bit-identical to the original "exit
    the moment price is off the peak at all" behaviour - and was added
    10 Sep 2026 (see config) so a trade that's still trending isn't
    stopped out on a one-tick wiggle. highest_price is already maintained
    by the caller (update_highest_price) before this runs, so a tick that
    itself sets a new high never triggers this - only a later tick far
    enough below an already-recorded peak does. The percentage trailing/
    hard SL below still catches a larger reversal independently.

    liquidity_guard_triggered (ported from Luxury's identical check, added
    there 2 Sep 2026 - see config.LIQUIDITY_GUARD_ENABLED's own docstring)
    is checked LAST, deliberately - an independent, additional early-
    warning trigger (the option's own contract has gone quiet for several
    minutes straight), not meant to override a genuine profit-taking exit
    that already fired first on the exact same tick; it only matters when
    NONE of the price-threshold checks above have fired yet."""
    loss_rs = (position.entry_price - ltp) * position.quantity
    if loss_rs >= current_max_loss_per_trade_rs():
        return "MAX_LOSS_HIT"
    if config.ENABLE_TARGET_EXIT and ltp >= position.target_price:
        return "TARGET_HIT"
    peak_profit_rs = (position.highest_price - position.entry_price) * position.quantity
    giveback_floor = position.highest_price * (1 - config.PROFIT_PROTECTION_GIVEBACK_PCT)
    if peak_profit_rs > current_profit_protection_threshold_rs() and ltp < giveback_floor:
        return "PROFIT_PROTECTION_HIT"
    trailing_sl = position.current_trailing_sl
    if ltp <= trailing_sl:
        return "TRAILING_SL_HIT" if trailing_sl > position.hard_stop_loss else "STOP_LOSS_HIT"
    if config.ENABLE_SUPERTREND_EXIT and supertrend_against_position:
        return "SUPERTREND_EXIT"
    if config.LIQUIDITY_GUARD_ENABLED and liquidity_guard_triggered:
        return "LIQUIDITY_GUARD_ZERO_VOLUME"
    return None


async def _check_broker_stop_already_filled(symbol: str, position: Position) -> bool:
    """Ported from Luxury's identical function - see config.BROKER_STOP_
    LOSS_ENABLED's own docstring for the full design. Cheap (cache-only
    unless the order has actually gone terminal - see check_if_order_
    filled's own docstring) - safe to call on every single monitor tick/
    price tick for every open position. Returns True if the broker's own
    stop-loss order had ALREADY filled (in which case the position is
    closed here directly, no fresh SELL needed - it's already flat at
    the broker) - callers should stop processing this position for the
    current tick either way."""
    if not config.BROKER_STOP_LOSS_ENABLED or not position.stop_loss_order_id:
        return False
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, dhan_wrapper.check_if_order_filled, position.stop_loss_order_id
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s: could not check broker stop-loss order %s status - falling through to "
                          "the normal poll/tick-driven check this tick", symbol, position.stop_loss_order_id)
        return False
    if result is None:
        return False  # still resting, unfired - nothing to do
    if result.status == OrderStatus.TRADED:
        final_exit_price = result.fill_price or position.hard_stop_loss
        logger.info(
            "%s: broker-side stop-loss order %s ALREADY FILLED (exchange fired it directly, ahead of "
            "our own poll/tick check) - closing at the real fill price %.2f",
            symbol, position.stop_loss_order_id, final_exit_price,
        )
        await position_store.close_position(symbol, final_exit_price, "MAX_LOSS_HIT")
        await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
        return True
    # REJECTED/CANCELLED/EXPIRED - the broker-side stop is gone without
    # firing (e.g. a margin/RMS rejection after placement). The position
    # is now protected ONLY by the existing poll/tick-driven MAX_LOSS_HIT
    # check from here on - logged loudly since this is a real, if rare,
    # loss of the faster backstop this feature exists to provide.
    logger.warning(
        "%s: broker-side stop-loss order %s ended as %s without firing - this position now relies "
        "solely on the regular poll/tick-driven MAX_LOSS_HIT check",
        symbol, position.stop_loss_order_id, result.status,
    )
    return False


async def _check_one_position(symbol: str, position: Position) -> None:
    if position.pending_exit_order_id or _exit_on_cooldown(position):
        return  # already has an outstanding exit order, or backing off after a placement failure

    if await _check_broker_stop_already_filled(symbol, position):
        return

    try:
        ltp = await _get_ltp(position.option_trading_symbol)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch LTP for %s", position.option_trading_symbol)
        return

    await position_store.update_highest_price(symbol, ltp)

    supertrend_against_position = False
    if config.ENABLE_SUPERTREND_EXIT:
        loop = asyncio.get_running_loop()
        # Blocking REST call, cached internally (see refresh_supertrend_signal) -
        # safe to call every poll tick since it's a no-op unless the cache is
        # stale. Only done here, not in on_price_tick, to keep the WebSocket
        # tick path non-blocking - this poll (every MONITOR_INTERVAL_SECONDS)
        # is what keeps the cache fresh for both paths to read.
        await loop.run_in_executor(None, dhan_wrapper.refresh_supertrend_signal, position.underlying_symbol)
        supertrend_against_position = _supertrend_signal_for(position)

    liquidity_guard_triggered = False
    if config.LIQUIDITY_GUARD_ENABLED:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dhan_wrapper.refresh_liquidity_signal, position.option_trading_symbol)
        liquidity_guard_triggered = bool(dhan_wrapper.get_cached_illiquid(position.option_trading_symbol))

    reason = _exit_reason_for(position, ltp, supertrend_against_position, liquidity_guard_triggered)
    if reason and await position_store.try_start_exit(symbol):
        await _exit_position(symbol, position, ltp, reason)


async def on_price_tick(trading_symbol: str, ltp: float) -> None:
    """Event-driven exit check, fired as soon as a new price arrives over
    the WebSocket market feed - instead of waiting for monitor_loop's next
    poll (up to MONITOR_INTERVAL_SECONDS, 5s by default, late). The poll
    loop still runs as a fallback/heartbeat (e.g. if the feed is disabled
    or a position's symbol isn't ticking). Wired up in option_main.py's
    lifespan via dhan_wrapper.add_price_tick_subscriber(), invoked from the
    feed's own thread via asyncio.run_coroutine_threadsafe - wrapped in
    try/except here because exceptions from a threadsafe-scheduled
    coroutine are otherwise silently dropped rather than surfacing
    anywhere."""
    try:
        match = next(
            ((sym, pos) for sym, pos in position_store.live_positions.items()
             if pos.option_trading_symbol == trading_symbol),
            None,
        )
        if not match:
            return
        symbol, position = match

        if position.pending_exit_order_id or _exit_on_cooldown(position):
            return

        if await _check_broker_stop_already_filled(symbol, position):
            return

        await position_store.update_highest_price(symbol, ltp)

        # Cache-only read (no I/O) - the poll loop's refresh_supertrend_signal()
        # keeps this warm; a blocking REST call here would stall the event
        # loop on every tick.
        supertrend_against_position = config.ENABLE_SUPERTREND_EXIT and _supertrend_signal_for(position)
        liquidity_guard_triggered = config.LIQUIDITY_GUARD_ENABLED and bool(
            dhan_wrapper.get_cached_illiquid(position.option_trading_symbol)
        )

        reason = _exit_reason_for(position, ltp, supertrend_against_position, liquidity_guard_triggered)
        if reason and await position_store.try_start_exit(symbol):
            await _exit_position(symbol, position, ltp, reason)
    except Exception:  # noqa: BLE001
        logger.exception("on_price_tick failed for %s", trading_symbol)


async def _square_off_all(reason: str) -> None:
    positions = dict(position_store.live_positions)  # snapshot of keys/values
    if not positions:
        return
    logger.info("Square-off triggered (%s) for %d open position(s)", reason, len(positions))
    for symbol, position in positions.items():
        if position.pending_exit_order_id or _exit_on_cooldown(position):
            continue  # already has an outstanding exit order, or backing off after a placement failure
        try:
            ltp = await _get_ltp(position.option_trading_symbol)
        except Exception:  # noqa: BLE001
            ltp = position.entry_price  # best-effort fallback for logging only
        if not await position_store.try_start_exit(symbol):
            continue  # an event-driven tick claimed it in the meantime
        await _exit_position(symbol, position, ltp, reason)


async def _sync_pending_orders() -> None:
    """Re-checks orders whose fate was still open last time: queued AMO BUY
    entries not yet promoted to a live Position, and outstanding exit
    orders on live positions. Cheap - normally a no-op, since orders placed
    during market hours already resolve within wait_for_order_result's own
    retry budget. This only matters for AMO orders that need to be picked
    up once the next session actually dispatches them."""
    loop = asyncio.get_running_loop()

    pending_entries = [
        o for o in position_store.orders_today.values()
        if o.transaction_type == "BUY"
        and o.status not in OrderStatus.TERMINAL_STATUSES
        and o.underlying_symbol not in position_store.live_positions
        # Confirmed live: without this, a monitor_loop tick landing while
        # _enter_single_position is still inline-resolving a fresh order
        # (e.g. delayed by a rate-limit retry) races the placer to promote
        # the same order to a Position twice.
        and not o.owned_by_placer
    ]
    for order in pending_entries:
        try:
            result = await loop.run_in_executor(
                None, dhan_wrapper.refresh_order_status, order.order_id, order.is_amo
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh AMO BUY order %s", order.order_id)
            continue

        await position_store.update_order_status(order.order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning(
                "AMO BUY order %s for %s ended as %s - releasing reservation.",
                order.order_id, order.underlying_symbol, result.status,
            )
            await position_store.release_symbol(order.underlying_symbol)
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, order.trading_symbol)
            continue

        if result.status in OrderStatus.TERMINAL_STATUSES:
            fill_price = result.fill_price
            if not fill_price:
                fill_price = await loop.run_in_executor(
                    None, dhan_wrapper.get_option_ltp, order.trading_symbol
                )
            entry_candle_start = await _capture_supertrend_entry_candle(loop, order.underlying_symbol)
            position = Position(
                underlying_symbol=order.underlying_symbol,
                option_trading_symbol=order.trading_symbol,
                option_type=order.option_type or config.OPTION_TYPE,
                quantity=order.quantity,
                lot_size=order.lot_size or config.LOT_SIZE_FALLBACK,
                entry_price=fill_price,
                highest_price=fill_price,
                target_price=fill_price * (1 + config.TARGET_PCT),
                hard_stop_loss=fill_price * (1 - config.STOP_LOSS_PCT),
                order_id=order.order_id,
                product_type=config.OPTIONS_PRODUCT,
                supertrend_entry_candle_start=entry_candle_start,
            )
            await position_store.add_position(position)
            logger.info(
                "AMO BUY order %s for %s filled - position now live.",
                order.order_id, order.underlying_symbol,
            )
        # else: still queued as AMO - nothing to do, check again next tick.

    positions = dict(position_store.live_positions)
    for symbol, position in positions.items():
        if not position.pending_exit_order_id or position.pending_exit_order_id == EXIT_CLAIMED:
            continue  # no exit outstanding, or one's mid-placement right now - nothing to sync yet
        try:
            result = await loop.run_in_executor(
                None, dhan_wrapper.refresh_order_status, position.pending_exit_order_id, True
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh AMO SELL order %s", position.pending_exit_order_id)
            continue

        await position_store.update_order_status(position.pending_exit_order_id, result.status, result.remark)

        if result.status in OrderStatus.REJECTED_STATUSES or result.status == OrderStatus.CANCELLED:
            logger.warning(
                "AMO SELL order %s for %s ended as %s - clearing so the next tick retries the exit.",
                position.pending_exit_order_id, symbol, result.status,
            )
            await position_store.set_pending_exit_order(symbol, None)
            continue

        if result.status in OrderStatus.TERMINAL_STATUSES:
            final_exit_price = result.fill_price or position.highest_price
            await position_store.close_position(symbol, final_exit_price, position.pending_exit_reason or "AMO_EXIT_FILLED")
            await loop.run_in_executor(None, dhan_wrapper.unsubscribe_option_price, position.option_trading_symbol)
        # else: still queued as AMO - nothing to do, check again next tick.


async def monitor_loop() -> None:
    """Runs forever; polls open positions and enforces exits + a square-off
    on whichever days _todays_square_off_time() says apply one - every day
    (config.ENABLE_SQUARE_OFF), Fridays only
    (config.ENABLE_FRIDAY_SQUARE_OFF, so a position never carries into the
    weekend), or neither (NRML/overnight carry Mon-Thu) - see NOTES.md's
    design-decision entry."""
    logger.info("Monitor loop started.")
    squared_off_today_for: set = set()

    while True:
        try:
            await position_store.maybe_reset_for_new_day()
            await _sync_pending_orders()

            cutoff = _todays_square_off_time()
            if cutoff is not None:
                now = _now_ist()
                square_off_at = _parse_hhmm_today(cutoff)
                today_key = now.date()

                if now >= square_off_at and today_key not in squared_off_today_for:
                    reason = "EOD_SQUARE_OFF_FRIDAY" if not config.ENABLE_SQUARE_OFF else "EOD_SQUARE_OFF_3_15PM"
                    await _square_off_all(reason)
                    squared_off_today_for.add(today_key)
                elif now < square_off_at:
                    positions = list(position_store.live_positions.items())
                    await asyncio.gather(
                        *[_check_one_position(sym, pos) for sym, pos in positions]
                    )
            elif dhan_wrapper.is_market_open():
                # No forced square-off today at all (Mon-Thu, both flags
                # off, or Friday-carve-out disabled) - keep evaluating every
                # live position's own exit conditions (target/SL/dynamic-SL/
                # Supertrend/MAX_LOSS_HIT) for as long as it takes, including
                # across a day boundary (see PositionStore.maybe_reset_for_
                # new_day, which deliberately does NOT clear live_positions
                # in this mode). Gated on is_market_open() rather than
                # running unconditionally, so this doesn't hammer Dhan's LTP
                # REST endpoint every MONITOR_INTERVAL_SECONDS for the ~17.75
                # hours the market is shut overnight - a position genuinely
                # gets zero exit protection during that window (the real
                # risk this mode accepts), and simply resumes being checked
                # the moment the next session's ticks start.
                positions = list(position_store.live_positions.items())
                await asyncio.gather(
                    *[_check_one_position(sym, pos) for sym, pos in positions]
                )
        except Exception:  # noqa: BLE001
            logger.exception("Error in monitor loop tick")

        await asyncio.sleep(config.MONITOR_INTERVAL_SECONDS)
