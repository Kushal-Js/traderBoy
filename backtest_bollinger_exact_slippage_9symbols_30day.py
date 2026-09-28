"""
User request (26 Sep 2026): "Run Bollinger and SWING V3 on same data set
but with exact entry and exit conditions, considering 2% slippage also
while closing the trade and show me the PnL comparisons day and trade
wise." This is the BOLLINGER half of that comparison (see
backtest_v3_exact_slippage_9symbols_30day.py for the Swing V3 half) - same
9-symbol NSE-equity Swing watchlist, same 30-day window, same OPTIONS
basket.

UNLIKE the earlier `backtest_bollinger_vortex_9symbols_30day.py` this
session (a video-interpretation backtest, built BEFORE Bollinger existed
as a live package), this script reads config from the REAL, now-deployed
`Bollinger/config.py` module instead of re-declaring/hardcoding the same
constants locally, and adds the one real fidelity gap a fresh Explore-
agent read of `Bollinger/trading_engine.py` + `Bollinger/signals.py`
found relative to that earlier script (done fresh for this request, not
assumed from memory):

  **No Friday square-off was modeled.** Live's `_is_friday_square_off_
  time()` + its own `_monitor_tick` force every open Bollinger position
  flat by `Bollinger/config.py`'s `FRIDAY_SQUARE_OFF_TIME` (15:25 IST
  default) every Friday - all 9 symbols here are plain NSE equities
  (non-MCX, non-index), so this applies to every one of them. This is
  genuinely NEW live behavior (added the same day Bollinger itself went
  live, per that package's own docstring: "Update market hours and
  timings and Square OFF policies as we have for SWING strategy
  currently") - the earlier backtest predates it entirely, not a bug in
  that script. Added here as a forced exit, checked only after the
  existing MAX_LOSS_HIT / STOP_LOSS_HIT / TRAILING_STOP_HIT ladder has
  had its chance to fire that same tick (matches the live 2-stage check:
  `_exit_reason_for` first, `_monitor_tick`'s own square-off sweep
  second).

  Everything else - the BB-ribbon+Vortex trend filter, the pullback/
  pending-stop-order state machine, and the trailing-stop math (1/3-of-
  stop distance, 1/5-of-that step) - was CONFIRMED byte-for-byte identical
  between the earlier standalone script and the live `Bollinger/
  signals.py`/`Bollinger/trading_engine.py` by that same fresh read (that
  package's own config.py docstring's claim - "ports its indicator math
  from verbatim" - still holds today). No other changes were needed
  there; see the earlier script's own docstring for the full video-
  derivation and interpretation-call history, not repeated here.

  Known, DISCLOSED gaps still not modeled (needs live-only state, same
  category this repo always discloses rather than fakes): ATM-strike
  LIQUIDITY filtering (`get_liquid_atm_option` vs this script's plain
  nearest-strike `nearest_for_strike_ref`), the funds check, the
  duplicate-pending-order guard, and `MAX_CONCURRENT_TRADES` capacity
  (this script runs each symbol's own single-position-at-a-time replay
  independently, same as every other single-symbol-at-a-time backtest in
  this repo - it can't see whether the real 5-slot-wide live book would
  have been full).

**SLIPPAGE, SCALED INVERSELY TO PREMIUM** (updated 26 Sep 2026 - see
`backtest_v3_exact_slippage_9symbols_30day.py`'s own docstring for the
full reasoning; identical model used here for a fair comparison between
the two strategies). A flat percentage was the first pass; after the user
asked "do you think 2% slippage is fair evaluation?" and discussed why a
flat % is unrealistic (too small in rupee terms for a cheap/thin contract,
too large for an expensive/liquid one - a real spread tracks a roughly
FIXED number of exchange ticks, not a fixed percentage of premium), this
was rebuilt with a per-trade percentage instead:

    effective_pct = clamp(MIN_TICK_SLIPPAGE_RS / premium,
                           BASE_SLIPPAGE_PCT, MAX_SLIPPAGE_PCT)

`MIN_TICK_SLIPPAGE_RS=Rs 0.10` (2x NSE's Rs 0.05 options tick size) is the
real spread floor and the only line actually doing the "inverse to
premium" work; `BASE_SLIPPAGE_PCT=0.5%` stops the effective % shrinking to
nothing on expensive/liquid contracts; `MAX_SLIPPAGE_PCT=10%` caps it for
near-worthless deep-OTM contracts. ~10% at Rs 1 premium, 2% at Rs 5
(coincidentally matching the old flat rate), 0.5% at Rs 20+. These are
this script's own judgment call, not measured NSE bid-ask data (Dhan's
historical REST endpoints have no quote/depth history to calibrate
against). Every closing exit price is worsened by this per-trade
percentage before P&L is computed and before it's recorded as the trade's
`exit_price`; the strategy's own exit-decision logic and the running
best_price/trailing bookkeeping still evaluate against the clean quote -
only the recorded fill is worsened.

Run:
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_bollinger_exact_slippage_9symbols_30day.py [TEST_DAYS_BACK] [SYMBOL1,SYMBOL2,...]
"""
from __future__ import annotations

import json
import os
import sys
import time
from calendar import Calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env", override=False)

from Options import config as ocfg  # noqa: E402
from Options.dhan_client import dhan_wrapper  # noqa: E402
from Bollinger import config as bcfg  # noqa: E402  - the REAL deployed package's own config, not swing_config
from Swing import config as swing_config  # noqa: E402  - only for INDEX_SYMBOLS membership check below
from Swing.position_store import (  # noqa: E402
    unrealized_pnl_rs, hard_stop_for, price_past_hard_stop,
)


def _authenticate_avoiding_session_collision() -> None:
    handoff = os.environ.get("HANDOFF_DHAN_ACCESS_TOKEN")
    if handoff:
        ocfg.DHAN_AUTH_MODE = "access_token"
        ocfg.DHAN_ACCESS_TOKEN = handoff
        print("[backtest] using hand-off access token (access_token mode) - "
              "droplet's own live session left untouched.")
    else:
        print("[backtest] WARNING: no HANDOFF_DHAN_ACCESS_TOKEN set - authenticate() will refuse "
              "pin_totp from a local process unless ALLOW_LOCAL_PIN_TOTP=true.")
    dhan_wrapper.authenticate()


IST = ZoneInfo("Asia/Kolkata")
TEST_DAYS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DEFAULT_SYMBOLS = ["BANDHANBNK", "TORNTPHARM", "DLF", "ZYDUSLIFE", "SONACOMS", "CIPLA", "ASHOKLEY", "VEDL", "SOLARINDS"]
SYMBOLS = sys.argv[2].split(",") if len(sys.argv) > 2 else DEFAULT_SYMBOLS
SIGNAL_LOOKBACK_DAYS = 90
INSTRUMENT_FETCH_DAYS_BACK = 90
MIN_TICK_SLIPPAGE_RS = 0.10  # 2x NSE options tick size (Rs 0.05) - see docstring
BASE_SLIPPAGE_PCT = 0.005    # 0.5% floor for high-premium/liquid contracts
MAX_SLIPPAGE_PCT = 0.10      # 10% ceiling for near-worthless deep-OTM contracts


def slippage_pct_for(premium: float) -> float:
    """Inverse-to-premium slippage - see docstring's SLIPPAGE section for
    the reasoning and the exact constants."""
    if premium <= 0:
        return MAX_SLIPPAGE_PCT
    return min(MAX_SLIPPAGE_PCT, max(BASE_SLIPPAGE_PCT, MIN_TICK_SLIPPAGE_RS / premium))

# Every constant below is read LIVE from the real deployed Bollinger/config.py -
# no local re-declaration, so a future .env change to any BOLLINGER_* var is
# automatically picked up here too (the earlier standalone script pre-dated
# this package and had to declare its own copies; this one doesn't).
SIGNAL_INTERVAL_MINUTES = bcfg.SIGNAL_INTERVAL_MINUTES
BB_PERIOD = bcfg.BB_PERIOD
BB_DEVIATIONS = bcfg.BB_DEVIATIONS
VORTEX_PERIOD = bcfg.VORTEX_PERIOD
SWING_FRACTAL_LOOKBACK = bcfg.SWING_FRACTAL_LOOKBACK
MIN_PULLBACK_CANDLES = bcfg.MIN_PULLBACK_CANDLES
MIN_STOP_PCT = bcfg.MIN_STOP_PCT
TRAILING_STOP_FRACTION = bcfg.TRAILING_STOP_FRACTION
TRAILING_STEP_FRACTION = bcfg.TRAILING_STEP_FRACTION
QUANTITY_LOTS = bcfg.QUANTITY_LOTS
MAX_LOSS_PROTECTION_RS = bcfg.MAX_LOSS_PROTECTION_RS
BASKET_TYPE = bcfg.BASKET_TYPE.upper()
FRIDAY_SQUARE_OFF_ENABLED = bcfg.FRIDAY_SQUARE_OFF_ENABLED
FRIDAY_SQUARE_OFF_TIME = datetime.strptime(bcfg.FRIDAY_SQUARE_OFF_TIME, "%H:%M").time()

assert BASKET_TYPE == "OPTIONS", f"expected live BASKET_TYPE=options, got {BASKET_TYPE!r}"
_KNOWN_MCX = {"COPPER", "CRUDEOIL", "NATURALGAS"}
for _s in SYMBOLS:
    assert _s not in _KNOWN_MCX and _s not in swing_config.INDEX_SYMBOLS, f"{_s}: needs mcx/index handling"

CACHE_ROOT = REPO_ROOT / "history" / "bt_bollinger_exact_slippage_9symbols"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

print(f"[backtest] STRATEGY=bollinger_exact SYMBOLS={SYMBOLS} BASKET_TYPE={BASKET_TYPE} "
      f"TEST_DAYS_BACK={TEST_DAYS_BACK} "
      f"SLIPPAGE=inverse-to-premium(tick=Rs{MIN_TICK_SLIPPAGE_RS},floor={BASE_SLIPPAGE_PCT:.1%},cap={MAX_SLIPPAGE_PCT:.0%}) "
      f"BB_PERIOD={BB_PERIOD} BB_DEVIATIONS={BB_DEVIATIONS} VORTEX_PERIOD={VORTEX_PERIOD} "
      f"SWING_FRACTAL_LOOKBACK={SWING_FRACTAL_LOOKBACK} MIN_PULLBACK_CANDLES={MIN_PULLBACK_CANDLES} "
      f"MIN_STOP_PCT={MIN_STOP_PCT} TRAILING_STOP_FRACTION={TRAILING_STOP_FRACTION:.3f} "
      f"TRAILING_STEP_FRACTION={TRAILING_STEP_FRACTION:.3f} MAX_LOSS_PROTECTION_RS={MAX_LOSS_PROTECTION_RS} "
      f"FRIDAY_SQUARE_OFF_ENABLED={FRIDAY_SQUARE_OFF_ENABLED} FRIDAY_SQUARE_OFF_TIME={FRIDAY_SQUARE_OFF_TIME} "
      f"NO_TARGET=True (trailing-stop-only, matches live)")


# --------------------------------------------------------------------------- #
# Indicator math - confirmed byte-identical to Bollinger/signals.py by a
# fresh read done for this request (see docstring above)
# --------------------------------------------------------------------------- #
def compute_sma(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def compute_bb_ribbon(closes: list[float], period: int, deviations: tuple[float, ...]):
    n = len(closes)
    sma = compute_sma(closes, period)
    std: list[float | None] = [None] * n
    for i in range(period - 1, n):
        m = sma[i]
        variance = sum((c - m) ** 2 for c in closes[i - period + 1:i + 1]) / period
        std[i] = variance ** 0.5
    bands = {}
    for dev in deviations:
        upper = [None if sma[i] is None else sma[i] + dev * std[i] for i in range(n)]
        lower = [None if sma[i] is None else sma[i] - dev * std[i] for i in range(n)]
        bands[dev] = (upper, lower)
    return sma, bands


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(n):
        tr[i] = (highs[i] - lows[i]) if i == 0 else max(
            highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return tr


def compute_vortex(highs: list[float], lows: list[float], closes: list[float], period: int):
    n = len(closes)
    tr = true_range(highs, lows, closes)
    vm_plus = [0.0] * n
    vm_minus = [0.0] * n
    for i in range(1, n):
        vm_plus[i] = abs(highs[i] - lows[i - 1])
        vm_minus[i] = abs(lows[i] - highs[i - 1])
    vi_plus: list[float | None] = [None] * n
    vi_minus: list[float | None] = [None] * n
    for i in range(period, n):
        sum_tr = sum(tr[i - period + 1:i + 1])
        if sum_tr > 0:
            vi_plus[i] = sum(vm_plus[i - period + 1:i + 1]) / sum_tr
            vi_minus[i] = sum(vm_minus[i - period + 1:i + 1]) / sum_tr
    return vi_plus, vi_minus


def compute_fractal_swings(highs: list[float], lows: list[float], lookback: int):
    n = len(highs)
    swing_high = [False] * n
    swing_low = [False] * n
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback:i + lookback + 1]
        window_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swing_high[i] = True
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swing_low[i] = True
    return swing_high, swing_low


# --------------------------------------------------------------------------- #
# Candle / option-contract plumbing
# --------------------------------------------------------------------------- #
def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def fetch_equity_cached(symbol: str, cache_dir: Path, interval_minutes: int, days_back: int) -> dict:
    # Date-stamped (28 Sep 2026 fix - real bug found: two symbols in a
    # baseline run silently reused a 2-day-old cache from an earlier,
    # unrelated backtest that happened to fetch the same symbol, because
    # the cache key had no date in it at all. "Last 30 days" must always
    # mean 30 days back from TODAY, not from whenever the cache file
    # happened to be written - a stale file is now a same-day-only cache,
    # never silently reused across days.
    today_str = datetime.now(IST).strftime("%Y%m%d")
    cache_file = cache_dir / f"{symbol}_{interval_minutes}min_{days_back}d_{today_str}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    security_id = dhan_wrapper._equity_security_id(symbol)
    result = dhan_wrapper.fetch_continuous_intraday(
        security_id, "NSE_EQ", "EQUITY", interval_minutes, lookback_days_override=days_back)
    result = {"opens": result.get("open") or [], "highs": result.get("high") or [], "lows": result.get("low") or [],
              "closes": result.get("close") or [], "volumes": result.get("volume") or [],
              "timestamps": result.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def previous_monthly_expiry_cutoff(front_expiry_str: str) -> date:
    front = datetime.strptime(front_expiry_str[:10], "%Y-%m-%d").date()
    prev_month_last_day = front.replace(day=1) - timedelta(days=1)
    cal_days = [d for d in Calendar().itermonthdates(prev_month_last_day.year, prev_month_last_day.month)
                if d.month == prev_month_last_day.month]
    thursdays = [d for d in cal_days if d.weekday() == 3]
    return thursdays[-1]


def resolve_option_universe(symbol: str):
    df = dhan_wrapper.instruments()
    opts = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")]
    matches = opts[opts["SEM_TRADING_SYMBOL"].apply(
        lambda s: dhan_wrapper._underlying_from_trading_symbol(str(s)) == symbol
    )]
    if matches.empty:
        raise ValueError(f"No option contracts found for {symbol}")
    nearest_expiry = matches["SEM_EXPIRY_DATE"].min()
    front = matches[matches["SEM_EXPIRY_DATE"] == nearest_expiry].copy()
    return front, str(nearest_expiry)


def nearest_for_strike_ref(front_df, option_type: str, ref_price: float) -> dict:
    matches = front_df[front_df["SEM_OPTION_TYPE"] == option_type].copy()
    matches["dist"] = (matches["SEM_STRIKE_PRICE"] - ref_price).abs()
    row = matches.sort_values("dist").iloc[0]
    return {
        "security_id": str(int(row["SEM_SMST_SECURITY_ID"])), "trading_symbol": str(row["SEM_CUSTOM_SYMBOL"]),
        "strike": float(row["SEM_STRIKE_PRICE"]), "lot_size": int(float(row["SEM_LOT_UNITS"])),
    }


def fetch_option_1min_cached(cache_dir: Path, security_id: str) -> dict:
    # Same date-stamping fix as fetch_equity_cached above - see its comment.
    today_str = datetime.now(IST).strftime("%Y%m%d")
    cache_file = cache_dir / f"OPT_{security_id}_1min_{today_str}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    try:
        data = dhan_wrapper.fetch_continuous_intraday(
            security_id, "NSE_FNO", "OPTSTK", 1, lookback_days_override=INSTRUMENT_FETCH_DAYS_BACK)
    except Exception as exc:  # noqa: BLE001
        print(f"    OPTION FETCH FAILED for {security_id}: {exc}")
        data = {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def price_at_or_before(ts_list, closes, target_ts):
    idx = idx_at_or_before(ts_list, target_ts)
    return closes[idx] if idx is not None else None


# --------------------------------------------------------------------------- #
# Strategy signal computation - unchanged from the earlier script, confirmed
# byte-identical to Bollinger/signals.py's own _replay
# --------------------------------------------------------------------------- #
def compute_signals(fast: dict):
    closes, highs, lows = fast["closes"], fast["highs"], fast["lows"]
    n = len(closes)
    sma, _bands = compute_bb_ribbon(closes, BB_PERIOD, BB_DEVIATIONS)
    vi_plus, vi_minus = compute_vortex(highs, lows, closes, VORTEX_PERIOD)
    swing_high, swing_low = compute_fractal_swings(highs, lows, SWING_FRACTAL_LOOKBACK)

    valid_bullish = [False] * n
    valid_bearish = [False] * n
    for i in range(n):
        if sma[i] is None or vi_plus[i] is None or vi_minus[i] is None:
            continue
        bb_side_bullish = closes[i] > sma[i]
        bb_side_bearish = closes[i] < sma[i]
        vortex_bullish = vi_plus[i] > vi_minus[i]
        vortex_bearish = vi_minus[i] > vi_plus[i]
        valid_bullish[i] = bb_side_bullish and vortex_bullish
        valid_bearish[i] = bb_side_bearish and vortex_bearish

    return {"valid_bullish": valid_bullish, "valid_bearish": valid_bearish,
            "swing_high": swing_high, "swing_low": swing_low}


class PendingOrder:
    __slots__ = ("side", "trigger_price", "stop_price", "armed_since_idx")

    def __init__(self, side: str, trigger_price: float, stop_price: float, armed_since_idx: int):
        self.side = side
        self.trigger_price = trigger_price
        self.stop_price = stop_price
        self.armed_since_idx = armed_since_idx


def run_backtest(symbol, signals, fast, master_equity, test_days, front_df, tradable_from, option_cache, cache_dir):
    trades = []
    skipped_entries = []
    position = None
    pending: PendingOrder | None = None
    last_swing_high_since_bullish = None
    last_swing_low_since_bearish = None
    running_pullback_extreme = None

    fast_ts = fast["timestamps"]
    valid_bullish, valid_bearish = signals["valid_bullish"], signals["valid_bearish"]
    swing_high, swing_low = signals["swing_high"], signals["swing_low"]
    n = len(fast_ts)

    def price_at(t, ts_list, closes):
        return price_at_or_before(ts_list, closes, t)

    def close_position(dt, clean_premium, reason):
        slip_pct = slippage_pct_for(clean_premium)  # LONG on the premium regardless of CE/PE
        slipped_price = clean_premium * (1 - slip_pct)
        pnl = unrealized_pnl_rs("LONG", position["entry_price"], slipped_price, position["pnl_multiplier"])
        print(f"  EXIT  {position['option_type']} @ {dt} reason={reason} quote={clean_premium:.2f} "
              f"filled={slipped_price:.2f} (slip={slip_pct:.2%}) pnl={pnl:+.0f}")
        trades.append({
            "symbol": symbol, "label": position["option_type"], "entry_dt": str(position["entry_dt"]),
            "entry_price": position["entry_price"], "exit_dt": str(dt), "exit_price": slipped_price,
            "exit_price_quote": clean_premium, "slippage_pct": slip_pct, "exit_reason": reason,
            "pnl_multiplier": position["pnl_multiplier"],
            "pnl": pnl, "status": "closed", "trading_symbol": position["opt"]["trading_symbol"],
        })

    master_ts = [t for t in master_equity["timestamps"] if datetime.fromtimestamp(t, tz=IST).date() in test_days]

    # Real data-availability bug found and fixed while validating the v3 half
    # of this comparison (backtest_v3_exact_slippage_9symbols_30day.py's own
    # comment has the full story): Dhan's 1-min NSE_EQ series stops at 15:14
    # every day, so a plain `dt.time() >= FRIDAY_SQUARE_OFF_TIME(15:25)` check
    # can never fire. Same fix applied here for consistency between the two
    # halves of this comparison - force-close at the last available bar of a
    # Friday too, not just the (unreachable) time check.
    last_ts_of_date: dict = {}
    for t in master_ts:
        d = datetime.fromtimestamp(t, tz=IST).date()
        last_ts_of_date[d] = t

    def entry_signal_for_bar(sig_idx):
        nonlocal pending, last_swing_high_since_bullish, last_swing_low_since_bearish, running_pullback_extreme
        i = sig_idx
        if i < SWING_FRACTAL_LOOKBACK:
            return None
        j = i - SWING_FRACTAL_LOOKBACK

        if swing_high[j] and valid_bullish[j]:
            last_swing_high_since_bullish = (j, fast["highs"][j])
        if swing_low[j] and valid_bearish[j]:
            last_swing_low_since_bearish = (j, fast["lows"][j])
        if not valid_bullish[i]:
            last_swing_high_since_bullish = None
        if not valid_bearish[i]:
            last_swing_low_since_bearish = None

        if pending is not None:
            still_valid = valid_bullish[i] if pending.side == "BULLISH" else valid_bearish[i]
            if not still_valid:
                pending = None
                running_pullback_extreme = None

        if valid_bullish[i] and last_swing_high_since_bullish is not None:
            swing_idx, swing_price = last_swing_high_since_bullish
            if i > swing_idx:
                closes_since = fast["closes"][swing_idx:i + 1]
                down_streak = 0
                for k in range(1, len(closes_since)):
                    if closes_since[k] < closes_since[k - 1]:
                        down_streak += 1
                    else:
                        down_streak = 0
                if down_streak >= MIN_PULLBACK_CANDLES:
                    if pending is None or pending.side != "BULLISH":
                        pending = PendingOrder("BULLISH", swing_price, fast["lows"][i], i)
                        running_pullback_extreme = fast["lows"][i]
                    else:
                        if swing_price > pending.trigger_price:
                            pending.trigger_price = swing_price
                        running_pullback_extreme = min(running_pullback_extreme, fast["lows"][i])
                        pending.stop_price = running_pullback_extreme

        if valid_bearish[i] and last_swing_low_since_bearish is not None:
            swing_idx, swing_price = last_swing_low_since_bearish
            if i > swing_idx:
                closes_since = fast["closes"][swing_idx:i + 1]
                up_streak = 0
                for k in range(1, len(closes_since)):
                    if closes_since[k] > closes_since[k - 1]:
                        up_streak += 1
                    else:
                        up_streak = 0
                if up_streak >= MIN_PULLBACK_CANDLES:
                    if pending is None or pending.side != "BEARISH":
                        pending = PendingOrder("BEARISH", swing_price, fast["highs"][i], i)
                        running_pullback_extreme = fast["highs"][i]
                    else:
                        if swing_price < pending.trigger_price:
                            pending.trigger_price = swing_price
                        running_pullback_extreme = max(running_pullback_extreme, fast["highs"][i])
                        pending.stop_price = running_pullback_extreme

        if pending is not None:
            if pending.side == "BULLISH" and fast["highs"][i] >= pending.trigger_price:
                fired = pending
                pending = None
                running_pullback_extreme = None
                last_swing_high_since_bullish = None
                return "BULLISH", fired
            if pending.side == "BEARISH" and fast["lows"][i] <= pending.trigger_price:
                fired = pending
                pending = None
                running_pullback_extreme = None
                last_swing_low_since_bearish = None
                return "BEARISH", fired
        return None

    processed_sig_idx = -1
    for t in master_ts:
        dt = datetime.fromtimestamp(t, tz=IST)
        sig_idx = idx_at_or_before(fast_ts, t)
        if sig_idx is None:
            continue

        if position is not None:
            opt_ts_list, opt_closes = position["opt_data"]["timestamps"], position["opt_data"]["closes"]
            premium = price_at(t, opt_ts_list, opt_closes)
            if premium is not None:
                if premium > position["best_price"]:
                    position["best_price"] = premium
                loss_rs = -unrealized_pnl_rs("LONG", position["entry_price"], premium, position["pnl_multiplier"])
                reason = None
                if loss_rs >= MAX_LOSS_PROTECTION_RS:
                    reason = "MAX_LOSS_HIT"
                else:
                    favorable_move = position["best_price"] - position["entry_price"]
                    if not position["trailing_armed"] and favorable_move >= position["trailing_stop_dist"]:
                        position["trailing_armed"] = True
                        position["trailing_stop_price"] = position["best_price"] - position["trailing_stop_dist"]
                    elif position["trailing_armed"]:
                        candidate = position["best_price"] - position["trailing_stop_dist"]
                        if candidate - position["trailing_stop_price"] >= position["trailing_step"]:
                            position["trailing_stop_price"] = candidate
                    active_stop = position["trailing_stop_price"] if position["trailing_armed"] else position["hard_stop_loss"]
                    if price_past_hard_stop("LONG", premium, active_stop):
                        reason = "TRAILING_STOP_HIT" if position["trailing_armed"] else "STOP_LOSS_HIT"
                    # New vs the earlier script (fidelity fix) - Friday square-off,
                    # checked last, same placement convention as the v3 half of
                    # this comparison. Last-bar-of-Friday fallback required - see
                    # last_ts_of_date's own comment above.
                    if (reason is None and FRIDAY_SQUARE_OFF_ENABLED and dt.weekday() == 4
                            and (dt.time() >= FRIDAY_SQUARE_OFF_TIME or t == last_ts_of_date.get(dt.date()))):
                        reason = "FRIDAY_SQUARE_OFF"
                if reason:
                    close_position(dt, premium, reason)
                    position = None

        fired = None
        while processed_sig_idx < sig_idx:
            processed_sig_idx += 1
            result = entry_signal_for_bar(processed_sig_idx)
            if result is not None and processed_sig_idx == sig_idx:
                fired = result

        if position is None and fired is not None:
            entry_signal, fired_order = fired
            equity_price = price_at(t, master_equity["timestamps"], master_equity["closes"])
            if dt.date() <= tradable_from - timedelta(days=1):
                print(f"  SKIP  {dt} signal={entry_signal} reason=expired_contract_unavailable")
                skipped_entries.append((str(dt), "expired_contract_unavailable"))
                continue
            option_type = "CE" if entry_signal == "BULLISH" else "PE"
            try:
                opt = nearest_for_strike_ref(front_df, option_type, equity_price)
                if opt["security_id"] not in option_cache:
                    option_cache[opt["security_id"]] = fetch_option_1min_cached(cache_dir, opt["security_id"])
                opt_data = option_cache[opt["security_id"]]
            except Exception as exc:  # noqa: BLE001
                print(f"  {dt}: SKIPPED entry signal={entry_signal} - could not resolve/fetch ATM {option_type} ({exc})")
                skipped_entries.append((str(dt), str(exc)))
                continue
            entry_price = price_at(t, opt_data["timestamps"], opt_data["closes"])
            if entry_price is None or equity_price is None:
                print(f"  {dt}: SKIPPED entry signal={entry_signal} - no option/underlying price data at entry time")
                skipped_entries.append((str(dt), "no_option_price_data"))
                continue

            stop_distance_underlying = abs(fired_order.trigger_price - fired_order.stop_price)
            stop_pct = stop_distance_underlying / equity_price if equity_price else 0.0
            stop_pct = max(stop_pct, MIN_STOP_PCT)
            hard_stop_loss = hard_stop_for("LONG", entry_price, stop_pct)
            trailing_stop_dist = entry_price * stop_pct * TRAILING_STOP_FRACTION
            trailing_step = trailing_stop_dist * TRAILING_STEP_FRACTION

            qty = opt["lot_size"] * QUANTITY_LOTS
            print(f"  ENTER {option_type} @ {dt} signal={entry_signal} contract={opt['trading_symbol']} "
                  f"entry_price={entry_price} stop_pct={stop_pct:.3%} trigger={fired_order.trigger_price:.2f} "
                  f"swing_stop={fired_order.stop_price:.2f}")
            position = {
                "option_type": option_type, "entry_dt": dt, "entry_price": entry_price,
                "best_price": entry_price, "hard_stop_loss": hard_stop_loss,
                "trailing_stop_dist": trailing_stop_dist, "trailing_step": trailing_step,
                "trailing_armed": False, "trailing_stop_price": None,
                "opt": opt, "opt_data": opt_data, "pnl_multiplier": qty,
            }

    if position is not None:
        trades.append({
            "symbol": symbol, "label": position["option_type"], "entry_dt": str(position["entry_dt"]),
            "entry_price": position["entry_price"],
            "status": "still open through end of available data", "pnl": None,
        })
    if skipped_entries:
        print(f"[backtest] {symbol}: {len(skipped_entries)} entries skipped (expired contract/no price data)")
    return trades


def day_wise(trades):
    d = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for tr in trades:
        pnl = tr.get("pnl")
        if pnl is None:
            continue
        day = tr["entry_dt"][:10]
        d[day]["pnl"] += pnl
        d[day]["trades"] += 1
        if pnl > 0:
            d[day]["wins"] += 1
        elif pnl < 0:
            d[day]["losses"] += 1
    return d


def print_day_wise(name, trades):
    dw = day_wise(trades)
    print(f"\n=== [{name}] DAY-WISE P&L ===")
    running = 0.0
    for d in sorted(dw):
        s = dw[d]
        running += s["pnl"]
        print(f"{d}: trades={s['trades']:2d} wins={s['wins']:2d} losses={s['losses']:2d} "
              f"net_pnl={s['pnl']:+.0f} running_total={running:+.0f}")
    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    total = sum(t["pnl"] for t in closed)
    print(f"[{name}] SUMMARY: trades={len(trades)} (closed={len(closed)}, open={len(trades)-len(closed)}) "
          f"wins={len(wins)} losses={len(losses)} win_rate={(len(wins)/len(closed)*100) if closed else 0:.1f}% "
          f"net_pnl=Rs {total:+.0f}")
    return {"trades": len(trades), "closed": len(closed), "wins": len(wins), "losses": len(losses), "total": total}


def print_trade_wise(name, trades):
    print(f"\n=== [{name}] TRADE-WISE P&L ===")
    print(f"{'Entry':<17} {'Symbol':<11} {'Type':<4} {'Entry':>8} {'Exit(slip)':>10} {'Reason':<18} {'PnL':>10}")
    for tr in trades:
        exit_p = tr.get("exit_price")
        print(f"{tr['entry_dt'][:16]:<17} {tr['symbol']:<11} {tr.get('label',''):<4} "
              f"{tr['entry_price']:>8.2f} {(exit_p if exit_p is not None else 0):>10.2f} "
              f"{str(tr.get('exit_reason')):<18} {(tr['pnl'] if tr.get('pnl') is not None else 0):>+10.0f}")


def run_symbol(symbol: str):
    cache_dir = CACHE_ROOT / symbol.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'=' * 20} {symbol} {'=' * 20}")

    fast = fetch_equity_cached(symbol, cache_dir, SIGNAL_INTERVAL_MINUTES, SIGNAL_LOOKBACK_DAYS)
    master_equity = fetch_equity_cached(symbol, cache_dir, 1, SIGNAL_LOOKBACK_DAYS)
    print(f"  fast({SIGNAL_INTERVAL_MINUTES}min): {len(fast['closes'])} bars, master(1min): {len(master_equity['closes'])} bars")

    signals = compute_signals(fast)

    front_df, front_expiry = resolve_option_universe(symbol)
    tradable_from = previous_monthly_expiry_cutoff(front_expiry) + timedelta(days=1)
    print(f"[backtest] {symbol}: front-month option expiry {front_expiry} - TRADABLE_FROM_DATE={tradable_from}")

    all_days = sorted({datetime.fromtimestamp(t, tz=IST).date() for t in fast["timestamps"]})
    test_days = set(all_days[-TEST_DAYS_BACK:])
    print(f"[backtest] {symbol}: testing entries over the last {TEST_DAYS_BACK} trading days: "
          f"{sorted(test_days)[0]} to {sorted(test_days)[-1]}")

    option_cache: dict = {}
    trades = run_backtest(symbol, signals, fast, master_equity, test_days, front_df, tradable_from, option_cache, cache_dir)
    return trades


def main():
    _authenticate_avoiding_session_collision()
    all_trades = []
    per_symbol_summary = {}
    for symbol in SYMBOLS:
        trades = run_symbol(symbol)
        s = print_day_wise(symbol, trades)
        print_trade_wise(symbol, trades)
        per_symbol_summary[symbol] = s
        all_trades.extend(trades)

    (CACHE_ROOT / "results_combined.json").write_text(json.dumps(all_trades, default=str, indent=2))

    combined = print_day_wise(f"COMBINED ({'+'.join(SYMBOLS)})", all_trades)
    print_trade_wise("COMBINED - ALL SYMBOLS, CHRONOLOGICAL BY SYMBOL", all_trades)

    print(f"\n{'=' * 25} PER-SYMBOL SUMMARY (bollinger_exact, inverse-to-premium slippage) {'=' * 25}")
    print(f"{'Symbol':<12} {'Trades':>7} {'Wins':>5} {'Losses':>7} {'WinRate':>8} {'NetP&L':>12}")
    for symbol, s in per_symbol_summary.items():
        wr = (s['wins'] / s['closed'] * 100) if s['closed'] else 0
        print(f"{symbol:<12} {s['trades']:>7} {s['wins']:>5} {s['losses']:>7} {wr:>7.1f}% Rs{s['total']:>+10.0f}")
    print(f"{'COMBINED':<12} {combined['trades']:>7} {combined['wins']:>5} {combined['losses']:>7} "
          f"{(combined['wins']/combined['closed']*100) if combined['closed'] else 0:>7.1f}% "
          f"Rs{combined['total']:>+10.0f}")


if __name__ == "__main__":
    main()
