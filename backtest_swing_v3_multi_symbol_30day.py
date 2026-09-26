"""
User request 25 Sep 2026: "backtest SOLARINDS and VEDL with SWING v3
strategy and show me day wise and trade wise PnL reports for last 30
days." Generalized version of backtest_swing_ashokley_v3_30day.py (24
Sep 2026) - identical methodology, SYMBOL hardcoding replaced with a
CLI-driven symbol list so multiple NSE-equity symbols can be backtested
in one run with per-symbol AND combined reports. See that script's own
docstring for the full design rationale (live v2/"v3" formula - LEVEL-
based regime leg + Day Range Bull/Bear branch B, current_target_pct's
real 0.35 non-COPPER target, the generalized regime re-entry gate) -
not re-explained here, this file is otherwise unchanged logic.

SOLARINDS/VEDL are both plain NSE equity F&O stocks (like ASHOKLEY/
SONACOMS before them) - same "equity" resolution path, no MCX/index
handling needed.

Run ON THE DROPLET or locally with a hand-off token:
    HANDOFF_DHAN_ACCESS_TOKEN=... uv run python backtest_swing_v3_multi_symbol_30day.py [TEST_DAYS_BACK] [SYMBOL1,SYMBOL2,...]
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
from Options.dhan_client import dhan_wrapper, _compute_ema, _compute_supertrend, _compute_rsi  # noqa: E402
from Swing import config as swing_config  # noqa: E402
from Swing.trading_engine import current_target_pct  # noqa: E402
from Swing.position_store import (  # noqa: E402
    unrealized_pnl_rs, target_price_for, hard_stop_for,
    price_past_target, price_past_hard_stop, giveback_floor, price_past_giveback_floor,
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
SYMBOLS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["SOLARINDS", "VEDL"]
SIGNAL_LOOKBACK_DAYS = 90
INSTRUMENT_FETCH_DAYS_BACK = 90

CACHE_ROOT = REPO_ROOT / "history" / "bt_swing_v3_multi_symbol"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

QUANTITY_LOTS = swing_config.QUANTITY_LOTS
MAX_LOSS_PROTECTION_RS = swing_config.MAX_LOSS_PROTECTION_RS
PROFIT_PROTECTION_RS_OPTIONS = swing_config.PROFIT_PROTECTION_RS_OPTIONS
PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS = swing_config.PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS
HARD_STOP_LOSS_PCT = swing_config.HARD_STOP_LOSS_PCT
ENABLE_TARGET_EXIT = swing_config.ENABLE_TARGET_EXIT
REGIME_FAST_INTERVAL_MINUTES = swing_config.REGIME_FAST_INTERVAL_MINUTES  # 5
REGIME_SLOW_INTERVAL_MINUTES = swing_config.REGIME_SLOW_INTERVAL_MINUTES  # 15
REGIME_EMA_PERIOD = swing_config.REGIME_EMA_PERIOD  # 200
REGIME_GAP_WIDENING_LOOKBACK_CANDLES = swing_config.REGIME_GAP_WIDENING_LOOKBACK_CANDLES  # 8
SUPERTREND_PERIOD = swing_config.SUPERTREND_PERIOD      # 10
SUPERTREND_MULTIPLIER = swing_config.SUPERTREND_MULTIPLIER  # 3.0
NSE_VOLUME_FLOOR_GATE_ENABLED = swing_config.NSE_VOLUME_FLOOR_GATE_ENABLED
NSE_VOLUME_FLOOR_RATIO_MIN = swing_config.NSE_VOLUME_FLOOR_RATIO_MIN
DAY_RANGE_RSI_PERIOD = swing_config.DAY_RANGE_RSI_PERIOD          # 14
DAY_RANGE_RSI_BULL_LEVEL = swing_config.DAY_RANGE_RSI_BULL_LEVEL  # 60
DAY_RANGE_RSI_BEAR_LEVEL = swing_config.DAY_RANGE_RSI_BEAR_LEVEL  # 40
BASKET_TYPE = swing_config.BASKET_TYPE.upper()

assert BASKET_TYPE == "OPTIONS", f"expected live BASKET_TYPE=options, got {BASKET_TYPE!r}"
for _s in SYMBOLS:
    assert _s not in swing_config.INDEX_SYMBOLS, f"{_s}: needs index handling"
assert swing_config.ENTRY_STRATEGY_VERSION in ("v2", "v3"), (
    f"expected live SWING_ENTRY_STRATEGY_VERSION in (v2, v3) - v3 is a literal alias for v2, byte-identical "
    f"behavior (see commit 34c0103) - got {swing_config.ENTRY_STRATEGY_VERSION!r}"
)

print(f"[backtest] SYMBOLS={SYMBOLS} BASKET_TYPE={BASKET_TYPE} TEST_DAYS_BACK={TEST_DAYS_BACK} "
      f"ENTRY_STRATEGY_VERSION={swing_config.ENTRY_STRATEGY_VERSION} "
      f"(live v2/'v3' formula: LEVEL regime + Day Range branch B, generalized re-entry gate)")


def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def last_closed_idx(ts_list, cutoff_ts, interval_minutes):
    interval_s = interval_minutes * 60
    best = None
    for i, ts in enumerate(ts_list):
        if ts + interval_s <= cutoff_ts:
            best = i
        else:
            break
    return best


def aligned_gap(fast_ema, fast_ts, fast_idx, slow_ema, slow_ts):
    if fast_idx < 0 or fast_idx >= len(fast_ema) or fast_ema[fast_idx] is None:
        return None
    cutoff = fast_ts[fast_idx] + REGIME_FAST_INTERVAL_MINUTES * 60
    idx = last_closed_idx(slow_ts, cutoff, REGIME_SLOW_INTERVAL_MINUTES)
    if idx is None or slow_ema[idx] is None:
        return None
    return fast_ema[fast_idx] - slow_ema[idx]


def fetch_equity_cached(symbol: str, cache_dir: Path, interval_minutes: int, days_back: int) -> dict:
    cache_file = cache_dir / f"{symbol}_{interval_minutes}min_{days_back}d.json"
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
    cache_file = cache_dir / f"OPT_{security_id}_1min.json"
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


def compute_supertrend_cross_series(highs, lows, closes):
    st = _compute_supertrend(highs, lows, closes, period=SUPERTREND_PERIOD, multiplier=SUPERTREND_MULTIPLIER)
    n = len(closes)
    is_above = [None] * n
    crossed_above = [False] * n
    crossed_below = [False] * n
    for i in range(n):
        if st[i] is not None:
            is_above[i] = closes[i] > st[i]
    for i in range(1, n):
        if is_above[i] is None or is_above[i - 1] is None:
            continue
        if (not is_above[i - 1]) and is_above[i]:
            crossed_above[i] = True
        if is_above[i - 1] and not is_above[i]:
            crossed_below[i] = True
    return st, is_above, crossed_above, crossed_below


def volume_ratio_at(volumes, idx, lookback=20):
    if idx < lookback or idx >= len(volumes):
        return None
    window = volumes[idx - lookback:idx]
    avg = sum(window) / len(window) if window else 0.0
    return (volumes[idx] / avg) if avg else None


def run_backtest(symbol, target_pct, filter_bullish, filter_bearish, branch_b_bullish, branch_b_bearish, is_bullish,
                  fast_ts, crossed_above5, crossed_below5, vol_ratio5, master_equity, test_days,
                  front_df, tradable_from, option_cache, cache_dir):
    trades = []
    skipped_entries = []
    position = None
    consumed_signal_idx = None
    consumed_side = None

    def price_at_or_before(ts_list, closes, target_ts):
        idx = idx_at_or_before(ts_list, target_ts)
        return closes[idx] if idx is not None else None

    master_ts = [t for t in master_equity["timestamps"] if datetime.fromtimestamp(t, tz=IST).date() in test_days]

    for t in master_ts:
        dt = datetime.fromtimestamp(t, tz=IST)
        sig_idx = idx_at_or_before(fast_ts, t)
        if sig_idx is None:
            continue

        side_now = None
        if is_bullish[sig_idx] is not None:
            side_now = 1 if is_bullish[sig_idx] else -1
        if side_now is not None and consumed_side is not None and side_now != consumed_side:
            consumed_side = None

        if position is not None:
            opt_ts_list, opt_closes = position["opt_data"]["timestamps"], position["opt_data"]["closes"]
            premium = price_at_or_before(opt_ts_list, opt_closes, t)
            if premium is not None:
                position["best_price"] = max(position["best_price"], premium)
                loss_rs = -unrealized_pnl_rs("LONG", position["entry_price"], premium, position["pnl_multiplier"])
                reason = None
                if loss_rs >= MAX_LOSS_PROTECTION_RS:
                    reason = "MAX_LOSS_HIT"
                elif ENABLE_TARGET_EXIT and price_past_target("LONG", premium, position["target_price"]):
                    reason = "TARGET_HIT"
                else:
                    peak_profit_rs = unrealized_pnl_rs("LONG", position["entry_price"],
                                                        position["best_price"], position["pnl_multiplier"])
                    if peak_profit_rs > PROFIT_PROTECTION_RS_OPTIONS:
                        floor = giveback_floor("LONG", position["best_price"], PROFIT_PROTECTION_GIVEBACK_PCT_OPTIONS)
                        if price_past_giveback_floor("LONG", premium, floor):
                            reason = "PROFIT_PROTECTION_HIT"
                    if reason is None and price_past_hard_stop("LONG", premium, position["hard_stop_loss"]):
                        reason = "STOP_LOSS_HIT"
                    if (reason is None and sig_idx is not None
                            and fast_ts[sig_idx] > position["entry_candle_ts"]):
                        option_type = position["option_type"]
                        if option_type == "CE" and crossed_below5[sig_idx]:
                            reason = "SUPERTREND_REVERSAL"
                        elif option_type == "PE" and crossed_above5[sig_idx]:
                            reason = "SUPERTREND_REVERSAL"
                if reason:
                    pnl = unrealized_pnl_rs("LONG", position["entry_price"], premium, position["pnl_multiplier"])
                    print(f"  EXIT  {position['option_type']} @ {dt} reason={reason} exit_price={premium:.2f} pnl={pnl:+.0f}")
                    trades.append({
                        "symbol": symbol, "label": position["option_type"], "entry_dt": str(position["entry_dt"]),
                        "entry_price": position["entry_price"], "exit_dt": str(dt), "exit_price": premium,
                        "exit_reason": reason, "pnl_multiplier": position["pnl_multiplier"], "pnl": pnl,
                        "status": "closed", "trading_symbol": position["opt"]["trading_symbol"],
                        "entry_branch": position["entry_branch"],
                    })
                    position = None

        if position is None and sig_idx != consumed_signal_idx:
            entry_signal = None
            entry_branch = None
            if side_now is not None and consumed_side is not None and side_now == consumed_side:
                pass
            elif filter_bullish[sig_idx] and crossed_above5[sig_idx]:
                entry_signal, entry_branch = "BULLISH", "branch_a"
            elif filter_bearish[sig_idx] and crossed_below5[sig_idx]:
                entry_signal, entry_branch = "BEARISH", "branch_a"
            elif branch_b_bullish[sig_idx]:
                entry_signal, entry_branch = "BULLISH", "branch_b_day_range"
            elif branch_b_bearish[sig_idx]:
                entry_signal, entry_branch = "BEARISH", "branch_b_day_range"
            if entry_signal:
                consumed_signal_idx = sig_idx
                if NSE_VOLUME_FLOOR_GATE_ENABLED:
                    vr = vol_ratio5[sig_idx]
                    if vr is not None and vr < NSE_VOLUME_FLOOR_RATIO_MIN:
                        print(f"  SKIP  {dt} signal={entry_signal} reason=nse_volume_floor_gate vol_ratio={vr:.3f}")
                        continue
                if dt.date() <= tradable_from - timedelta(days=1):
                    print(f"  SKIP  {dt} signal={entry_signal} reason=expired_contract_unavailable")
                    skipped_entries.append((str(dt), "expired_contract_unavailable"))
                    continue
                option_type = "CE" if entry_signal == "BULLISH" else "PE"
                equity_price = price_at_or_before(master_equity["timestamps"], master_equity["closes"], t)
                try:
                    opt = nearest_for_strike_ref(front_df, option_type, equity_price)
                    if opt["security_id"] not in option_cache:
                        option_cache[opt["security_id"]] = fetch_option_1min_cached(cache_dir, opt["security_id"])
                    opt_data = option_cache[opt["security_id"]]
                except Exception as exc:  # noqa: BLE001
                    print(f"  {dt}: SKIPPED entry signal={entry_signal} - could not resolve/fetch ATM {option_type} ({exc})")
                    skipped_entries.append((str(dt), str(exc)))
                    continue
                entry_price = price_at_or_before(opt_data["timestamps"], opt_data["closes"], t)
                if entry_price is None:
                    print(f"  {dt}: SKIPPED entry signal={entry_signal} - no option price data at entry time")
                    skipped_entries.append((str(dt), "no_option_price_data"))
                    continue
                qty = opt["lot_size"] * QUANTITY_LOTS
                target_price = target_price_for("LONG", entry_price, target_pct)
                hard_stop_loss = hard_stop_for("LONG", entry_price, HARD_STOP_LOSS_PCT)
                print(f"  ENTER {option_type} @ {dt} signal={entry_signal} branch={entry_branch} "
                      f"contract={opt['trading_symbol']} entry_price={entry_price}")
                position = {
                    "option_type": option_type, "entry_dt": dt, "entry_price": entry_price,
                    "best_price": entry_price, "target_price": target_price, "hard_stop_loss": hard_stop_loss,
                    "entry_candle_ts": fast_ts[sig_idx], "opt": opt, "opt_data": opt_data,
                    "pnl_multiplier": qty, "entry_branch": entry_branch,
                }
                consumed_side = 1 if entry_signal == "BULLISH" else -1

    if position is not None:
        trades.append({
            "symbol": symbol, "label": position["option_type"], "entry_dt": str(position["entry_dt"]),
            "entry_price": position["entry_price"],
            "status": "still open through end of available data", "pnl": None,
        })
    return trades, skipped_entries


def print_report(name, trades, skipped_entries):
    if skipped_entries:
        print(f"\n[{name}] SKIPPED entry signals: {len(skipped_entries)}")
        for s in skipped_entries:
            print(f"    {s[0]}: {s[1]}")

    print(f"\n[{name}] {len(trades)} trades")
    print(f"\n=== [{name}] TRADE-WISE P&L ===")
    total = 0.0
    for i, tr in enumerate(trades, 1):
        pnl = tr.get("pnl")
        if pnl is not None:
            total += pnl
        print(f"{i}. {tr.get('symbol','?')} {tr['label']} ({tr.get('trading_symbol', 'N/A')}) "
              f"branch={tr.get('entry_branch', 'N/A')} "
              f"entry={tr['entry_dt']} entry_price={tr['entry_price']:.2f} exit={tr.get('exit_dt', 'N/A')} "
              f"exit_price={tr.get('exit_price', 'N/A')} status={tr['status']} "
              f"exit_reason={tr.get('exit_reason')} pnl={pnl if pnl is not None else 'N/A'}")

    print(f"\n=== [{name}] DAY-WISE P&L ===")
    by_date: dict = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for tr in trades:
        pnl = tr.get("pnl")
        if pnl is None:
            continue
        d = tr["entry_dt"][:10]
        by_date[d]["pnl"] += pnl
        by_date[d]["trades"] += 1
        if pnl > 0:
            by_date[d]["wins"] += 1
        elif pnl < 0:
            by_date[d]["losses"] += 1
    running = 0.0
    for d in sorted(by_date):
        s = by_date[d]
        running += s["pnl"]
        print(f"{d}: trades={s['trades']:2d} wins={s['wins']:2d} losses={s['losses']:2d} "
              f"net_pnl={s['pnl']:+.0f} running_total={running:+.0f}")

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    print(f"\n=== [{name}] SUMMARY ===")
    print(f"Total trades: {len(trades)} (closed={len(closed)}, still-open={len(trades) - len(closed)})")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {(len(wins) / len(closed) * 100) if closed else 0:.1f}%")
    print(f"TOTAL net P&L: Rs {total:+.0f}")
    branch_b_trades = sum(1 for t in trades if t.get("entry_branch") == "branch_b_day_range")
    print(f"Of these, {branch_b_trades} were entered via the Day Range branch B specifically "
          f"(the rest via the ordinary regime+Supertrend branch A).")
    return {"trades": len(trades), "closed": len(closed), "wins": len(wins), "losses": len(losses), "total_pnl": total}


def run_symbol(symbol: str):
    target_pct = current_target_pct(symbol)
    cache_dir = CACHE_ROOT / symbol.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'=' * 20} {symbol} (TARGET_PCT={target_pct}) {'=' * 20}")

    fast = fetch_equity_cached(symbol, cache_dir, REGIME_FAST_INTERVAL_MINUTES, SIGNAL_LOOKBACK_DAYS)
    slow = fetch_equity_cached(symbol, cache_dir, REGIME_SLOW_INTERVAL_MINUTES, SIGNAL_LOOKBACK_DAYS)
    master_equity = fetch_equity_cached(symbol, cache_dir, 1, SIGNAL_LOOKBACK_DAYS)
    print(f"  fast(5min): {len(fast['closes'])} bars, slow(15min): {len(slow['closes'])} bars, "
          f"master(1min): {len(master_equity['closes'])} bars")

    fast_ts, slow_ts = fast["timestamps"], slow["timestamps"]
    st5, is_above5, crossed_above5, crossed_below5 = compute_supertrend_cross_series(
        fast["highs"], fast["lows"], fast["closes"])
    st15, is_above15, _, _ = compute_supertrend_cross_series(slow["highs"], slow["lows"], slow["closes"])
    vol_ratio5 = [volume_ratio_at(fast["volumes"], i) for i in range(len(fast["closes"]))]

    fast_ema = _compute_ema(fast["closes"], REGIME_EMA_PERIOD)
    slow_ema = _compute_ema(slow["closes"], REGIME_EMA_PERIOD)
    rsi5 = _compute_rsi(fast["closes"], DAY_RANGE_RSI_PERIOD)

    n = len(fast["closes"])
    is_above15_aligned = [None] * n
    is_bullish = [None] * n
    gap_widened = [None] * n

    for i in range(n):
        cutoff = fast_ts[i] + REGIME_FAST_INTERVAL_MINUTES * 60
        s_idx = last_closed_idx(slow_ts, cutoff, REGIME_SLOW_INTERVAL_MINUTES)
        if s_idx is not None:
            is_above15_aligned[i] = is_above15[s_idx]
        current_gap = aligned_gap(fast_ema, fast_ts, i, slow_ema, slow_ts)
        if current_gap is not None:
            is_bullish[i] = current_gap > 0

    for i in range(n):
        current_gap = aligned_gap(fast_ema, fast_ts, i, slow_ema, slow_ts)
        gap_n_ago = aligned_gap(fast_ema, fast_ts, i - REGIME_GAP_WIDENING_LOOKBACK_CANDLES, slow_ema, slow_ts)
        if current_gap is None or gap_n_ago is None:
            gap_widened[i] = None
        elif is_bullish[i]:
            gap_widened[i] = current_gap > gap_n_ago
        else:
            gap_widened[i] = current_gap < gap_n_ago

    trend_aware_bullish = [bool(is_bullish[i] is True and gap_widened[i] is True) for i in range(n)]
    trend_aware_bearish = [bool(is_bullish[i] is False and gap_widened[i] is True) for i in range(n)]

    filter_bullish = [False] * n
    filter_bearish = [False] * n
    for i in range(n):
        st15_above = is_above15_aligned[i]
        regime_bullish_leg = is_bullish[i] is True
        regime_bearish_leg = is_bullish[i] is False
        filter_bullish[i] = bool(st15_above) or trend_aware_bullish[i] or regime_bullish_leg
        filter_bearish[i] = (st15_above is False) or trend_aware_bearish[i] or regime_bearish_leg

    day_start_idx: dict = {}
    for i, ts in enumerate(fast_ts):
        d = datetime.fromtimestamp(ts, tz=IST).date()
        if d not in day_start_idx:
            day_start_idx[d] = i
    bullish_day_range_entry = [False] * n
    bearish_day_range_entry = [False] * n
    for i in range(n):
        d = datetime.fromtimestamp(fast_ts[i], tz=IST).date()
        first_idx = day_start_idx[d]
        if first_idx == 0 or i < 1:
            continue
        today_open = fast["opens"][first_idx]
        yesterday_close = fast["closes"][first_idx - 1]
        gap_up_day = today_open > yesterday_close
        gap_down_day = today_open < yesterday_close
        prev_rsi, rsi = rsi5[i - 1], rsi5[i]
        crossed_above_bull = bool(prev_rsi is not None and rsi is not None
                                   and prev_rsi <= DAY_RANGE_RSI_BULL_LEVEL < rsi)
        crossed_below_bear = bool(prev_rsi is not None and rsi is not None
                                   and prev_rsi >= DAY_RANGE_RSI_BEAR_LEVEL > rsi)
        is_above_st = is_above5[i]
        if is_above_st is None:
            continue
        bullish_day_range_entry[i] = bool(gap_up_day and fast["closes"][i] > today_open
                                           and is_above_st and crossed_above_bull)
        bearish_day_range_entry[i] = bool(gap_down_day and fast["closes"][i] < today_open
                                           and (not is_above_st) and crossed_below_bear)

    branch_b_bullish = [bullish_day_range_entry[i] and trend_aware_bullish[i] for i in range(n)]
    branch_b_bearish = [bearish_day_range_entry[i] and trend_aware_bearish[i] for i in range(n)]

    day_range_signal_count = sum(branch_b_bullish) + sum(branch_b_bearish)
    print(f"[backtest] {symbol}: Day Range branch B would independently fire on "
          f"{day_range_signal_count} five-min candles across the full {SIGNAL_LOOKBACK_DAYS}-day lookback")

    front_df, front_expiry = resolve_option_universe(symbol)
    tradable_from = previous_monthly_expiry_cutoff(front_expiry) + timedelta(days=1)
    print(f"[backtest] {symbol}: front-month option expiry {front_expiry} - TRADABLE_FROM_DATE={tradable_from}")

    all_days = sorted({datetime.fromtimestamp(t, tz=IST).date() for t in fast_ts})
    test_days = set(all_days[-TEST_DAYS_BACK:])
    print(f"[backtest] {symbol}: testing entries over the last {TEST_DAYS_BACK} trading days: "
          f"{sorted(test_days)[0]} to {sorted(test_days)[-1]}")

    option_cache: dict = {}
    trades, skipped = run_backtest(
        symbol, target_pct, filter_bullish, filter_bearish, branch_b_bullish, branch_b_bearish, is_bullish,
        fast_ts, crossed_above5, crossed_below5, vol_ratio5, master_equity, test_days,
        front_df, tradable_from, option_cache, cache_dir)
    return trades, skipped


def main():
    _authenticate_avoiding_session_collision()
    for _s in SYMBOLS:
        assert not dhan_wrapper.is_mcx_commodity(_s), f"{_s}: needs mcx handling"
    all_trades = []
    per_symbol_summaries = {}
    for symbol in SYMBOLS:
        trades, skipped = run_symbol(symbol)
        target_pct = current_target_pct(symbol)
        summary = print_report(f"{symbol} live v2/'v3' formula, TARGET_PCT={target_pct}", trades, skipped)
        per_symbol_summaries[symbol] = summary
        all_trades.extend(trades)

    (CACHE_ROOT / "results_combined.json").write_text(json.dumps(all_trades, default=str, indent=2))

    if len(SYMBOLS) > 1:
        combined = print_report(f"{'+'.join(SYMBOLS)} COMBINED", all_trades, [])
        print(f"\n{'=' * 25} PER-SYMBOL SUMMARY {'=' * 25}")
        for symbol, s in per_symbol_summaries.items():
            wr = (s['wins'] / s['closed'] * 100) if s['closed'] else 0
            print(f"{symbol:<12} trades={s['trades']:3d} wins={s['wins']:3d} losses={s['losses']:3d} "
                  f"win_rate={wr:5.1f}%  net_pnl=Rs {s['total_pnl']:+.0f}")


if __name__ == "__main__":
    main()
