"""
Luxury strategy: Chartink -> Dhan ATM CE/PE buying, exiting on
target/stop-loss/dynamic-SL/Supertrend - a same-account duplicate of
Options/option_main.py (user request 31 Aug 2026: same logic/setup as
Options, CE+PE webhooks, own configuration). Reuses the Options package's
single authenticated Dhan connection (dhan_client.py here just re-exports
it), same as Futures/CopperOptions/IndexScalping already do - main.py
must mount this package's lifespan *inside* Options' own nesting (after
it), so authenticate() and start_feed() have already run by the time this
package's lifespan starts.

`lifespan` and `router` are composed into the shared app by the top-level
main.py, the same way every other strategy package is - see main.py's own
docstring.

Accepts Chartink scanner webhook alerts on two endpoints:
   - POST /chartink/webhook-luxury       (bullish scan -> buys ATM CE)
   - POST /chartink/webhook-luxury-sell  (bearish scan -> buys ATM PE)
Same entry/exit/dedup/capacity machinery either way, sharing one position
pool, entirely separate from Options'/Futures' own - only the ATM leg and
which end of the %change ranking counts as "strongest" differ, identical
design to Options' own two-endpoint pair.
  1. Picks the top-N stocks by today's %change from the alert (highest
     first for the bullish webhook, lowest/most negative first for the
     bearish one)
  2. Buys the ATM option for each, at market price (AMO if placed outside
     market hours)
  3. Runs a background monitor loop that exits a leg on target/stop-loss/
     trailing-SL/dynamic-SL/Supertrend/SQUARE_OFF_TIME - identical rules
     to Options', this package's own config values (config.py)
  4. Won't re-enter a symbol that already has an open or in-flight
     position of either type, and caps concurrent live positions
     separately per option type (config.MAX_LIVE_POSITIONS_CE/_PE) -
     entirely separate pool/capacity from Options'/Futures', so alerts on
     any side can't crowd out the others'.

Does NOT mount a paper-trade evaluation webhook (Options' own
/chartink/webhook-papertrade) and does NOT filter against choppy_stocks.py
(scoped to Options only per the user's own wording when that feature was
requested) - ask if either should be extended here too.
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
import paper_mode_control
import climactic_entry_guard
import reversal_filters

from . import config
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

logger = logging.getLogger("luxury_main")

router = APIRouter()

_monitor_task: Optional[asyncio.Task] = None
_breakout_signal_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Luxury strategy's own startup/shutdown. Does NOT authenticate or
    start the Dhan feed - it reuses Options' already-authenticated
    connection, so main.py must nest this lifespan inside Options' own
    (after it), the same pattern Futures/IndexScalping/CopperOptions use.

    DOES reconcile broker positions at startup - filtered through
    attribute_open_broker_position so it can never import a position
    that's actually Options'/Futures' own, since Dhan's own data can't
    tell strategies apart. See trading_engine.py's module docstring and
    reconcile_broker_positions' own docstring for the full mechanism."""
    global _monitor_task, _breakout_signal_task
    loop = asyncio.get_running_loop()

    def _on_price_tick(trading_symbol: str, ltp: float) -> None:
        asyncio.run_coroutine_threadsafe(on_price_tick(trading_symbol, ltp), loop)

    dhan_wrapper.add_price_tick_subscriber(_on_price_tick)

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
    # Breakout-signal scanner (20 Sep 2026, promoted to SOLE entry path 21
    # Sep 2026, user request) - see breakout_signal.py's own module
    # docstring. Calls _breakout_entry_fn below, which wraps the real
    # _process_one_entry with the same pre-entry window/gap-down checks
    # the webhook handler used to apply before ever reaching
    # enter_positions_for_stocks (now retired from that handler).
    _breakout_signal_task = asyncio.create_task(
        breakout_signal.signal_scanner_loop("Luxury", config, _breakout_entry_fn)
    )
    logger.info("Luxury strategy startup complete: monitor loop + breakout-signal scanner (SOLE entry path) running (reusing Options' Dhan connection).")
    yield
    if _monitor_task:
        _monitor_task.cancel()
    if _breakout_signal_task:
        _breakout_signal_task.cancel()


async def _breakout_entry_fn(symbol: str, option_type: str) -> dict:
    """Entry point breakout_signal.py calls once a signal is confirmed -
    THE only place a real Luxury entry now originates from (21 Sep 2026 -
    _handle_chartink_webhook above no longer calls enter_positions_for_
    stocks directly). Applies the SAME pre-entry gates that handler used
    to check before ranking/entering, then defers to the real, unmodified
    _process_one_entry for everything else (daily re-entry cap, loss-
    repeat block, cross-strategy claim, capacity + the opening-burst slot,
    liquid-contract resolution, funds check - all inherited automatically,
    nothing reimplemented here)."""
    if config.NIFTY_GAP_BLOCK_ENABLED and dhan_wrapper.should_block_all_entries_today():
        return {"symbol": symbol, "status": "skipped", "reason": "nifty_gap_down_block"}
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
        # climactic_entry_guard.py's own module docstring.
        return await climactic_entry_guard.guard_entry("Luxury", symbol, option_type, _resolve_entry)
    return await _resolve_entry(symbol, option_type)


async def _resolve_entry(symbol: str, option_type: str) -> dict:
    """The actual real-vs-paper dispatch, extracted out of
    _breakout_entry_fn (22 Sep 2026 paper-mode logic, unchanged) so
    climactic_entry_guard.guard_entry can call it either immediately
    (non-climactic alert) or later, with a possibly-different
    option_type, once a deferred alert's cooldown clears."""
    if paper_mode_control.is_paper_mode_enabled("Luxury"):
        # Paper-mode REPLACES real trading for this package (22 Sep 2026,
        # explicit user instruction; runtime-togglable since 24 Sep 2026
        # via POST /paper-mode - see breakout_paper_engine.py's own
        # docstring for both). Real entry never runs while this is true.
        return await breakout_paper_engine.process_paper_entry("Luxury", symbol, option_type)
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
    """Shared by both webhooks below. REWRITTEN 21 Sep 2026, then made
    CONDITIONAL the same day - see Options/option_main.py's identical
    function for the full rationale (same design here). Every alert is
    ALWAYS recorded into breakout_signal's watchlist first; what happens
    next depends on config.BREAKOUT_SIGNAL_ENABLED - True (default) queues
    only, letting the scanner decide; False falls through to
    _enter_directly_from_webhook, the restored pre-21-Sep-2026 direct
    ranked-entry path."""
    await position_store.maybe_reset_for_new_day()
    stocks = payload.stock_list()
    fire_and_forget(breakout_signal.record_alert("Luxury", option_type, stocks, cfg=config))

    if not config.BREAKOUT_SIGNAL_ENABLED:
        return await _enter_directly_from_webhook(payload, option_type, prefer_highest, stocks)

    logger.info(
        "Luxury webhook received (%s): scan=%s alert=%s stocks=%s - queued for the breakout-signal "
        "scanner (direct ranked entry retired 21 Sep 2026, see breakout_signal.py).",
        option_type, payload.scan_name, payload.alert_name, stocks,
    )
    fire_and_forget(record_webhook_alert(
        "Luxury", payload.scan_name, payload.alert_name, stocks, "queued_for_breakout_signal", None,
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
    explicit fallback for config.BREAKOUT_SIGNAL_ENABLED=False - see
    Options/option_main.py's identical function for the full rationale.
    Byte-for-byte the same logic this handler used before being rewritten
    to queue-only."""
    def _log_alert(status: str, reason: Optional[str] = None) -> None:
        fire_and_forget(record_webhook_alert(
            "Luxury", payload.scan_name, payload.alert_name, stocks, status, reason,
        ))

    if config.NIFTY_GAP_BLOCK_ENABLED and dhan_wrapper.should_block_all_entries_today():
        logger.info("Ignoring alert (%s) - Nifty50 gap-down block active for the whole trading day.", option_type)
        _log_alert("ignored", "nifty_gap_down_block")
        return {"status": "ignored", "reason": "nifty_gap_down_block"}

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
        "Luxury webhook received (%s): scan=%s alert=%s stocks=%s (direct entry - breakout signal scanner disabled)",
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
            "Luxury", payload.scan_name, option_type, stocks, [s for s, _ in ranked],
        ))

    asyncio.create_task(reversal_filters.log_ribbon_switch_shadow_for_alert(
        "Luxury", option_type, stocks, position_store,
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


@router.post("/chartink/webhook-luxury")
async def chartink_webhook_luxury(payload: ChartinkWebhookPayload):
    """Bullish scan - buys ATM CE (call) on the alerted stocks with the
    highest %change."""
    return await _handle_chartink_webhook(payload, option_type="CE", prefer_highest=True)


@router.post("/chartink/webhook-luxury-sell")
async def chartink_webhook_luxury_sell(payload: ChartinkWebhookPayload):
    """Bearish scan - buys ATM PE (put) on the alerted stocks with the
    lowest %change (biggest decliners). Same entry/exit/dedup/capacity
    machinery as /chartink/webhook-luxury, sharing the same position pool -
    a symbol already open from either webhook blocks the other from also
    entering it."""
    return await _handle_chartink_webhook(payload, option_type="PE", prefer_highest=False)


# --------------------------------------------------------------------------- #
# Observability endpoints
# --------------------------------------------------------------------------- #
@router.get("/luxury/positions")
async def get_positions():
    return await position_store.snapshot()


@router.get("/luxury/orders")
async def get_orders():
    """Every order placed today (entry BUY + exit SELL legs), with Dhan's
    own order_status."""
    snapshot = await position_store.snapshot()
    return {"orders": snapshot["orders_today"]}


@router.post("/luxury/square-off-now")
async def manual_square_off():
    """Manual kill-switch: closes every live Luxury position immediately."""
    from .trading_engine import _square_off_all  # local import to avoid cycles at module load
    await _square_off_all("MANUAL_SQUARE_OFF")
    return await position_store.snapshot()


@router.get("/luxury/breakout-signal")
async def get_breakout_signal_status():
    """Today's breakout-signal watchlist - which symbols are being
    watched, and which have already fired (at most one signal/symbol/day).
    Read-only, see breakout_signal.py's own module docstring."""
    return await breakout_signal.snapshot("Luxury")


@router.get("/luxury/climactic-guard/pending")
async def get_climactic_guard_pending():
    """Every alert currently deferred by the climactic-entry cooldown
    guard - read-only, see climactic_entry_guard.py's own module
    docstring."""
    return {"enabled": config.CLIMACTIC_GUARD_ENABLED, "pending": climactic_entry_guard.snapshot("Luxury")}
