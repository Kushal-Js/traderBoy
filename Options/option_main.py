"""
Options strategy: Chartink -> Dhan ATM CE/PE buying, exiting on
target/stop-loss/dynamic-SL/Supertrend. Everything in this file is
specific to *this* strategy - `lifespan` and `router` are composed into
the shared app by the top-level main.py, which can mount other,
non-options strategies alongside this one the same way (see main.py's
own docstring).

Accepts Chartink scanner webhook alerts on two endpoints:
   - POST /chartink/webhook       (bullish scan -> watches for ATM CE)
   - POST /chartink/webhook-sell  (bearish scan -> watches for ATM PE)

REWRITTEN 21 Sep 2026 (user request: "the breakout-signal scanner has to
be main entry path... webhook path will feed the signals to breakout-
signal scanner and it will decide which trades to be placed"). A raw
alert on either endpoint does NOT buy anything directly - it only records
the alerted symbols into breakout_signal.py's own watchlist (see that
module's docstring). A background scanner loop then re-checks each
not-yet-signaled symbol's real 5-min candles against its own 7-rule
breakout/breakdown confirmation, and ONLY THEN calls _breakout_entry_fn
(this file) -> the real, unmodified _process_one_entry - the actual real
trade origin now, for both CE and PE alike.

rank_and_pick_top_stocks / enter_positions_for_stocks still exist in
trading_engine.py, unmodified - kept, not deleted, same as this repo's
other retired-but-present code (see choppy_stocks.py) - but nothing
calls them anymore; a raw alert's %change ranking is no longer what
decides an entry.

  1. Exit logic is UNCHANGED - background monitor loop exits a leg on:
       - target / hard stop-loss (config.TARGET_PCT / STOP_LOSS_PCT)
       - continuous trailing stop-loss (trails the peak price in the
         trade's favor) - optional, see config.ENABLE_TRAILING_SL
       - stepped/"ratchet" stop-loss (every step % the option's own
         premium climbs from entry, the floor moves up
         DYNAMIC_SL_INCREASE_PCT; step width is per-leg -
         config.DYNAMIC_SL_STEP_PCT_CE / _PE) - optional, stacks with the
         continuous trailing stop above, see config.ENABLE_DYNAMIC_SL
       - the underlying's 5-min Supertrend turning against the position's
         direction - optional, see config.ENABLE_SUPERTREND_EXIT
       - config.SQUARE_OFF_TIME hard square-off of everything still open
  2. Won't re-enter a symbol that already has an open or in-flight
     position of either type, and caps concurrent live positions
     separately per option type - config.MAX_LIVE_POSITIONS_CE /
     MAX_LIVE_POSITIONS_PE - so a run of signals on one side can't crowd
     out capacity for the other. Once a position closes, its symbol is
     free to be entered again the same day (subject to
     MAX_DAILY_ENTRIES_PER_SYMBOL).

Also mounts POST /chartink/webhook-papertrade (paper_webhook.py) - a
third, independent Chartink endpoint for evaluating a new scan before
trusting it with real money. Bullish/CE only, entirely separate
position pool/capacity from the two real webhooks above, reuses this
same file's ranking/exit logic for fidelity, and never places a real
order - see paper_webhook.py's own module docstring for the safety
invariant and NOTES.md's design-decision entry for why it lives here
rather than as its own top-level package.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, field_validator

from trade_history import fire_and_forget, record_webhook_alert
import breakout_signal
import breakout_paper_engine
import choppy_stocks
import climactic_entry_guard
import reversal_filters

from . import config, paper_webhook
from . import trading_engine
from .dhan_client import dhan_wrapper
from .position_store import position_store
from .trading_engine import (
    enter_positions_for_stocks,
    is_past_allowed_trading_time,
    is_past_square_off_time,
    is_within_trading_windows,
    monitor_loop,
    on_price_tick,
    rank_and_pick_top_stocks,
    reconcile_broker_positions,
)

logger = logging.getLogger("option_main")

router = APIRouter()
router.include_router(paper_webhook.router)

_monitor_task: Optional[asyncio.Task] = None
_paper_monitor_task: Optional[asyncio.Task] = None
_breakout_signal_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Options strategy's own startup/shutdown - authenticates with Dhan,
    starts its market-data feed, reconciles any broker positions left
    open from a previous run, and starts this strategy's monitor loop.
    Composed into the shared app's lifespan by main.py."""
    global _monitor_task, _paper_monitor_task, _breakout_signal_task
    loop = asyncio.get_running_loop()

    # One-time bootstrap - writes the default choppy-stocks list to disk
    # ONLY if it doesn't already exist (first-ever deploy of this feature,
    # or a fresh server). No further scheduling: this list is now
    # maintained by hand - see choppy_stocks.py's own docstring.
    choppy_stocks.ensure_choppy_list_exists()

    # Bridges the market-feed's WebSocket thread back onto the event loop -
    # on_price_tick is a plain sync function called directly from that
    # thread (see dhan_client._on_market_tick), so it can't just `await`.
    # Wired up before start_feed() so no tick can arrive before this exists.
    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_price_tick)

    # authenticate() and start_feed() are blocking SDK calls; push them to a
    # worker thread rather than calling them directly on the lifespan
    # coroutine (a socket connection spinning up its own event loop
    # internally can't happen on a thread that already has uvicorn's event
    # loop running).
    await loop.run_in_executor(None, dhan_wrapper.authenticate)

    try:
        # A bad/misscoped token could otherwise hang startup on the socket
        # retrying; fail fast and run in REST-only (polling) mode instead.
        # All feed-reading call sites already fall back to REST when the
        # feed has no cached data.
        # This must happen before reconcile_broker_positions() below, since
        # that also touches the feed (to subscribe reconciled positions'
        # prices).
        await asyncio.wait_for(loop.run_in_executor(None, dhan_wrapper.start_feed), timeout=15)
    except Exception:  # noqa: BLE001
        logger.exception("Could not start Dhan WebSocket feed - continuing in REST-only mode.")

    try:
        reconciled = await reconcile_broker_positions()
        if reconciled:
            await position_store.reconcile_from_broker(reconciled)
            logger.info(
                "Reconciled %d existing broker position(s) at startup: %s",
                len(reconciled), [p.underlying_symbol for p in reconciled],
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not reconcile broker positions at startup - continuing without them.")

    _monitor_task = asyncio.create_task(monitor_loop())
    _paper_monitor_task = asyncio.create_task(paper_webhook.poll_loop())
    # Breakout-signal scanner, CE+PE, SOLE real entry path (21 Sep 2026,
    # user request) - see breakout_signal.py's own module docstring and
    # config.BREAKOUT_SIGNAL_ENABLED's own comment for the full history.
    # Calls _breakout_entry_fn below, which applies the same pre-entry
    # window/gap-down checks the webhook handler used to apply before
    # ever reaching enter_positions_for_stocks - loop itself always
    # starts regardless of config.BREAKOUT_SIGNAL_ENABLED (needed for the
    # watchlist's own daily refresh housekeeping either way, see
    # breakout_signal.signal_scanner_loop's own docstring) - only the
    # scanning/entry side is flag-gated.
    _breakout_signal_task = asyncio.create_task(
        breakout_signal.signal_scanner_loop("Options", config, _breakout_entry_fn)
    )
    logger.info("Options strategy startup complete: authenticated + monitor loop + paper-trade poll loop + "
                "breakout-signal scanner (CE+PE, SOLE entry path, enabled=%s) running.", config.BREAKOUT_SIGNAL_ENABLED)
    yield
    if _monitor_task:
        _monitor_task.cancel()
    if _paper_monitor_task:
        _paper_monitor_task.cancel()
    if _breakout_signal_task:
        _breakout_signal_task.cancel()


async def _breakout_entry_fn(symbol: str, option_type: str) -> dict:
    """Entry point breakout_signal.py calls once a signal is confirmed -
    THE only place a real Options entry now originates from (21 Sep 2026 -
    _handle_chartink_webhook below no longer calls enter_positions_for_
    stocks directly). Applies the SAME pre-entry gates that handler used
    to check before ranking/entering, then defers to the real, unmodified
    _process_one_entry for everything else (daily re-entry cap, RSI-gated
    loss re-entry block, loss-repeat block, volume-floor gate, cross-
    strategy claim, capacity, liquid-contract resolution, funds check -
    all inherited automatically, nothing reimplemented here). Called for
    both CE and PE."""
    if not is_within_trading_windows():
        return {"symbol": symbol, "status": "skipped", "reason": "outside_trading_windows"}
    if is_past_allowed_trading_time():
        return {"symbol": symbol, "status": "skipped", "reason": "past_allowed_trading_time"}
    if is_past_square_off_time():
        return {"symbol": symbol, "status": "skipped", "reason": "past_square_off_time"}
    if option_type == "CE" and config.ENABLE_GAP_DOWN_CE_DELAY and dhan_wrapper.should_delay_ce_entry():
        return {"symbol": symbol, "status": "skipped", "reason": "nifty_gap_down_ce_delay"}
    if config.CLIMACTIC_GUARD_ENABLED:
        # Gates BOTH branches below (real and paper) - see
        # climactic_entry_guard.py's own module docstring. Placed after
        # the gap-down-CE gate deliberately: that one can hand back a
        # DIFFERENT (still valid) reason to skip a CE alert outright,
        # which should still short-circuit before this heavier check.
        return await climactic_entry_guard.guard_entry("Options", symbol, option_type, _resolve_entry)
    return await _resolve_entry(symbol, option_type)


async def _resolve_entry(symbol: str, option_type: str) -> dict:
    """The actual real-vs-paper dispatch, extracted out of
    _breakout_entry_fn (22 Sep 2026 paper-mode logic, unchanged) so
    climactic_entry_guard.guard_entry can call it either immediately
    (non-climactic alert) or later, with a possibly-different
    option_type, once a deferred alert's cooldown clears."""
    if breakout_paper_engine.is_paper_mode_enabled("Options"):
        # Paper-mode REPLACES real trading for this package (22 Sep 2026,
        # explicit user instruction; runtime-togglable since 24 Sep 2026
        # via POST /paper-mode - see breakout_paper_engine.py's own
        # docstring for both). Real entry never runs while this is true.
        return await breakout_paper_engine.process_paper_entry("Options", symbol, option_type)
    return await trading_engine._process_one_entry(symbol, option_type)


# --------------------------------------------------------------------------- #
# Webhook payload schema (matches the sample Chartink payload exactly)
# --------------------------------------------------------------------------- #
class ChartinkWebhookPayload(BaseModel):
    stocks: str
    trigger_prices: str
    triggered_at: str
    scan_name: str
    scan_url: str
    alert_name: str
    webhook_url: Optional[str] = None

    @field_validator("stocks")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("stocks must not be empty")
        return v

    def stock_list(self) -> list[str]:
        return [s.strip().upper() for s in self.stocks.split(",") if s.strip()]

    def trigger_price_list(self) -> list[float]:
        out = []
        for p in self.trigger_prices.split(","):
            p = p.strip()
            if p:
                try:
                    out.append(float(p))
                except ValueError:
                    out.append(0.0)
        return out


# --------------------------------------------------------------------------- #
# Webhook endpoints
# --------------------------------------------------------------------------- #
async def _handle_chartink_webhook(
    payload: ChartinkWebhookPayload, option_type: str, prefer_highest: bool,
) -> dict:
    """Shared by both webhooks below. REWRITTEN 21 Sep 2026 (user request:
    "the breakout-signal scanner has to be main entry path... webhook path
    will feed the signals to breakout-signal scanner and it will decide
    which trades to be placed"), then made CONDITIONAL the same day
    (follow-up user request: a way to turn this off per-package via a
    flag, without losing the ability to run it). Every alert is ALWAYS
    recorded into breakout_signal's watchlist first (cheap bookkeeping,
    harmless either way). What happens next depends on
    config.BREAKOUT_SIGNAL_ENABLED:
      - True (the default): queue only - the breakout-signal scanner
        decides, independently, whether/when to actually enter, per
        _breakout_entry_fn below. This is the 21 Sep 2026 behavior.
      - False: falls through to _enter_directly_from_webhook, the
        restored pre-21-Sep-2026 direct ranked-entry path (byte-for-byte
        the same logic this handler used before that rewrite) - a real
        Chartink alert bypasses the breakout scanner's filtering
        entirely and can place a real order immediately, exactly as it
        always did before today."""
    await position_store.maybe_reset_for_new_day()
    stocks = payload.stock_list()
    fire_and_forget(breakout_signal.record_alert("Options", option_type, stocks, cfg=config))

    if not config.BREAKOUT_SIGNAL_ENABLED:
        return await _enter_directly_from_webhook(payload, option_type, prefer_highest, stocks)

    logger.info(
        "Webhook received (%s): scan=%s alert=%s stocks=%s - queued for the breakout-signal scanner "
        "(direct ranked entry retired 21 Sep 2026, see breakout_signal.py).",
        option_type, payload.scan_name, payload.alert_name, stocks,
    )
    fire_and_forget(record_webhook_alert(
        "Options", payload.scan_name, payload.alert_name, stocks, "queued_for_breakout_signal", None,
    ))
    return {
        "status": "queued_for_breakout_signal",
        "option_type": option_type,
        "stocks": stocks,
        "breakout_signal_enabled": config.BREAKOUT_SIGNAL_ENABLED,
    }


async def _enter_directly_from_webhook(
    payload: ChartinkWebhookPayload, option_type: str, prefer_highest: bool, stocks: list[str],
) -> dict:
    """The pre-21-Sep-2026 direct ranked-entry path, restored as an
    explicit fallback for config.BREAKOUT_SIGNAL_ENABLED=False (see that
    flag's own docstring in config.py and _handle_chartink_webhook's own
    docstring above). Byte-for-byte the same logic this handler used
    before being rewritten to queue-only - trading-window/cutoff/square-
    off checks, gap-down CE delay, capacity, ranking, entry - restored,
    not reimplemented differently. Only the alert-recording step (now
    done by the caller before this is reached) and the function boundary
    itself changed."""
    def _log_alert(status: str, reason: Optional[str] = None) -> None:
        fire_and_forget(record_webhook_alert(
            "Options", payload.scan_name, payload.alert_name, stocks, status, reason,
        ))

    if not is_within_trading_windows():
        logger.info(
            "Ignoring alert (%s) - outside today's allowed trading windows (%s), not opening new positions.",
            option_type, config.TRADING_WINDOWS,
        )
        _log_alert("ignored", "outside_trading_windows")
        return {
            "status": "ignored",
            "reason": "outside_trading_windows",
            "trading_windows": config.TRADING_WINDOWS,
        }

    if is_past_allowed_trading_time():
        logger.info(
            "Ignoring alert (%s) - past today's allowed trading cutoff (%s), not opening new positions.",
            option_type, config.ALLOWED_TRADING_TIME,
        )
        _log_alert("ignored", "past_allowed_trading_time")
        return {
            "status": "ignored",
            "reason": "past_allowed_trading_time",
            "allowed_trading_time": config.ALLOWED_TRADING_TIME,
        }

    if is_past_square_off_time():
        logger.info(
            "Ignoring alert (%s) - past today's %s square-off time, not opening new positions.",
            option_type, config.SQUARE_OFF_TIME,
        )
        _log_alert("ignored", "past_square_off_time")
        return {
            "status": "ignored",
            "reason": "past_square_off_time",
            "square_off_time": config.SQUARE_OFF_TIME,
        }

    logger.info(
        "Webhook received (%s): scan=%s alert=%s stocks=%s (direct entry - breakout signal scanner disabled)",
        option_type, payload.scan_name, payload.alert_name, stocks,
    )

    if option_type == "CE" and config.ENABLE_GAP_DOWN_CE_DELAY and dhan_wrapper.should_delay_ce_entry():
        cond = dhan_wrapper.evaluate_nifty_open_condition()
        logger.info(
            "Ignoring CE alert - Nifty50 gap-down/sharp-fall cool-off active until %s "
            "(gap=%.1f pts, fall=%.2f%% from open).",
            cond["delay_until"].strftime("%H:%M"), cond["gap_points"], cond["fall_pct"],
        )
        _log_alert("ignored", "nifty_gap_down_ce_delay")
        return {
            "status": "ignored",
            "reason": "nifty_gap_down_ce_delay",
            "nifty_open_condition": {k: v for k, v in cond.items() if k != "date"},
        }

    cap = config.MAX_LIVE_POSITIONS_CE if option_type == "CE" else config.MAX_LIVE_POSITIONS_PE
    remaining = await position_store.remaining_capacity(option_type)
    if remaining == 0:
        logger.info("No %s capacity left (%s live/in-flight already) - ignoring alert.", option_type, cap)
        _log_alert("ignored", "max_live_positions_reached")
        return {
            "status": "ignored",
            "reason": "max_live_positions_reached",
            "option_type": option_type,
            "max_live_positions": cap,
        }

    loop = asyncio.get_running_loop()
    if prefer_highest and config.RIBBON_RANKING_ENABLED:
        ranked = await reversal_filters.rank_by_ribbon_expansion(stocks, config.TOP_N_STOCKS)
    elif not prefer_highest and config.RIBBON_RANKING_PE_ENABLED:
        ranked = await reversal_filters.rank_by_ribbon_breakdown(stocks, config.TOP_N_STOCKS)
    else:
        ranked = await loop.run_in_executor(
            None, rank_and_pick_top_stocks, stocks, config.TOP_N_STOCKS, prefer_highest
        )

    if len(stocks) > 1:
        asyncio.create_task(reversal_filters.log_alert_candidates(
            "Options", payload.scan_name, option_type, stocks, [s for s, _ in ranked],
        ))

    asyncio.create_task(reversal_filters.log_ribbon_switch_shadow_for_alert(
        "Options", option_type, stocks, position_store,
    ))

    if not ranked:
        _log_alert("no_action", "could_not_rank_any_stock")
        return {"status": "no_action", "reason": "could_not_rank_any_stock"}

    results = await enter_positions_for_stocks(ranked, option_type)
    _log_alert("processed")

    return {
        "status": "processed",
        "ranked_by_day_change_pct": ranked,
        "entries": results,
    }


@router.post("/chartink/webhook")
async def chartink_webhook(payload: ChartinkWebhookPayload):
    """Bullish scan - buys ATM CE (call) on the alerted stocks with the
    highest %change."""
    return await _handle_chartink_webhook(payload, option_type="CE", prefer_highest=True)


@router.post("/chartink/webhook-sell")
async def chartink_webhook_sell(payload: ChartinkWebhookPayload):
    """Bearish scan - buys ATM PE (put) on the alerted stocks with the
    lowest %change (biggest decliners). Same entry/exit/dedup/capacity
    machinery as /chartink/webhook, sharing the same position pool - a
    symbol already open from either webhook blocks the other from also
    entering it."""
    return await _handle_chartink_webhook(payload, option_type="PE", prefer_highest=False)


# --------------------------------------------------------------------------- #
# Observability endpoints
# --------------------------------------------------------------------------- #
@router.get("/positions")
async def get_positions():
    return await position_store.snapshot()


@router.get("/orders")
async def get_orders():
    """Every order placed today (entry BUY + exit SELL legs), with Dhan's
    own order_status (e.g. REJECTED, TRADED, CANCELLED - see
    dhan_client.OrderStatus for the full documented enum)."""
    snapshot = await position_store.snapshot()
    return {"orders": snapshot["orders_today"]}


@router.get("/feed-stats")
async def feed_stats():
    """Proves (or disproves) whether the WebSocket caches are actually
    being used instead of REST fallbacks - see dhan_client.DhanWrapper.stats."""
    return dhan_wrapper.stats


@router.get("/breakout-signal")
async def get_breakout_signal_status():
    """Today's CE/PE breakout-signal watchlists - which symbols are being
    watched, and which have already fired (at most one signal/symbol/day).
    Read-only, see breakout_signal.py's own module docstring."""
    return await breakout_signal.snapshot("Options")


@router.get("/climactic-guard/pending")
async def get_climactic_guard_pending():
    """Every alert currently deferred by the climactic-entry cooldown
    guard (RSI-extreme + high-ER) - read-only, see climactic_entry_guard.py's
    own module docstring. Empty list whenever config.CLIMACTIC_GUARD_ENABLED
    is false or nothing is currently climactic."""
    return {"enabled": config.CLIMACTIC_GUARD_ENABLED, "pending": climactic_entry_guard.snapshot("Options")}


@router.post("/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live position immediately."""
    from .trading_engine import _square_off_all  # local import to avoid cycles at module load
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()
