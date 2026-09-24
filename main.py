"""
Shared entry point across all trading strategies. Each strategy owns its
own package (its own lifespan, its own FastAPI router, its own state) -
this file just composes them onto one app so they can run side by side
in the same process. Three are mounted today:
  - Options/option_main.py - the live options-buying strategy (real
    orders, real money).
  - IndexScalping/index_main.py - a NIFTY/BankNifty scalping strategy,
    PAPER TRADING ONLY (see IndexScalping/paper_engine.py's safety
    invariant) - runs its own signal/exit logic and logs what it would
    have done, places no real orders.
  - Futures/futures_main.py - PLACEHOLDER strategy (buys ATM CE options
    via the identical mechanics as Options/, standing in until real
    futures-contract buying replaces it, by explicit request), REAL
    orders, own separate position pool/capacity - see
    Futures/trading_engine.py's module docstring for why it skips broker
    reconciliation at startup.
  - K01/screener_main.py - "K01", the daily F&O stock screener (Minervini
    Trend Template + liquidity floor, run once/day, feeding intraday
    Supertrend/RSI/ROC momentum entries), PAPER TRADING ONLY (see
    K01/paper_engine.py's safety invariant). Named/documented 30 Aug 2026
    (was FnoScreener/ until this rename - no trade history existed yet to
    migrate). MVP scope shipped the same day for first live test - full
    design in the separate trading-skills repo (designs/k01.md); OI-buildup
    gating and VCP detection are
    explicit phase-2 items, not yet built.
  - Luxury/luxury_main.py - user request 31 Aug 2026: a same-account
    duplicate of Options (same ranking/ATM-buying/exit logic, own CE+PE
    webhooks, own separate position pool/capacity/config), REAL orders -
    see Luxury/trading_engine.py's module docstring for what it does and
    doesn't share with Options (reuses the one Dhan connection; does NOT
    share choppy_stocks.py filtering or the paper-trade evaluation
    webhook, both scoped to Options only).
  - Swing/swing_main.py - user request 31 Aug 2026: buys 1 lot of a
    stock's FUTURES contract hedged with 1 lot of its ATM PE option, as
    an all-or-nothing "basket" (Dhan has no native basket-order API - see
    Swing/trading_engine.py's own module docstring for the compensating-
    rollback design this uses instead). Entry/exit CONDITION logic is
    deliberately deferred to the user - see Swing/config.py's own
    docstring. REAL orders, own separate basket capacity/config,
    DEPLOYED DISABLED (config.STRATEGY_ENABLED=false) until that logic is
    defined. The first package in this codebase to trade an actual
    futures contract (Options/dhan_client.py's new get_futures_contract())
    rather than buying an ATM option as a placeholder for one.
  - Paper01/paper01_main.py - user request 15 Sep 2026: a real-time,
    paper-only twin of the Options strategy - exact same entry/exit rules
    (reuses Options' own ranking/exit-ladder/Position code directly, see
    Paper01/trading_engine.py's own docstring), own CE+PE webhooks, own
    separate paper-only position pool/capacity, PAPER TRADING ONLY (see
    Paper01/config.py's safety invariant) - never places a real order.
An eighth strategy would be added the same way - its own package,
exporting `router` + `lifespan`, mounted below - without touching any
existing one.

Run with:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, date as date_cls
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from pydantic import BaseModel

from trade_history import HISTORY_DIR, read_all_jsonl, read_all_trades, read_all_webhook_alerts
import choppy_stocks
import cross_strategy_registry
import fund_allocation
from Options import option_main
from Options import config as options_config
from IndexScalping import index_main
from Futures import futures_main
from Futures import config as futures_config
from K01 import screener_main
from Luxury import luxury_main
from Luxury import config as luxury_config
from Swing import swing_main, swing_paper_engine
from Paper01 import paper01_main
import universe_bucket
import breakout_signal
import breakout_paper_engine
import underlying_candle_feed
from Options.dhan_client import dhan_wrapper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

# Every strategy's blocking Dhan/Tradehull calls (order placement, ATM/
# futures resolution, margin checks, LTP REST fallback, position
# reconciliation) go through `loop.run_in_executor(None, ...)`, which
# without this would fall back to Python's default pool sized
# min(32, cpu_count()+4) - just 5 threads on the droplet's 1 vCPU, shared
# across ALL FOUR live strategies (Options/Futures/Luxury/Swing) at once.
# Raised 13 Sep 2026 (user-requested, after identifying this as the real
# bottleneck under simultaneous multi-stock/multi-strategy entries -
# TOP_N_STOCKS=4 stocks already enter concurrently via asyncio.gather,
# which alone can nearly saturate 5 threads) - these are I/O-bound waits
# on Dhan's API, not CPU-bound work, so more threads than vCPUs is safe
# and doesn't compete for the box's limited CPU the way more concurrent
# computation would. Does NOT help with Dhan's own broker-side rate
# limits (a separate, unaffected risk) - this only relieves OUR OWN
# internal queuing.
#
# First deployed 13 Sep 2026, reverted the same day after a WebSocket
# reconnect storm immediately following restart - but the market was
# CLOSED that day (Sunday), which turned out to be a large confound (see
# trading-skills/incidents/2026-09-13-executor-sizing-ws-storm-on-closed-
# market.md for the full writeup): a dead feed with no ticks flowing
# looks nearly identical to a WS regression from the outside, so that
# restart's instability was never a clean signal either way. Re-attempted
# 15 Sep 2026 (user go-ahead, deliberately deployed and observed during
# live market hours this time, not a closed-market restart) specifically
# so a real before/after WS-health comparison is actually possible.
EXECUTOR_MAX_WORKERS = int(os.getenv("EXECUTOR_MAX_WORKERS", "10"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Combines every mounted strategy's own lifespan. Add a new
    strategy's context manager to this stack the same way to bring its
    startup/shutdown along without touching the others. Options' lifespan
    runs first since IndexScalping/Futures reuse its already-authenticated
    Dhan connection (see IndexScalping/paper_engine.py's and
    Futures/futures_main.py's docstrings) - keep it first in this nesting
    if more strategies are added later that also depend on it.

    The executor is sized BEFORE any strategy's lifespan starts, since
    Options' own lifespan authenticates against Dhan immediately and that
    already goes through run_in_executor."""
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=EXECUTOR_MAX_WORKERS)
    )
    logger.info("Default executor sized to max_workers=%d", EXECUTOR_MAX_WORKERS)
    async with option_main.lifespan(app):
        async with index_main.lifespan(app):
            async with futures_main.lifespan(app):
                async with screener_main.lifespan(app):
                    async with luxury_main.lifespan(app):
                        async with swing_main.lifespan(app):
                            async with paper01_main.lifespan(app):
                                dispatcher_task = None
                                if options_config.UNIVERSE_DISPATCHER_ENABLED:
                                    # Started here, not inside any one
                                    # package's own lifespan, since it
                                    # spans two of them - see breakout_
                                    # signal.py's own dispatcher-section
                                    # docstring. By this point every
                                    # nested lifespan above has already
                                    # run, so Luxury's/Futures' own
                                    # _breakout_entry_fn are ready to call.
                                    # CE keeps its existing Luxury/Futures-only, rotating-for-
                                    # fairness split (unchanged 23 Sep 2026). PE gets its OWN
                                    # target list (added 23 Sep 2026, user request, after
                                    # discovering universe_bucket's PE bucket was already being
                                    # live-dispatched to Luxury/Futures with no PE-focused
                                    # destination at all) - Options FIRST (it's an
                                    # options-trading strategy, the natural home for a
                                    # PE/bearish signal), Futures/Luxury only as capacity
                                    # fallback behind it - see breakout_signal.py's own
                                    # _ROTATE_OPTION_TYPES comment for why PE is fixed-order,
                                    # never rotated, unlike CE.
                                    dispatcher_task = asyncio.create_task(breakout_signal.universe_dispatcher_loop({
                                        "CE": [
                                            ("Luxury", luxury_config, luxury_main._breakout_entry_fn),
                                            ("Futures", futures_config, futures_main._breakout_entry_fn),
                                        ],
                                        "PE": [
                                            ("Options", options_config, option_main._breakout_entry_fn),
                                            ("Futures", futures_config, futures_main._breakout_entry_fn),
                                            ("Luxury", luxury_config, luxury_main._breakout_entry_fn),
                                        ],
                                    }))
                                    logger.info("UniverseDispatcher task started (CE: Luxury+Futures, PE: Options>Futures>Luxury).")
                                # Started unconditionally (cheap no-op when no
                                # package has BREAKOUT_PAPER_MODE_ENABLED on -
                                # see breakout_paper_engine.py's own docstring),
                                # same reasoning as the dispatcher_task above:
                                # it spans Options/Luxury/Futures, so it can't
                                # live inside any one package's own lifespan.
                                paper_engine_task = asyncio.create_task(breakout_paper_engine.paper_engine_monitor_loop())
                                # Swing's own paper-mode kill switch (23 Sep
                                # 2026, user request) - separate task/module
                                # from the one above since Swing has its own
                                # single-strategy paper engine, not a
                                # dispatch-table one (see swing_paper_
                                # engine.py's own docstring). Same "cheap
                                # no-op when config.PAPER_MODE_ENABLED is
                                # off" reasoning.
                                swing_paper_engine_task = asyncio.create_task(swing_paper_engine.paper_engine_monitor_loop())
                                try:
                                    yield
                                finally:
                                    if dispatcher_task:
                                        dispatcher_task.cancel()
                                    paper_engine_task.cancel()
                                    swing_paper_engine_task.cancel()


app = FastAPI(title="Chartink -> Dhan Algo Bot", lifespan=lifespan)
app.include_router(option_main.router)
app.include_router(index_main.router)
app.include_router(futures_main.router)
app.include_router(screener_main.router)
app.include_router(luxury_main.router)
app.include_router(swing_main.router)
app.include_router(paper01_main.router)
app.include_router(universe_bucket.router)


# --------------------------------------------------------------------------- #
# Endpoints common to every strategy (not specific to options)
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/incidents")
async def get_incidents(limit: int = 20):
    """Records from watchdog.py - a separate process/systemd unit that
    polls /health independently of this app (so it can see this app being
    down) and logs any outage past its threshold, including the actual
    dhanboy.service journal output for that window. Exists because
    journald's own retention is limited and a restart that lands in a
    transient failure (e.g. a Dhan auth blip) can otherwise self-heal via
    systemd's Restart=always and leave no lasting trace.

    Reads every history/<date>_incidents.log file (dated-history
    convention, 31 Aug 2026 - see trade_history.py/watchdog.py), not one
    fixed path, so incidents from any prior day are still visible here.
    Returns the most recent `limit` incidents, newest first."""
    incidents: list[str] = []
    for path in sorted(HISTORY_DIR.glob("*_incidents.log")):
        try:
            with open(path) as f:
                content = f.read()
        except FileNotFoundError:
            continue
        blocks = [b.strip() for b in content.split("=== INCIDENT ") if b.strip()]
        incidents.extend("=== INCIDENT " + b for b in blocks)
    if not incidents:
        return {"incidents": [], "note": "no incidents recorded yet"}
    return {"incidents": list(reversed(incidents))[:limit], "total_recorded": len(incidents)}


@app.get("/trade-history")
async def trade_history(strategy: str | None = None):
    """Persistent, cross-restart record of every REAL (non-paper) closed
    trade, tagged by which package placed it - see trade_history.py.
    Options/position_store.py, Futures/position_store.py,
    Luxury/position_store.py, and Swing/position_store.py's own daily
    closed-trade logs all reset daily and don't survive a restart; this
    is the durable record for later analysis. strategy=None returns all
    four; pass strategy=Options, Futures, Luxury, or Swing to filter to
    one (Swing's own records tag each basket's two legs separately -
    option_type "FUT" for the futures leg, "PE" for the option leg)."""
    if strategy is not None and strategy not in ("Options", "Futures", "Luxury", "Swing"):
        return {"error": "strategy must be 'Options', 'Futures', 'Luxury', or 'Swing' (or omitted for all)"}
    trades = read_all_trades(strategy)
    return {"count": len(trades), "trades": trades}


@app.get("/paper-trades")
async def get_paper_trades(strategy: str | None = None):
    """Consolidated view of breakout-scanner paper trading across
    Options/Futures/Luxury (added 24 Sep 2026, user request: "I should
    see a consolidated view" - closed-only via history/<date>_breakout_
    paper_trades.log left OPEN paper positions invisible between entry
    and exit, the exact gap this closes). Combines:
      - "open": every currently-open paper position, live from
        breakout_paper_engine.snapshot() (in-memory - NOT the durable
        record, just what's live right now);
      - "closed": every closed paper trade ever recorded, from
        history/*_breakout_paper_trades.log (durable, survives a
        restart - read_all_jsonl reads every dated file, not just
        today's, same convention /trade-history already uses for real
        trades).
    strategy=None returns all 3 packages; pass strategy=Options/Futures/
    Luxury to filter both halves to one. Swing's own separate paper
    engine (swing_paper_engine.py, index-only, gated by SWING_INDEX_
    PAPER_MODE_ENABLED - currently false) is NOT included here; its own
    trades log to history/<date>_swing_paper_trades.log directly if/when
    that's ever turned back on."""
    if strategy is not None and strategy not in ("Options", "Futures", "Luxury"):
        return {"error": "strategy must be 'Options', 'Futures', or 'Luxury' (or omitted for all)"}
    open_positions = await breakout_paper_engine.snapshot()
    if strategy is not None:
        open_positions = {k: v for k, v in open_positions.items() if k.startswith(f"{strategy}:")}
    closed = read_all_jsonl(breakout_paper_engine.PAPER_TRADES_LOG_NAME)
    if strategy is not None:
        closed = [t for t in closed if t.get("strategy") == strategy]
    return {"open_count": len(open_positions), "open": open_positions,
            "closed_count": len(closed), "closed": closed}


@app.get("/webhook-alerts")
async def webhook_alerts(strategy: str | None = None):
    """Every incoming Chartink alert (processed AND ignored, with why),
    tagged by which endpoint received it - see trade_history.py's
    record_webhook_alert. Each handler already logged receipt via the
    standard logger, but that only reaches journald (limited retention,
    not queryable) - this is the durable, structured record. strategy=None
    returns all; pass strategy=Options/Futures/Luxury/Swing/Swing-Watchlist/
    Options-PaperTrade to filter to one."""
    if strategy is not None and strategy not in (
        "Options", "Futures", "Luxury", "Swing", "Swing-Watchlist", "Options-PaperTrade", "Paper01",
    ):
        return {"error": "strategy must be 'Options', 'Futures', 'Luxury', 'Swing', 'Swing-Watchlist', "
                          "'Options-PaperTrade', or 'Paper01' (or omitted for all)"}
    alerts = read_all_webhook_alerts(strategy)
    return {"count": len(alerts), "alerts": alerts}


@app.get("/choppy-stocks")
async def choppy_stocks_list():
    """Stocks the Options strategy currently won't enter new positions in -
    a manually-maintained list (user request 31 Aug 2026), edited directly
    on the server at choppy/choppy_stocks.json, not auto-computed or
    auto-refreshed. See choppy_stocks.py's own docstring for how to edit
    it and when an edit takes effect."""
    data = choppy_stocks.read_choppy_list()
    if data is None:
        return {"stocks": [],
                "note": "No choppy-stocks list on disk yet - nothing is being excluded in the meantime (fails open)."}
    return data


@app.get("/entry-claims")
async def entry_claims():
    """Underlyings currently mid-entry-attempt, and which of Options/
    Futures/Luxury holds the claim - see cross_strategy_registry.py (user
    request 31 Aug 2026). Normally empty or near-empty - a claim only
    exists for the brief window between a webhook accepting an alert and
    that stock's order placement/fill resolving, not for the life of an
    open position. A non-empty entry that persists across repeated calls
    would indicate a claim never got released (a bug in the try/finally
    wrapping, not expected behavior) - see this module's own docstring."""
    return {"claims": cross_strategy_registry.snapshot()}


@app.get("/funds/buckets")
async def funds_buckets():
    """The 2-bucket fund allocation system's own current state (user
    request 1 Sep 2026, see fund_allocation.py's own module docstring) -
    the account's real total available balance, and each bucket's own
    live computed share of it: "primary" (Swing's own basket/basket_
    hedge/sequential entries) and "secondary" (Options/Futures/Luxury's
    own single-leg entries, shared). Both percentages are configurable
    (FUND_PRIMARY_BUCKET_PCT/FUND_SECONDARY_BUCKET_PCT) - this endpoint
    is the quickest way to see the actual Rupee amount each currently
    resolves to, and to catch a misconfiguration (the two not summing to
    100%) without doing the arithmetic by hand."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fund_allocation.snapshot)


# --------------------------------------------------------------------------- #
# WS-candle-reconstruction observation endpoints (added 21 Sep 2026, user
# request - see trading-skills' designs/ws-candle-reconstruction-parity-
# results.md). Deliberately DECOUPLED from BREAKOUT_USE_WS_CANDLES (which
# stays false for all 3 packages) - subscribing a symbol here has zero
# effect on any real entry decision. Purpose: let real WS ticks accumulate
# for underlying_candle_feed.py's own bar reconstruction during a live
# market session, so its output can be diffed against real REST candles
# fetched separately after the fact - the only way to settle whether the
# reconstructed OPEN price (the one metric the REST-replay parity backtest
# structurally cannot validate) matches a genuine live tick stream, as
# opposed to the REST-replay's own 1-min-close-as-tick proxy.
# --------------------------------------------------------------------------- #
class UnderlyingFeedSubscribeRequest(BaseModel):
    symbols: list[str]


@app.post("/debug/underlying-feed/subscribe")
async def underlying_feed_subscribe(payload: UnderlyingFeedSubscribeRequest):
    """Subscribes the given symbols' underlying equities on the live
    bot's own already-authenticated market-data WebSocket (Quote mode,
    the same shared connection Options/Luxury/Futures already use for
    option LTP) so underlying_candle_feed.py starts reconstructing real
    5-min bars for them from here on. No new session, no order placed,
    no effect on any package's real entry logic - purely additive
    observation. See this section's own module-level comment."""
    symbols = [s.strip().upper() for s in payload.symbols if s.strip()]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, underlying_candle_feed.subscribe, symbols)
    return {"subscribed": symbols}


@app.get("/debug/underlying-feed/snapshot")
async def underlying_feed_snapshot():
    """Read-only: every symbol subscribed via the endpoint above, its
    completed-bar count, and how long ago its last real tick arrived -
    see underlying_candle_feed.snapshot()'s own docstring."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, underlying_candle_feed.snapshot)


@app.get("/debug/underlying-feed/candles/{symbol}")
async def underlying_feed_candles(symbol: str):
    """Read-only: this symbol's completed 5-min bars as reconstructed
    from real WS ticks so far today - the SAME dict-of-lists shape a REST
    intraday_minute_data call returns, so it can be diffed directly
    against one fetched separately after market close for the same
    symbol/day."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, underlying_candle_feed.get_candles_dict, symbol.strip().upper())
    return {"symbol": symbol.strip().upper(), "candles": data}


_UNDERLYING_FEED_IST = ZoneInfo("Asia/Kolkata")


def _fetch_rest_5m_candles_sync(symbol: str, day: date_cls) -> dict:
    """Real REST 5-min candles via the LIVE bot's own already-authenticated
    connection - deliberately NOT a separate local script, so this needs no
    hand-off access_token and carries zero session-collision risk (see
    incidents/2026-09-21-local-backtest-dhan-session-collision.md). Same
    call shape backtest_ws_candle_reconstruction_parity.py's own
    fetch_real_5m_candles uses, just routed through the bot's own
    dhan_wrapper instead of a competing local session.

    Retries with backoff (same discipline the standalone parity script
    needed - added after ICICIBANK/SBIN/ITC transiently 500'd on the
    first real run of ws_candle_parity_check.py, back-to-back with zero
    pacing, a textbook DH-904-shaped rate-limit blip): Dhan's REST
    endpoint intermittently returns a bare failure envelope under back-
    to-back load, not something a single attempt should ever trust."""
    import time
    sec_id = dhan_wrapper._equity_security_id(symbol)
    delay = 3.0
    last_exc: Optional[Exception] = None
    for attempt in range(4):
        try:
            resp = dhan_wrapper.client.Dhan.intraday_minute_data(
                security_id=sec_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
                from_date=day.isoformat(), to_date=day.isoformat(), interval=5,
            )
            if not isinstance(resp, dict) or resp.get("status") != "success":
                raise RuntimeError(f"REST fetch failed for {symbol} {day}: {resp}")
            return resp.get("data") or {}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 3:
                logger.warning("_fetch_rest_5m_candles_sync(%s, %s) attempt %d/4 failed: %s - retrying in %.0fs",
                                symbol, day, attempt + 1, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, 15.0)
    raise last_exc


@app.get("/debug/underlying-feed/rest-candles/{symbol}")
async def underlying_feed_rest_candles(symbol: str, day: str | None = None):
    """Real REST 5-min candles for `symbol` on `day` (default: today,
    IST) - the ground-truth side of the WS-candle parity comparison,
    fetched through the live bot's own connection (see
    _fetch_rest_5m_candles_sync's own docstring for why this matters)."""
    d = date_cls.fromisoformat(day) if day else datetime.now(_UNDERLYING_FEED_IST).date()
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _fetch_rest_5m_candles_sync, symbol.strip().upper(), d)
    return {"symbol": symbol.strip().upper(), "day": d.isoformat(), "candles": data}


@app.get("/debug/underlying-feed/parity/{symbol}")
async def underlying_feed_parity(symbol: str, day: str | None = None):
    """The full WS-vs-REST comparison for `symbol` on `day` (default:
    today), computed server-side against the bot's own real WS-
    reconstructed bars and its own real REST fetch - same tolerance
    thresholds as backtest_ws_candle_reconstruction_parity.py (close
    <0.05%, volume <1%, open+close exact <0.01) for a directly comparable
    result. Unlike that backtest (which replays 1-min REST closes as a
    proxy tick stream), this compares against GENUINE live WS ticks -
    the comparison this whole investigation has been building toward.
    See trading-skills' designs/ws-candle-reconstruction-parity-results.md."""
    sym = symbol.strip().upper()
    d = date_cls.fromisoformat(day) if day else datetime.now(_UNDERLYING_FEED_IST).date()
    loop = asyncio.get_running_loop()
    real = await loop.run_in_executor(None, _fetch_rest_5m_candles_sync, sym, d)
    recon = await loop.run_in_executor(None, underlying_candle_feed.get_candles_dict, sym)

    r_ts, r_o, r_h, r_l, r_c, r_v = (real.get(k) or [] for k in ("timestamp", "open", "high", "low", "close", "volume"))
    real_by_ts = {int(e): {"open": o, "high": h, "low": l, "close": c, "volume": v}
                  for e, o, h, l, c, v in zip(r_ts, r_o, r_h, r_l, r_c, r_v)}
    recon_ts, recon_o, recon_h, recon_l, recon_c, recon_v = (recon.get(k) or [] for k in ("timestamp", "open", "high", "low", "close", "volume"))
    recon_by_ts = {int(e): {"open": o, "high": h, "low": l, "close": c, "volume": v}
                   for e, o, h, l, c, v in zip(recon_ts, recon_o, recon_h, recon_l, recon_c, recon_v)}

    matched = sorted(set(real_by_ts) & set(recon_by_ts))
    close_matches = ohlc_exact_matches = vol_matches = 0
    max_close_diff_pct = max_vol_diff_pct = 0.0
    rows = []
    for ts in matched:
        rb, cb = real_by_ts[ts], recon_by_ts[ts]
        close_diff_pct = abs(rb["close"] - cb["close"]) / rb["close"] * 100 if rb["close"] else 0.0
        vol_diff_pct = (abs(rb["volume"] - cb["volume"]) / rb["volume"] * 100) if rb["volume"] else (100.0 if cb["volume"] else 0.0)
        exact = abs(rb["open"] - cb["open"]) < 0.01 and abs(rb["close"] - cb["close"]) < 0.01
        max_close_diff_pct = max(max_close_diff_pct, close_diff_pct)
        max_vol_diff_pct = max(max_vol_diff_pct, vol_diff_pct)
        if close_diff_pct < 0.05:
            close_matches += 1
        if exact:
            ohlc_exact_matches += 1
        if vol_diff_pct < 1.0:
            vol_matches += 1
        rows.append({
            "time": datetime.fromtimestamp(ts, tz=_UNDERLYING_FEED_IST).strftime("%H:%M"),
            "real": rb, "recon": cb, "close_diff_pct": round(close_diff_pct, 3), "vol_diff_pct": round(vol_diff_pct, 3),
        })

    return {
        "symbol": sym, "day": d.isoformat(),
        "real_bar_count": len(real_by_ts), "recon_bar_count": len(recon_by_ts), "matched_bars": len(matched),
        "close_matches_within_0.05pct": close_matches, "open_and_close_exact_matches": ohlc_exact_matches,
        "volume_matches_within_1pct": vol_matches,
        "max_close_diff_pct": round(max_close_diff_pct, 3), "max_volume_diff_pct": round(max_vol_diff_pct, 3),
        "rows": rows,
    }
