"""
Shared plumbing for the "target extension" backtest (DhanBoy options
strategy). Read-only: only ever calls Dhan's historical/intraday/instrument-
master REST endpoints via Options.dhan_client.dhan_wrapper. Never touches
order_placement / place_market_order / cancel_order.

Run from the repo root with `uv run python <script>.py` so `Options.config`
picks up the real .env (DHAN_AUTH_MODE=pin_totp) and so the production
imports below resolve.

--------------------------------------------------------------------------
Provenance / methodology note (read this before trusting any number below)
--------------------------------------------------------------------------
This scratchpad directory (session-scoped, persists across sessions run
against this same conversation slot) already contained, from an earlier
backtest round on THIS EXACT 15-day CSV pair, two files:
  - backtest_ce_15days_results.json (104 CE trades)
  - backtest_pe_15days_results.json (69 PE trades)
each with symbol / trading_symbol / date / entry_time / entry_price /
quantity per trade - the output of a full rank_and_pick_top_stocks-replica
+ dedup/capacity + ATM-strike-resolution pipeline (see the sibling
backtest_ce_15days.py / backtest_pe_15days.py in this same directory for
that pipeline's exact code), run against config.MAX_LIVE_POSITIONS_CE/PE=2.

Cross-checked before reuse: replaying those entries' baseline exit
(target/hard-SL only) reproduces BACKTEST_RESULTS.md's published
"Baseline (target/SL only)" figures for the 14-day validation round
EXACTLY - CE ₹152,913.50 / 104 trades, PE ₹77,313.50 / 69 trades - and the
then-current-production combined exit (dynamic SL + Supertrend, naive
20-candle warmup) reproduces "Fix B, 20-candle warmup" CE ₹137,993.00 and
PE "current behavior" ₹76,892.25 exactly too (see backtest_st_fix_ce/pe_
results.json, also already in this directory). That is about as strong a
validation as a rebuilt pipeline could hope to get - it is bit-for-bit the
number already published as ground truth.

Given that, this round REUSES those 104+69 fixed entries verbatim (same
symbol/trading_symbol/entry_time/entry_price/quantity - never re-derived)
rather than re-running the ranking pipeline a second time. This is not a
shortcut around the task's own methodology - it's the SAME discipline
BACKTEST_RESULTS.md itself uses throughout ("Entries are held fixed across
variants being compared in the same round - only the exit rule differs -
so a P&L delta isolates the exit rule's own effect", and explicitly in the
Supertrend-warmup-fix round: "replay the IDENTICAL fixed entries from the
14-day backtest's own result files ... not re-ranked - so this isolates
the exit-rule change cleanly"). Re-ranking from raw CSVs a second time
would only reintroduce the exact entry-drift risk BACKTEST_RESULTS.md's
PE Round 2 documents (a separate historical-data re-fetch shifted which
symbols ranked in the top-3), for no benefit - the entries are already
fixed and already validated bit-for-bit against the published baseline.

pipeline_validate.py (sibling script) still rebuilds the ranking/dedup/
capacity/ATM-strike pipeline from scratch against the raw CSVs, for ONE
day per side, and cross-checks it reproduces the same entries as the
fixed-entry files for that day - satisfying the task's "validate the
rebuilt pipeline on one day before trusting it" instruction without
having to burn the full 14-day ranking-fetch budget a second time.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[0]  # overridden by callers via sys.path insert
sys.path.insert(0, "/Users/kushalgaur/Desktop/projects/trading/traderBoy")

from zoneinfo import ZoneInfo

from Options import config  # noqa: E402  (must come after sys.path insert)
from Options.dhan_client import dhan_wrapper, _compute_supertrend  # noqa: E402
from Options.position_store import Position  # noqa: E402
from Options.trading_engine import _exit_reason_for  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------- #
# Config pulled straight from Options.config (which itself pulls from the
# real repo-root .env) - printed at import time so every run's log records
# exactly which values were live for that run.
# ---------------------------------------------------------------------- #
TOP_N = config.TOP_N_STOCKS
TARGET_PCT = config.TARGET_PCT
STOP_LOSS_PCT = config.STOP_LOSS_PCT
SQUARE_OFF_TIME = datetime.strptime(config.SQUARE_OFF_TIME, "%H:%M").time()
MARKET_OPEN = datetime.strptime(config.MARKET_OPEN_TIME, "%H:%M").time()
ST_PERIOD = config.SUPERTREND_PERIOD
ST_MULT = config.SUPERTREND_MULTIPLIER
ST_INTERVAL = config.SUPERTREND_INTERVAL_MINUTES
# Both SUPERTREND_MIN_WARMUP_CANDLES and SUPERTREND_ENTRY_GRACE_MINUTES were
# removed from production config.py entirely (user request 27 Aug 2026 -
# immediate action on a reversal signal, no tuned delay beyond skipping the
# entry candle itself). Hardcoded to 0 here to match: ST_MIN_WARMUP=0 means
# supertrend_state_at() only enforces the bare ST_PERIOD+1 ATR-seed minimum
# (same as production's dhan_client.refresh_supertrend_signal() now always
# does); GRACE_MINUTES=0 means supertrend_against_position() triggers on the
# very next candle after entry, matching trading_engine._supertrend_signal_for().
ST_MIN_WARMUP = 0
GRACE_MINUTES = 0
LOT_QTY = config.QUANTITY_LOTS

# New, backtest-only constant for the "target extension" variant - not part
# of production config.py (this script never modifies the live bot).
EXTENDED_TRAILING_SL_PCT = 0.05

print(
    f"[bt_common] TARGET_PCT={TARGET_PCT} STOP_LOSS_PCT={STOP_LOSS_PCT} "
    f"ENABLE_TRAILING_SL={config.ENABLE_TRAILING_SL} ENABLE_DYNAMIC_SL={config.ENABLE_DYNAMIC_SL} "
    f"DYNAMIC_SL_STEP_PCT_CE={config.DYNAMIC_SL_STEP_PCT_CE} DYNAMIC_SL_STEP_PCT_PE={config.DYNAMIC_SL_STEP_PCT_PE} "
    f"DYNAMIC_SL_INCREASE_PCT={config.DYNAMIC_SL_INCREASE_PCT} "
    f"ENABLE_SUPERTREND_EXIT={config.ENABLE_SUPERTREND_EXIT} ST_PERIOD={ST_PERIOD} ST_MULT={ST_MULT} "
    f"ST_INTERVAL={ST_INTERVAL} ST_MIN_WARMUP={ST_MIN_WARMUP} GRACE_MINUTES={GRACE_MINUTES} "
    f"MAX_LIVE_POSITIONS_CE={config.MAX_LIVE_POSITIONS_CE} MAX_LIVE_POSITIONS_PE={config.MAX_LIVE_POSITIONS_PE} "
    f"EXTENDED_TRAILING_SL_PCT={EXTENDED_TRAILING_SL_PCT}"
)

_AUTHED = False


def ensure_auth() -> None:
    global _AUTHED
    if not _AUTHED:
        dhan_wrapper.authenticate()
        _AUTHED = True


# Pacing between Dhan REST calls - see Options/dhan_client.py's _retry and
# trading_engine.rank_and_pick_top_stocks' own time.sleep(0.35) precedent.
# Dhan's market-data REST API has an undocumented rate limit (NOTES.md
# "Known external constraints"); 0.35s between calls is the value already
# proven safe by production/prior backtest code.
CALL_PACE_SECONDS = 0.35


def _paced_call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    time.sleep(CALL_PACE_SECONDS)
    return result


def retry_call(fn, *args, retries: int = 3, delay: float = 2.0, **kwargs):
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return _paced_call(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"    [retry {attempt+1}/{retries}] {getattr(fn, '__name__', fn)} failed: {exc}")
            time.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------- #
# Equity (underlying) historical data
# ---------------------------------------------------------------------- #
_security_id_cache: dict[str, str] = {}


def equity_security_id(symbol: str) -> str:
    if symbol not in _security_id_cache:
        _security_id_cache[symbol] = dhan_wrapper._equity_security_id(symbol)
    return _security_id_cache[symbol]


_daily_cache: dict[str, dict] = {}


def fetch_daily_series(symbol: str, from_date: str, to_date: str) -> None:
    """Previous-close lookups - one call per symbol (not per day), cached."""
    if symbol in _daily_cache:
        return
    try:
        security_id = equity_security_id(symbol)
        resp = retry_call(
            dhan_wrapper.client.Dhan.historical_daily_data,
            security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
            from_date=from_date, to_date=to_date,
        )
        data = resp.get("data", {})
        ts = data.get("timestamp") or []
        closes = data.get("close") or []
        by_date = {}
        for t, c in zip(ts, closes):
            by_date[datetime.fromtimestamp(t, tz=IST).date()] = c
        _daily_cache[symbol] = by_date
    except Exception as exc:  # noqa: BLE001
        print(f"  [daily FAILED] {symbol}: {exc}")
        _daily_cache[symbol] = {}


def prev_close_for(symbol: str, d) -> Optional[float]:
    series = _daily_cache.get(symbol) or {}
    prior = sorted(dd for dd in series if dd < d)
    return series[prior[-1]] if prior else None


_underlying_day_cache: dict[tuple, dict] = {}


def fetch_underlying_day(symbol: str, d) -> dict:
    """1-min (ranking/entry-timing) + 5-min (Supertrend) candles for one
    trading day, cached by (symbol, date)."""
    key = (symbol, d)
    if key in _underlying_day_cache:
        return _underlying_day_cache[key]
    security_id = equity_security_id(symbol)
    ds = d.isoformat()
    resp_1m = retry_call(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=ds, to_date=ds, interval=1,
    )
    resp_5m = retry_call(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=ds, to_date=ds, interval=ST_INTERVAL,
    )
    result = {
        "1m": resp_1m.get("data") or {},
        "5m": resp_5m.get("data") or {},
        "prev_close": prev_close_for(symbol, d),
    }
    _underlying_day_cache[key] = result
    return result


def fetch_underlying_5m_only(symbol: str, d) -> dict:
    """Like fetch_underlying_day but only the 5-min series (used by the
    full-run backtest, which reuses fixed entries and so never needs the
    1-min ranking series)."""
    key = (symbol, d)
    if key in _underlying_day_cache:
        return _underlying_day_cache[key]
    security_id = equity_security_id(symbol)
    ds = d.isoformat()
    resp_5m = retry_call(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=ds, to_date=ds, interval=ST_INTERVAL,
    )
    result = {"5m": resp_5m.get("data") or {}}
    _underlying_day_cache[key] = result
    return result


def price_at(candles: dict, t: datetime) -> Optional[float]:
    """Close of the candle at-or-nearest-before t (last candle whose start
    is <= t)."""
    ts = candles.get("timestamp") or []
    closes = candles.get("close") or []
    result = None
    for epoch, c in zip(ts, closes):
        ct = datetime.fromtimestamp(epoch, tz=IST)
        if ct <= t:
            result = c
        else:
            break
    return result


def candle_open_at_or_after(candles: dict, t: datetime) -> tuple[Optional[float], Optional[datetime]]:
    """First candle whose start is >= t - used for entry pricing (a market
    order placed right after a trigger fills at-or-after that moment, not
    before it). Returns (open_price, candle_start)."""
    ts = candles.get("timestamp") or []
    opens = candles.get("open") or []
    for epoch, o in zip(ts, opens):
        ct = datetime.fromtimestamp(epoch, tz=IST)
        if ct >= t:
            return o, ct
    return None, None


def pct_change_at(u_data: dict, t: datetime) -> Optional[float]:
    prev_close = u_data.get("prev_close")
    p = price_at(u_data["1m"], t)
    if not prev_close or p is None:
        return None
    return (p - prev_close) / prev_close * 100


# ---------------------------------------------------------------------- #
# Supertrend - historical walk-forward replica of
# dhan_client.refresh_supertrend_signal()'s exact slicing/gating, using
# the real dhan_client._compute_supertrend (pure function, imported
# directly - not reimplemented).
# ---------------------------------------------------------------------- #
def supertrend_state_at(u_5m_candles: dict, t: datetime) -> tuple[Optional[bool], Optional[datetime]]:
    """Returns (is_bearish, candle_start) as production's
    refresh_supertrend_signal would have cached it if "now" were t:
      - drops any candle not yet fully closed as of t (candle_start +
        ST_INTERVAL > t) - mirrors the "drop the still-forming candle"
        logic in dhan_client.py.
      - requires >= ST_MIN_WARMUP candles closed (config.
        SUPERTREND_MIN_WARMUP_CANDLES) - mirrors the Fix-B warmup gate.
      - is_bearish = closes[-1] < supertrend[-1] on that closed series.
    Returns (None, None) if no signal would be cached yet."""
    ts = u_5m_candles.get("timestamp") or []
    highs = u_5m_candles.get("high") or []
    lows = u_5m_candles.get("low") or []
    closes = u_5m_candles.get("close") or []

    cutoff = len(ts)
    for i, epoch in enumerate(ts):
        candle_start = datetime.fromtimestamp(epoch, tz=IST)
        if candle_start + timedelta(minutes=ST_INTERVAL) > t:
            cutoff = i
            break
    h, l, c, cts = highs[:cutoff], lows[:cutoff], closes[:cutoff], ts[:cutoff]

    if len(c) < ST_PERIOD + 1 or len(c) < ST_MIN_WARMUP:
        return None, None

    st = _compute_supertrend(h, l, c, period=ST_PERIOD, multiplier=ST_MULT)
    if st[-1] is None:
        return None, None
    is_bearish = c[-1] < st[-1]
    candle_start = datetime.fromtimestamp(cts[-1], tz=IST) if cts else None
    return is_bearish, candle_start


def supertrend_against_position(
    is_bearish: Optional[bool], candle_start: Optional[datetime],
    entry_candle_start: Optional[datetime], option_type: str,
) -> bool:
    """Faithful replica of trading_engine._supertrend_signal_for's rules
    (that function itself is cache-only/live and can't be called directly
    against historical data - see its own docstring)."""
    if is_bearish is None:
        return False
    against = is_bearish if option_type == "CE" else (not is_bearish)
    if not against:
        return False
    if candle_start is None or entry_candle_start is None:
        return True
    return candle_start > entry_candle_start + timedelta(minutes=GRACE_MINUTES)


def supertrend_favorable(is_bearish: Optional[bool], option_type: str) -> bool:
    """Target-extension eligibility check: a FULLY WARMED-UP signal that
    currently CONFIRMS the position's own direction (CE: bullish i.e.
    is_bearish is False; PE: bearish i.e. is_bearish is True) - no grace-
    period gating here (unlike supertrend_against_position above), per the
    task spec: "if it's a fully warmed-up signal AND currently confirms
    the position's direction ... do NOT exit"."""
    if is_bearish is None:
        return False
    return (not is_bearish) if option_type == "CE" else is_bearish


# ---------------------------------------------------------------------- #
# ATM strike resolution from the current (live) instrument master - see
# Options/dhan_client.py's get_atm_option docstring for why Tradehull's own
# ATM_Strike_Selection (live-price-only) can't be used historically here.
# ---------------------------------------------------------------------- #
_strikes_cache: dict[tuple, object] = {}


def nearest_expiry_strikes(underlying: str, option_type: str):
    """All instrument-master rows for this underlying's OPTSTK legs of the
    given option_type at the nearest listed expiry (the August 2026
    monthly - the only expiry cycle for individual stock options, no
    weeklies - still resolvable since expiry (25 Aug 2026) hadn't passed
    as of the backtest run date, 24 Aug 2026)."""
    key = (underlying, option_type)
    if key in _strikes_cache:
        return _strikes_cache[key]
    df = dhan_wrapper.instruments()
    opt = df[
        (df["SEM_EXM_EXCH_ID"] == "NSE")
        & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")
        & (df["SEM_OPTION_TYPE"] == option_type)
    ]
    opt = opt[opt["SEM_TRADING_SYMBOL"].apply(
        lambda s: dhan_wrapper._underlying_from_trading_symbol(s) == underlying
    )]
    if opt.empty:
        _strikes_cache[key] = None
        return None
    nearest_expiry = sorted(opt["SEM_EXPIRY_DATE"].unique())[0]
    result = opt[opt["SEM_EXPIRY_DATE"] == nearest_expiry]
    _strikes_cache[key] = result
    return result


def pick_atm_row(strikes_df, spot: float):
    idx = (strikes_df["SEM_STRIKE_PRICE"] - spot).abs().idxmin()
    return strikes_df.loc[idx]


_option_meta_cache: dict[str, str] = {}


def resolve_option_security_id(trading_symbol: str) -> str:
    """trading_symbol here is Dhan's SEM_CUSTOM_SYMBOL format ("RELIANCE 25
    AUG 1310 CALL"), matching what the fixed-entry JSON files store (that's
    what Tradehull/our own pipeline surfaces as trading_symbol - see
    dhan_client.py's _instrument_meta_by_security_id docstring for why)."""
    if trading_symbol in _option_meta_cache:
        return _option_meta_cache[trading_symbol]
    df = dhan_wrapper.instruments()
    row = df[df["SEM_CUSTOM_SYMBOL"] == trading_symbol]
    if row.empty:
        raise ValueError(f"No instrument found for trading_symbol={trading_symbol!r}")
    r = row.iloc[0]
    sec_id = str(int(r["SEM_SMST_SECURITY_ID"]))
    _option_meta_cache[trading_symbol] = sec_id
    return sec_id


_option_day_cache: dict[tuple, dict] = {}


def fetch_option_day(security_id: str, d) -> dict:
    key = (security_id, d)
    if key in _option_day_cache:
        return _option_day_cache[key]
    ds = d.isoformat()
    resp = retry_call(
        dhan_wrapper.client.Dhan.intraday_minute_data,
        security_id=security_id, exchange_segment="NSE_FNO", instrument_type="OPTSTK",
        from_date=ds, to_date=ds, interval=1,
    )
    data = resp.get("data") or {}
    _option_day_cache[key] = data
    return data


# ---------------------------------------------------------------------- #
# Production Position construction
# ---------------------------------------------------------------------- #
def make_position(symbol: str, trading_symbol: str, option_type: str,
                   quantity: int, entry_price: float, entry_time: datetime,
                   entry_candle_start: Optional[datetime]) -> Position:
    return Position(
        underlying_symbol=symbol,
        option_trading_symbol=trading_symbol,
        option_type=option_type,
        quantity=quantity,
        lot_size=quantity,  # not used by exit logic; quantity already lot_size*QUANTITY_LOTS
        entry_price=entry_price,
        highest_price=entry_price,
        target_price=entry_price * (1 + TARGET_PCT),
        hard_stop_loss=entry_price * (1 - STOP_LOSS_PCT),
        order_id="",
        product_type="MIS",
        opened_at=entry_time,
        supertrend_entry_candle_start=entry_candle_start,
    )
