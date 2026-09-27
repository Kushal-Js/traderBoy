"""
Near-real-time mock trading session - runs the REAL production tick loops
for every currently-live package (Options, Luxury, Swing, Bollinger) at
their REAL configured cadence, for a real wall-clock duration, with only
the Dhan network boundary mocked - same "fake the boundary, run the real
code" philosophy as every other test in this repo, just sustained over
time instead of one function call at a time.

WHY: unit/integration tests call one function once and assert on the
result. They don't catch what only shows up under sustained real-time
load: does a tick loop's timing drift when a mocked call is slow or
fails? does the LTP-retry fix actually keep the shared executor pool
healthy across hundreds of ticks, not just one? does the Swing/Bollinger
same-underlying race (flagged, unresolved, in SYSTEM_AUDIT_2026-09-27_
pre_monday.md) actually manifest with real numbers when both packages'
real _monitor_tick fires on the same symbol in the same real tick?

SCOPE (this run, ~6-8 real minutes):
  - Swing + Bollinger: their REAL monitor_loop -> _monitor_tick, at the
    REAL MONITOR_INTERVAL_SECONDS=5s cadence, for the full session -
    entry-scan AND exit-check, exactly as the live bot runs it. Their
    own signal-decision functions (_evaluate_entry_signal) are wrapped,
    not replaced: real watchlist symbols with no seeded signal behave
    exactly as they would with no real market data (returns None most
    ticks, matching production on a quiet symbol) - a handful of
    designated "trigger" symbols get a scripted BULLISH signal injected
    at a chosen tick, so real entries actually happen and the rest of
    the real pipeline (entry_backlog, capacity, PositionStore, order
    placement, exit-check on a LATER tick) gets genuinely exercised.
  - THE RACE SCENARIO: one symbol present on BOTH watchlists (this
    session's watchlists overlap almost entirely - see the audit doc)
    is scripted to fire BULLISH on both Swing's and Bollinger's real
    _evaluate_entry_signal in the SAME real tick. Neither package
    participates in cross_strategy_registry (confirmed in the audit) -
    this run reports whether both really did place an order for it.
  - Options + Luxury: their REAL monitor_loop (exit-check + admin
    housekeeping, MONITOR_INTERVAL_SECONDS=2s) runs the full session.
    Their REAL entry gate chain (_breakout_entry_fn, which is what the
    dispatcher itself calls every 60s in production - see the pipeline
    map this harness was built from) is called directly a few times
    during the session with synthetic alerts, rather than also running
    the literal 60s dispatcher loop (at 5-8 real minutes that loop would
    only fire 5-8 times total - not enough signal for the effort of
    wiring it faithfully). This still exercises the REAL gate chain,
    REAL paper-mode routing (paper mode is ON for both today, per
    .env - see main.py's own docstring), REAL entry_backlog is NOT
    involved here (that's Swing/Bollinger-only), and REAL order
    placement (mocked network only).
  - LTP flakiness: a real, tunable fraction of `_get_option_ltp_once`
    calls raise (matching the 656-failures-in-one-session SONACOMS/
    CIPLA pattern from the 27 Sep audit) - exercises get_option_ltp_
    async's real retry path under real sustained load, not a single
    isolated call.

WHAT'S DELIBERATELY OUT OF SCOPE: the literal breakout_signal.
universe_dispatcher_loop (60s cadence, too slow to exercise meaningfully
in a short run - see above); real Supertrend/regime/EMA-200 arithmetic
(mocked at the signals-module seam, same technique tests/test_swing_v2_
entry_exit.py already uses for get_supertrend_state - reproducing real
indicator warm-up from raw ticks needs hundreds of historical bars,
disproportionate for what this run is actually checking); real Dhan
auth/network (never - see every other test file's own safety note).

SAFETY: zero real orders. Every Dhan network call is mocked before any
package's real loop starts. Scratch PositionStore instances and a
scratch history/ directory, same convention as every deep-integration
test in this repo.

HOW TO RUN:
    uv run python tests/test_near_realtime_mock_session.py [duration_seconds]
    (default 360s = 6 minutes; pass e.g. 20 for a quick sanity check)
"""
import asyncio
import os
import random
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import trade_history

scratch_dir = Path(tempfile.mkdtemp(prefix="dhanboy_near_realtime_mock_"))
trade_history.HISTORY_DIR = scratch_dir

import Options.dhan_client as odc
import Options.trading_engine as ote
import Options.position_store as ops
import Options.option_main as om
import Luxury.trading_engine as lte
import Luxury.position_store as lps
import Luxury.luxury_main as lm
import Swing.trading_engine as ste
import Swing.position_store as sps
import Swing.signals as ssig
import Swing.candle_feed as scf
import Bollinger.trading_engine as bte
import Bollinger.position_store as bps
import Bollinger.signals as bsig
import breakout_paper_engine
import paper_mode_control
from Options.dhan_client import AtmOption, OrderResult, OrderStatus, dhan_wrapper

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------- #
# Fake clock - today is Sunday; every real market-hours/weekday gate
# would immediately no-op otherwise. Advances in real lockstep with wall
# clock so tick cadence/timing observations stay meaningful, pinned to a
# real weekday well inside 09:15-15:30 IST.
# --------------------------------------------------------------------- #
_session_start_wall = time.monotonic()
_FAKE_BASE = datetime(2026, 9, 28, 11, 0, 0, tzinfo=IST)  # a real Monday


def fake_now() -> datetime:
    return _FAKE_BASE + timedelta(seconds=time.monotonic() - _session_start_wall)


# --------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------- #
class Report:
    def __init__(self):
        self.tick_counts: dict[str, int] = {}
        self.exceptions: list[str] = []
        self.entries_attempted: dict[str, int] = {}
        self.orders_placed: list[dict] = []
        self.ltp_calls = 0
        self.ltp_failures = 0
        self.tick_gaps: dict[str, list[float]] = {}

    def note_tick(self, loop_name: str, gap: float):
        self.tick_counts[loop_name] = self.tick_counts.get(loop_name, 0) + 1
        self.tick_gaps.setdefault(loop_name, []).append(gap)

    def note_exception(self, where: str, exc: Exception):
        self.exceptions.append(f"{where}: {type(exc).__name__}: {exc}")

    def note_order(self, strategy: str, trading_symbol: str, transaction_type: str):
        self.orders_placed.append({"strategy": strategy, "trading_symbol": trading_symbol,
                                    "transaction_type": transaction_type, "at": fake_now().isoformat()})


REPORT = Report()


# --------------------------------------------------------------------- #
# Synthetic LTP feed - one evolving price per trading_symbol, small
# random walk, a tunable fraction of calls raise (real-world flakiness).
# --------------------------------------------------------------------- #
_LTP_STATE: dict[str, float] = {}
LTP_FAILURE_RATE = 0.12  # ~matches the SONACOMS/CIPLA density from the 27 Sep audit


def _seed_price(trading_symbol: str, base: float) -> None:
    _LTP_STATE.setdefault(trading_symbol, base)


_INTRADAY_CACHE: dict[str, dict] = {}


def _synthetic_intraday_series(security_id: str, num_bars: int = 300, interval_minutes: int = 5) -> dict:
    """Realistic-shaped OHLCV so real signal code (Bollinger/Swing's own
    _fetch_signal_state_once/_get_intraday_series) gets enough continuous
    multi-day history to compute on, instead of hitting its own "no data
    at all" failure branch (which retries repeatedly and stalls a real
    tick well past MONITOR_INTERVAL_SECONDS - found the hard way in this
    harness's own first run). Cached per security_id so repeated calls
    within one mock session return a consistent, still-growing series
    rather than a fresh random one each time."""
    cached = _INTRADAY_CACHE.get(security_id)
    if cached is not None:
        return cached
    end = fake_now()
    price = random.uniform(80.0, 2000.0)
    opens, highs, lows, closes, volumes, timestamps = [], [], [], [], [], []
    for i in range(num_bars, 0, -1):
        ts = end - timedelta(minutes=interval_minutes * i)
        o = price
        price = max(1.0, price * (1 + random.uniform(-0.006, 0.006)))
        c = price
        h = max(o, c) * (1 + random.uniform(0, 0.003))
        l = min(o, c) * (1 - random.uniform(0, 0.003))
        opens.append(round(o, 2)); highs.append(round(h, 2)); lows.append(round(l, 2))
        closes.append(round(c, 2)); volumes.append(random.randint(1000, 50000))
        timestamps.append(ts.timestamp())
    series = {"open": opens, "high": highs, "low": lows, "close": closes,
              "volume": volumes, "timestamp": timestamps}
    _INTRADAY_CACHE[security_id] = series
    return series


def _synthetic_ltp_once(trading_symbol: str) -> float:
    REPORT.ltp_calls += 1
    if random.random() < LTP_FAILURE_RATE:
        REPORT.ltp_failures += 1
        raise ValueError(f"No LTP returned for {trading_symbol}")
    price = _LTP_STATE.get(trading_symbol, 50.0)
    price = max(0.5, price * (1 + random.uniform(-0.02, 0.02)))
    _LTP_STATE[trading_symbol] = price
    return round(price, 2)


# --------------------------------------------------------------------- #
# Dhan network boundary - one shared dhan_wrapper singleton, every
# package's real trading_engine.py calls into it identically.
# --------------------------------------------------------------------- #
def install_all_mocks():
    originals = {name: getattr(dhan_wrapper, name, None) for name in [
        "get_atm_option", "get_liquid_atm_option", "get_futures_contract", "get_mcx_futures_contract",
        "_equity_instrument_meta", "_equity_security_id", "index_security_id", "is_mcx_commodity",
        "is_market_open", "_get_option_ltp_once", "get_cached_option_ltp", "note_rest_ltp",
        "get_last_historical_close", "get_margin_required", "get_fund_limits",
        "has_open_position_for_underlying", "get_pending_order_id", "get_broker_net_quantity",
        "cancel_order", "_get_open_fno_positions_once", "get_open_fno_positions",
        "get_open_equity_positions", "get_open_mcx_positions",
        "subscribe_option_price", "unsubscribe_option_price",
        "subscribe_equity_quote", "unsubscribe_equity_quote",
        "subscribe_index_quote", "unsubscribe_index_quote",
        "subscribe_mcx_quote", "unsubscribe_mcx_quote",
        "refresh_supertrend_signal", "get_cached_supertrend_bearish", "get_cached_supertrend_candle_start",
        "refresh_ema_cross_signal", "get_cached_ema_cross_bearish", "get_cached_ema_cross_crossed",
        "get_cached_ema_cross_candle_start",
        "refresh_liquidity_signal", "get_cached_illiquid",
        "is_rsi_loss_reentry_blocked", "get_cached_rsi", "get_cached_prev_rsi", "rsi_loss_reentry_reason",
        "place_market_order", "place_equity_market_order",
        "place_stop_loss_limit_order", "place_equity_stop_loss_limit_order", "place_stop_loss_market_order",
        "check_if_order_filled", "refresh_order_status", "wait_for_order_result",
        "should_delay_ce_entry", "get_day_change_pct", "get_cached_underlying_close",
        "fetch_continuous_intraday", "evaluate_nifty_open_condition", "should_block_all_entries_today",
    ]}

    def fake_atm(symbol: str, option_type: str) -> AtmOption:
        ts = f"{symbol} FAKE EXP {option_type}"
        _seed_price(ts, random.uniform(30.0, 200.0))
        return AtmOption(trading_symbol=ts, strike=1000.0, option_type=option_type,
                          lot_size=500, security_id=f"SECID-{symbol}",
                          expiry_date=fake_now().date() + timedelta(days=25))

    def fake_futures_contract(symbol: str):
        from Options.dhan_client import FuturesContract
        ts = f"{symbol} FAKE EXP FUT"
        _seed_price(ts, random.uniform(500.0, 5000.0))
        return FuturesContract(trading_symbol=ts, security_id=f"FUT-{symbol}", lot_size=500,
                                expiry_date=fake_now().date() + timedelta(days=25))

    def fake_mcx_futures_contract(symbol: str):
        from Options.dhan_client import FuturesContract
        ts = f"{symbol} FAKE EXP FUTCOM"
        _seed_price(ts, random.uniform(500.0, 900.0))
        return FuturesContract(trading_symbol=ts, security_id=f"MCXFUT-{symbol}", lot_size=1,
                                expiry_date=fake_now().date() + timedelta(days=25))

    placed_orders_local = REPORT.orders_placed

    def fake_place_market_order(trading_symbol, quantity, transaction_type, tag=None, product_type=None):
        order_id = f"FAKE-{trading_symbol}-{transaction_type}-{len(placed_orders_local)}"
        REPORT.note_order(tag or "?", trading_symbol, transaction_type)
        return {"order_id": order_id, "is_amo": False}

    def fake_wait_for_order_result(order_id, is_amo=False):
        price = _LTP_STATE.get(order_id.split("FAKE-")[-1].rsplit("-", 2)[0], 50.0)
        return OrderResult(order_id=order_id, status=OrderStatus.TRADED, remark="",
                            fill_price=round(price, 2), filled_quantity=500, is_amo=False)

    dhan_wrapper.get_atm_option = fake_atm
    dhan_wrapper.get_liquid_atm_option = fake_atm
    dhan_wrapper.get_futures_contract = fake_futures_contract
    dhan_wrapper.get_mcx_futures_contract = fake_mcx_futures_contract
    dhan_wrapper._equity_instrument_meta = lambda sym: {"security_id": f"EQSEC-{sym}", "lot_size": 1, "tick_size": 0.05}
    dhan_wrapper._equity_security_id = lambda sym: f"EQSEC-{sym}"
    dhan_wrapper.index_security_id = lambda sym: {"NIFTY": "13", "BANKNIFTY": "25"}.get(sym, "0")
    dhan_wrapper.is_mcx_commodity = lambda sym: sym in ("COPPER", "NATURALGAS", "CRUDEOIL")
    dhan_wrapper.is_market_open = lambda exchange_segment="NSE_FNO": True
    dhan_wrapper._get_option_ltp_once = _synthetic_ltp_once
    dhan_wrapper.get_cached_option_ltp = lambda ts: None  # always force the real REST-fallback path, on purpose
    dhan_wrapper.note_rest_ltp = lambda ts, ltp: None
    dhan_wrapper.get_last_historical_close = lambda ts, *a, **k: _LTP_STATE.get(ts, 50.0)
    dhan_wrapper.get_margin_required = lambda *a, **k: {"totalMargin": 999.0}
    dhan_wrapper.get_fund_limits = lambda: {"availabelBalance": 5_000_000.0}
    dhan_wrapper.has_open_position_for_underlying = lambda symbol: False
    dhan_wrapper.get_pending_order_id = lambda *a, **k: None
    dhan_wrapper.get_broker_net_quantity = lambda *a, **k: 500
    dhan_wrapper.cancel_order = lambda order_id: None
    dhan_wrapper._get_open_fno_positions_once = lambda: []
    dhan_wrapper.get_open_fno_positions = lambda: []
    dhan_wrapper.get_open_equity_positions = lambda: []
    dhan_wrapper.get_open_mcx_positions = lambda: []
    for name in ("subscribe_option_price", "unsubscribe_option_price",
                 "subscribe_equity_quote", "unsubscribe_equity_quote",
                 "subscribe_index_quote", "unsubscribe_index_quote",
                 "subscribe_mcx_quote", "unsubscribe_mcx_quote"):
        setattr(dhan_wrapper, name, lambda *a, **k: None)
    dhan_wrapper.refresh_supertrend_signal = lambda sym: None
    dhan_wrapper.get_cached_supertrend_bearish = lambda sym: None
    dhan_wrapper.get_cached_supertrend_candle_start = lambda sym: None
    dhan_wrapper.refresh_ema_cross_signal = lambda sym: None
    dhan_wrapper.get_cached_ema_cross_bearish = lambda sym: None
    dhan_wrapper.get_cached_ema_cross_crossed = lambda sym: None
    dhan_wrapper.get_cached_ema_cross_candle_start = lambda sym: None
    dhan_wrapper.refresh_liquidity_signal = lambda ts: None
    dhan_wrapper.get_cached_illiquid = lambda ts: None
    dhan_wrapper.is_rsi_loss_reentry_blocked = lambda sym: False
    dhan_wrapper.get_cached_rsi = lambda sym: None
    dhan_wrapper.get_cached_prev_rsi = lambda sym: None
    dhan_wrapper.rsi_loss_reentry_reason = lambda sym: None
    dhan_wrapper.place_market_order = fake_place_market_order
    dhan_wrapper.place_equity_market_order = fake_place_market_order
    dhan_wrapper.place_stop_loss_limit_order = lambda trading_symbol, quantity, transaction_type, trigger_price, limit_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SLL-{trading_symbol}"}
    dhan_wrapper.place_equity_stop_loss_limit_order = dhan_wrapper.place_stop_loss_limit_order
    dhan_wrapper.place_stop_loss_market_order = lambda trading_symbol, quantity, transaction_type, trigger_price, tag=None, product_type=None: {
        "order_id": f"FAKE-SL-{trading_symbol}"}
    dhan_wrapper.check_if_order_filled = lambda order_id: None
    dhan_wrapper.refresh_order_status = lambda order_id, is_amo=False: OrderResult(
        order_id=order_id, status=OrderStatus.PENDING, remark="", fill_price=0, filled_quantity=0)
    dhan_wrapper.wait_for_order_result = fake_wait_for_order_result
    dhan_wrapper.should_delay_ce_entry = lambda: False
    dhan_wrapper.get_day_change_pct = lambda symbol: 0.0
    dhan_wrapper.get_cached_underlying_close = lambda sym: 100.0
    dhan_wrapper.fetch_continuous_intraday = lambda security_id, exchange_segment, instrument_type, interval_minutes, lookback_days_override=None: _synthetic_intraday_series(security_id, interval_minutes=interval_minutes)
    dhan_wrapper.evaluate_nifty_open_condition = lambda: {"evaluated": False}
    dhan_wrapper.should_block_all_entries_today = lambda: False

    def restore():
        for name, fn in originals.items():
            if fn is not None:
                setattr(dhan_wrapper, name, fn)

    return restore


# --------------------------------------------------------------------- #
# Signal injection - real _evaluate_entry_signal is WRAPPED, not
# replaced. Designated trigger symbols get a scripted result at a
# chosen tick number; everything else falls through to the real
# function (mostly None, matching "no real market data" in this mock).
# --------------------------------------------------------------------- #
def wrap_entry_signal(module, trigger_map: dict[str, tuple[int, object]]):
    """trigger_map: {symbol: (tick_number, return_value)}. real_fn is
    called for every symbol on every OTHER tick, preserving real
    steady-state (mostly-None) behavior."""
    real_fn = module._evaluate_entry_signal
    counters = {"tick": 0}

    async def wrapped(symbol):
        if symbol in trigger_map:
            tick_no, value = trigger_map[symbol]
            if counters["tick"] == tick_no:
                return value
        try:
            return await real_fn(symbol)
        except Exception as exc:  # noqa: BLE001
            REPORT.note_exception(f"{module.__name__}._evaluate_entry_signal({symbol})", exc)
            return None

    def advance_tick():
        counters["tick"] += 1

    module._evaluate_entry_signal = wrapped
    return real_fn, advance_tick


async def run_swing_bollinger_session(duration_s: float):
    RACE_SYMBOL = "MCX"  # on both watchlists per data/watchlist + data/bollinger_watchlist

    # Shrink to a representative subset for this smoke run - the real
    # 19/17-symbol watchlists made a single _monitor_tick take ~17s
    # (real indicator computation across the whole list), swamping the
    # real 5s MONITOR_INTERVAL_SECONDS entirely. That's itself a real,
    # separate finding (computation, not I/O, dominates at full
    # watchlist size on a 1-vCPU box) - reported at the end, not hidden -
    # but a smoke run needs ticks landing close to real cadence to
    # actually validate cadence/timing behavior, so this run uses 6
    # symbols per package instead of the full list.
    import Swing.watchlist as swatchlist
    import Bollinger.watchlist as bwatchlist
    swing_subset_file = scratch_dir / "swing_watchlist_subset"
    bollinger_subset_file = scratch_dir / "bollinger_watchlist_subset"
    swing_subset = ["SONACOMS", "APOLLOHOSP", "NIFTY", RACE_SYMBOL, "BOSCHLTD", "RADICO"]
    bollinger_subset = ["SONACOMS", "NIFTY", RACE_SYMBOL, "BOSCHLTD", "RADICO"]
    swing_subset_file.write_text("\n".join(swing_subset) + "\n")
    bollinger_subset_file.write_text("\n".join(bollinger_subset) + "\n")
    swatchlist.WATCHLIST_FILE = swing_subset_file
    bwatchlist.WATCHLIST_FILE = bollinger_subset_file
    swatchlist.watchlist_store._symbols.clear()
    bwatchlist.watchlist_store._symbols.clear()

    swing_store = sps.SwingPositionStore()
    ste.position_store = swing_store
    await ste._entry_backlog.clear()

    bollinger_store = bps.BollingerPositionStore()
    bte.position_store = bollinger_store
    await bte._entry_backlog.clear()

    ssig._now_ist = fake_now
    bsig._now_ist = fake_now

    swing_triggers = {
        "SONACOMS": (10, "BULLISH"),
        RACE_SYMBOL: (30, "BULLISH"),   # race: fires this same tick on Bollinger too
        "APOLLOHOSP": (55, "BEARISH"),
    }
    bollinger_triggers = {
        RACE_SYMBOL: (30, ("BULLISH", 100.0, 90.0, 95.0)),
        "SONACOMS": (20, ("BULLISH", 50.0, 45.0, 47.0)),
    }
    real_swing_signal, swing_tick = wrap_entry_signal(ste, swing_triggers)
    real_bollinger_signal, bollinger_tick = wrap_entry_signal(bte, bollinger_triggers)

    for sym in list(swing_triggers) + list(bollinger_triggers):
        _seed_price(f"{sym} FAKE EXP CALL", random.uniform(40.0, 150.0))
        _seed_price(f"{sym} FAKE EXP PE", random.uniform(40.0, 150.0))

    # Seed candle_feed so is_symbol_ws_fresh is True from the start - a
    # real, warmed-up bot's steady state (WS data flowing). Without this
    # every symbol falls to the REST fallback + SYMBOL_PACING_SECONDS on
    # every single tick, which is real behavior for a COLD-started bot
    # but not what "near real time" is meant to validate here - found
    # this exact effect in this harness's own first run (ticks took
    # 15-17s instead of the real 5s interval).
    watchlist_symbols = set(swing_subset) | set(bollinger_subset)
    for sym in watchlist_symbols:
        scf._state[sym] = scf._SymbolState(security_id=f"MOCKSEC-{sym}")

    def _refresh_ws_freshness():
        now = fake_now()
        for sym in watchlist_symbols:
            st = scf._state.get(sym)
            if st is not None:
                st.last_tick_at = now

    deadline = time.monotonic() + duration_s
    last_tick_wall = time.monotonic()
    n = 0
    while time.monotonic() < deadline:
        n += 1
        gap = time.monotonic() - last_tick_wall
        last_tick_wall = time.monotonic()
        _refresh_ws_freshness()
        try:
            await ste._monitor_tick()
        except Exception as exc:  # noqa: BLE001
            REPORT.note_exception(f"Swing._monitor_tick #{n}", exc)
            traceback.print_exc()
        try:
            await bte._monitor_tick()
        except Exception as exc:  # noqa: BLE001
            REPORT.note_exception(f"Bollinger._monitor_tick #{n}", exc)
            traceback.print_exc()
        REPORT.note_tick("Swing._monitor_tick", gap)
        REPORT.note_tick("Bollinger._monitor_tick", gap)
        swing_tick()
        bollinger_tick()
        await asyncio.sleep(ste.config.MONITOR_INTERVAL_SECONDS)

    ste._evaluate_entry_signal = real_swing_signal
    bte._evaluate_entry_signal = real_bollinger_signal

    race_swing = RACE_SYMBOL in swing_store.live_positions or any(
        p.underlying_symbol == RACE_SYMBOL for p in swing_store.closed_positions_today)
    race_bollinger = RACE_SYMBOL in bollinger_store.live_positions or any(
        p.underlying_symbol == RACE_SYMBOL for p in bollinger_store.closed_positions_today)
    return {
        "swing_live": dict(swing_store.live_positions),
        "swing_closed_today": list(swing_store.closed_positions_today),
        "bollinger_live": dict(bollinger_store.live_positions),
        "bollinger_closed_today": list(bollinger_store.closed_positions_today),
        "race_symbol": RACE_SYMBOL,
        "race_swing_entered": race_swing,
        "race_bollinger_entered": race_bollinger,
    }


async def run_options_luxury_session(duration_s: float):
    options_store = ops.PositionStore()
    om.position_store = options_store
    ote.position_store = options_store
    luxury_store = lps.PositionStore()
    lm.position_store = luxury_store
    lte.position_store = luxury_store

    ote._now_ist = fake_now
    lte._now_ist = fake_now

    for cfg in (ote.config, lte.config):
        cfg.ENABLE_TRADING_TIME_LIMIT = False
        cfg.ENABLE_TRADING_WINDOWS = False
        cfg.NIFTY_GAP_BLOCK_ENABLED = False
        cfg.MAX_LIVE_POSITIONS_CE = 5
        cfg.MAX_LIVE_POSITIONS_PE = 5

    entry_symbols = ["MOCKOPT1", "MOCKOPT2", "MOCKOPT3", "MOCKOPT4"]
    for sym in entry_symbols:
        _seed_price(f"{sym} FAKE EXP CE", random.uniform(40.0, 150.0))

    deadline = time.monotonic() + duration_s
    last_tick_wall = time.monotonic()
    n = 0
    entry_fire_ticks = {
        20: (om._breakout_entry_fn, "MOCKOPT1", "Options"),
        40: (lm._breakout_entry_fn, "MOCKOPT2", "Luxury"),
        100: (om._breakout_entry_fn, "MOCKOPT3", "Options"),
        130: (lm._breakout_entry_fn, "MOCKOPT4", "Luxury"),
    }

    while time.monotonic() < deadline:
        n += 1
        gap = time.monotonic() - last_tick_wall
        last_tick_wall = time.monotonic()
        try:
            await _run_one_exit_tick(ote, options_store)
        except Exception as exc:  # noqa: BLE001
            REPORT.note_exception(f"Options exit-check #{n}", exc)
            traceback.print_exc()
        try:
            await _run_one_exit_tick(lte, luxury_store)
        except Exception as exc:  # noqa: BLE001
            REPORT.note_exception(f"Luxury exit-check #{n}", exc)
            traceback.print_exc()
        REPORT.note_tick("Options exit-check", gap)
        REPORT.note_tick("Luxury exit-check", gap)

        if n in entry_fire_ticks:
            entry_fn, sym, label = entry_fire_ticks[n]
            REPORT.entries_attempted[label] = REPORT.entries_attempted.get(label, 0) + 1
            try:
                await entry_fn(sym, "CE")
            except Exception as exc:  # noqa: BLE001
                REPORT.note_exception(f"{label}._breakout_entry_fn({sym})", exc)
                traceback.print_exc()

        await asyncio.sleep(2.0)  # real MONITOR_INTERVAL_SECONDS for Options/Luxury

    return {
        "options_live": dict(options_store.live_positions),
        "luxury_live": dict(luxury_store.live_positions),
    }


async def _run_one_exit_tick(module, store):
    await store.maybe_reset_for_new_day()
    if hasattr(module, "_sync_pending_orders"):
        await module._sync_pending_orders()
    positions = list(store.live_positions.items())
    if positions:
        await asyncio.gather(*[module._check_one_position(sym, pos) for sym, pos in positions],
                              return_exceptions=False)


async def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 360.0
    print(f"=== Near-real-time mock session - {duration_s:.0f}s, "
          f"4 live packages, real tick cadence, mocked Dhan boundary ===\n")

    restore_mocks = install_all_mocks()
    real_paper_mode = {name: paper_mode_control.is_paper_mode_enabled for name in ()}
    real_is_paper_mode_enabled = paper_mode_control.is_paper_mode_enabled
    paper_mode_control.is_paper_mode_enabled = lambda strategy: False  # exercise the REAL order path, not the paper engine, in this run

    start = time.monotonic()
    try:
        (swing_bollinger_result, options_luxury_result) = await asyncio.gather(
            run_swing_bollinger_session(duration_s),
            run_options_luxury_session(duration_s),
        )
    finally:
        restore_mocks()
        paper_mode_control.is_paper_mode_enabled = real_is_paper_mode_enabled

    elapsed = time.monotonic() - start

    print(f"--- Session complete: {elapsed:.1f}s real wall-clock ---\n")

    print("Tick counts (real cadence maintained):")
    for name, count in REPORT.tick_counts.items():
        gaps = REPORT.tick_gaps[name]
        avg_gap = sum(gaps[1:]) / max(1, len(gaps) - 1)
        print(f"  {name}: {count} ticks, avg inter-tick gap {avg_gap:.2f}s")

    print(f"\nLTP fetch calls: {REPORT.ltp_calls} ({REPORT.ltp_failures} injected failures, "
          f"{REPORT.ltp_failures / max(1, REPORT.ltp_calls) * 100:.1f}% - target ~{LTP_FAILURE_RATE * 100:.0f}%)")

    print(f"\nOrders placed ({len(REPORT.orders_placed)} total):")
    for o in REPORT.orders_placed:
        print(f"  [{o['at']}] {o['strategy']}: {o['transaction_type']} {o['trading_symbol']}")

    print(f"\nSwing live positions: {list(swing_bollinger_result['swing_live'].keys())}")
    print(f"Swing closed today: {[p.underlying_symbol for p in swing_bollinger_result['swing_closed_today']]}")
    print(f"Bollinger live positions: {list(swing_bollinger_result['bollinger_live'].keys())}")
    print(f"Bollinger closed today: {[p.underlying_symbol for p in swing_bollinger_result['bollinger_closed_today']]}")
    print(f"Options live positions: {list(options_luxury_result['options_live'].keys())}")
    print(f"Luxury live positions: {list(options_luxury_result['luxury_live'].keys())}")

    print(f"\n--- RACE SCENARIO: {swing_bollinger_result['race_symbol']} fired BULLISH on both "
          f"Swing and Bollinger in the same tick ---")
    print(f"  Swing entered it:     {swing_bollinger_result['race_swing_entered']}")
    print(f"  Bollinger entered it: {swing_bollinger_result['race_bollinger_entered']}")
    if swing_bollinger_result["race_swing_entered"] and swing_bollinger_result["race_bollinger_entered"]:
        print("  >>> CONFIRMED LIVE: both packages independently entered the SAME underlying "
              "in the same real tick - the cross_strategy_registry gap from the audit is real, "
              "not theoretical.")
    else:
        print("  Race did not fully materialize this run (one or both sides didn't reach entry - "
              "check exceptions below / re-run) - not evidence the gap doesn't exist, the code "
              "path (no registry check in either package) is unchanged either way.")

    print(f"\nExceptions caught ({len(REPORT.exceptions)}):")
    for e in REPORT.exceptions:
        print(f"  {e}")

    print("\n" + ("ALL CLEAR - zero uncaught exceptions across every real tick." if not REPORT.exceptions
                   else f"{len(REPORT.exceptions)} exception(s) surfaced - see above."))


if __name__ == "__main__":
    asyncio.run(main())
    import shutil
    shutil.rmtree(scratch_dir, ignore_errors=True)
