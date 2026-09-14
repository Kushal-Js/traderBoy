"""
User request 14 Sep 2026: backtest the SAME new EMA20/EMA100/EMA200
crossover rule (see backtest_swing_adaniports_ema_20day.py) on COPPER
(MCX) options over the last 20 trading days. Copper is NOT on the live
watchlist yet - this is a standalone backtest, same staged-rollout
discipline as every other Copper feature this session.

Real differences from the ADANIPORTS version, all confirmed against live
code/data before writing this (not assumed):

  - UNDERLYING REFERENCE for the EMA signals is Copper's own MCX FUTURES
    contract (COPPER SEP FUT, expiry 30-Sep-2026), not an equity - there
    is no continuous "spot" for an MCX commodity (Swing/signals.py's own
    _underlying_reference does exactly this for any MCX_SYMBOLS member).
    Verified this contract's own price history spans 16-Jun to 11-Sep
    2026 - comfortably covers the 20-day test window with no rollover
    gap, so a single contract's series is used throughout (matching how
    the live code itself resolves this once and caches it, not a
    synthetic multi-contract continuous series).
  - TRADED INSTRUMENT is ALWAYS an OPTION (CE or PE) regardless of the
    entry signal's direction - Copper is forced into OPTIONS via
    Swing/config.py's MCX_OPTIONS_ONLY_SYMBOLS, independent of whatever
    the global BASKET_TYPE is set to (this was this session's own
    "Copper always trades options" design, finalized 12-13 Sep 2026).
    BULLISH -> buy ATM CE; BEARISH -> buy ATM PE (resolved_option_type_for
    semantics, ported directly).
  - ATM strike is resolved DYNAMICALLY per entry (nearest strike to the
    Copper futures price at that exact moment, nearest expiry) against
    the MCX OPTFUT instrument rows (SEM_EXM_EXCH_ID=="MCX",
    SEM_INSTRUMENT_NAME=="OPTFUT", SM_SYMBOL_NAME=="COPPER") - verified
    only ONE expiry cycle (23-Sep-2026) was active/listed across the
    entire test window, so no rollover handling was needed for this
    particular 20-day pass; a longer backtest spanning a real MCX options
    rollover would need that added.
  - QUANTITY vs PNL_MULTIPLIER split (the single most important
    correctness point from this session's original Copper rollout):
    quantity = lot_size(from the OPTFUT row, real order-placement
    quantity) * QUANTITY_LOTS; pnl_multiplier = Swing/config.py's
    MCX_PNL_MULTIPLIERS["COPPER"] (2,500 kg, the REAL economic exposure
    per lot, verified via a live margin-calculator spike during the
    original rollout - NOT the same as quantity) * QUANTITY_LOTS. All
    rupee-denominated exit checks (MAX_LOSS_HIT, TARGET_HIT,
    PROFIT_PROTECTION_HIT, STOP_LOSS_HIT) use pnl_multiplier, never
    quantity - conflating these two was the near-miss this session
    caught before ever shipping Copper support live.
  - EXIT DIRECTION for the new EMA_CROSS_SQUAREOFF is keyed off the
    OPTION TYPE (CE/PE), NOT "instrument_side" - a real bug was found in
    the LIVE Swing/trading_engine.py._evaluate_exit_signal while building
    this script: resolve_instrument_side() returns "LONG" for BOTH an
    OPTIONS+BULLISH (CE) and OPTIONS+BEARISH (PE) entry (a bought PE is
    still technically a long position), but the live Supertrend-reversal
    check only ever looks at instrument_side ("LONG" -> crossed_below),
    never at the real CE/PE type - so for a live PE position, the code
    currently watches for the underlying turning MORE bearish (which
    CONFIRMS the PE thesis) rather than turning bullish (the real
    reversal-against-a-PE signal), and would essentially never catch a
    real reversal via that path. This script does NOT replicate that bug
    - it correctly keys the EMA_CROSS_SQUAREOFF direction off option_type
    (CE exits on EMA20-crosses-below-EMA100; PE exits on EMA20-crosses-
    above-EMA100), same as Options/Futures/Luxury's own _supertrend_
    signal_for already does correctly for CE/PE. The live bug is
    unaffected by this script and would still need a real fix before
    Copper (or any OPTIONS-basket symbol) trades a PE live.

Entry/exit rule (otherwise identical to the ADANIPORTS EMA backtest):
  ema20(5min, Copper futures) crosses ABOVE ema200(15min, Copper futures)
    -> "BULLISH" -> buy ATM CE
  ema20 crosses BELOW ema200 -> "BEARISH" -> buy ATM PE
  Exit ladder: MAX_LOSS_HIT -> TARGET_HIT -> PROFIT_PROTECTION_HIT ->
    STOP_LOSS_HIT (all Swing/config.py values, unchanged) ->
    EMA_CROSS_SQUAREOFF (CE: ema20 crosses below ema100; PE: ema20
    crosses above ema100, both 5-min Copper futures).

NOT modeled (same standing disclosure as every backtest this session):
  SL-L broker-side stop-loss, cross_strategy_registry, funds check,
  MCX options rollover (not exercised by this 20-day window - see above).

This is a BACKTEST ONLY - no live code touched, and Copper is NOT being
added to the live watchlist by this script. Deploying this rule (for
ADANIPORTS, Copper, or both) is a separate, explicit step gated on the
user reviewing these results.

Run via SSH on the droplet (never locally while the live bot's own
session is active):
    uv run python backtest_swing_copper_ema_20day.py [DAYS_BACK_TRADING]
    (optional - default 20)
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _compute_ema, _retry  # noqa: E402
from Swing import config as swing_config  # noqa: E402
from Swing.position_store import (  # noqa: E402
    unrealized_pnl_rs, target_price_for, hard_stop_for,
    price_past_target, price_past_hard_stop, giveback_floor, price_past_giveback_floor,
)

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "COPPER"

TEST_DAYS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 20
SIGNAL_FETCH_DAYS_BACK = 90
OPTION_FETCH_DAYS_BACK = 90

CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/swing_backtest_cache_copper_ema")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

QUANTITY_LOTS = swing_config.QUANTITY_LOTS
MAX_LOSS_PROTECTION_RS = swing_config.MAX_LOSS_PROTECTION_RS
PROFIT_PROTECTION_RS = swing_config.PROFIT_PROTECTION_RS
PROFIT_PROTECTION_GIVEBACK_PCT = swing_config.PROFIT_PROTECTION_GIVEBACK_PCT
TARGET_PCT = swing_config.TARGET_PCT
HARD_STOP_LOSS_PCT = swing_config.HARD_STOP_LOSS_PCT
ENABLE_TARGET_EXIT = swing_config.ENABLE_TARGET_EXIT
REGIME_FAST_INTERVAL_MINUTES = swing_config.REGIME_FAST_INTERVAL_MINUTES  # 5
REGIME_SLOW_INTERVAL_MINUTES = swing_config.REGIME_SLOW_INTERVAL_MINUTES  # 15
SLOW_EMA_PERIOD = swing_config.REGIME_EMA_PERIOD  # 200
FAST_EMA_PERIOD = 20
MID_EMA_PERIOD = 100
MCX_PNL_MULTIPLIER = swing_config.MCX_PNL_MULTIPLIERS[SYMBOL]

print(f"[backtest] SYMBOL={SYMBOL} TEST_DAYS_BACK={TEST_DAYS_BACK} "
      f"MAX_LOSS_PROTECTION_RS={MAX_LOSS_PROTECTION_RS} PROFIT_PROTECTION_RS={PROFIT_PROTECTION_RS} "
      f"GIVEBACK_PCT={PROFIT_PROTECTION_GIVEBACK_PCT} TARGET_PCT={TARGET_PCT} "
      f"HARD_STOP_LOSS_PCT={HARD_STOP_LOSS_PCT} ENABLE_TARGET_EXIT={ENABLE_TARGET_EXIT} "
      f"MCX_PNL_MULTIPLIER={MCX_PNL_MULTIPLIER} "
      f"ENTRY=EMA{FAST_EMA_PERIOD}({REGIME_FAST_INTERVAL_MINUTES}min) x EMA{SLOW_EMA_PERIOD}"
      f"({REGIME_SLOW_INTERVAL_MINUTES}min) crossover [on Copper FUTURES] "
      f"EXIT(new)=EMA{FAST_EMA_PERIOD} x EMA{MID_EMA_PERIOD} (both {REGIME_FAST_INTERVAL_MINUTES}min)")


def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def fetch_futures_signal_cached(interval_minutes: int, security_id: str, days_back: int = SIGNAL_FETCH_DAYS_BACK) -> dict:
    cache_file = CACHE_DIR / f"FUT_{security_id}_{interval_minutes}min_{days_back}d.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                  security_id=security_id, exchange_segment="MCX_COMM", instrument_type="FUTCOM",
                  from_date=from_date, to_date=to_date, interval=interval_minutes)
    data = resp.get("data") or {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def nearest_mcx_option_for_strike_ref(option_type: str, ref_price: float) -> dict:
    df = dhan_wrapper.instruments()
    opts = df[(df["SEM_EXM_EXCH_ID"] == "MCX") & (df["SEM_INSTRUMENT_NAME"] == "OPTFUT")
              & (df["SM_SYMBOL_NAME"] == SYMBOL) & (df["SEM_OPTION_TYPE"] == option_type)]
    if opts.empty:
        raise ValueError(f"No {option_type} contracts found for {SYMBOL}")
    nearest_expiry = opts["SEM_EXPIRY_DATE"].min()
    matches = opts[opts["SEM_EXPIRY_DATE"] == nearest_expiry].copy()
    matches["dist"] = (matches["SEM_STRIKE_PRICE"] - ref_price).abs()
    row = matches.sort_values("dist").iloc[0]
    return {
        "security_id": str(int(row["SEM_SMST_SECURITY_ID"])), "trading_symbol": str(row["SEM_CUSTOM_SYMBOL"]),
        "strike": float(row["SEM_STRIKE_PRICE"]), "lot_size": int(float(row["SEM_LOT_UNITS"])),
    }


def fetch_mcx_option_1min_cached(security_id: str) -> dict:
    cache_file = CACHE_DIR / f"OPT_{security_id}_1min.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=OPTION_FETCH_DAYS_BACK)).strftime("%Y-%m-%d")
    try:
        resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                      security_id=security_id, exchange_segment="MCX_COMM", instrument_type="OPTFUT",
                      from_date=from_date, to_date=to_date, interval=1)
        data = resp.get("data") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"    OPTION FETCH FAILED for {security_id}: {exc}")
        data = {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def compute_cross_series(fast_values, ref_values):
    n = len(fast_values)
    crossed_above = [False] * n
    crossed_below = [False] * n
    is_above = [None] * n
    for i in range(n):
        if fast_values[i] is not None and ref_values[i] is not None:
            is_above[i] = fast_values[i] > ref_values[i]
    for i in range(1, n):
        if is_above[i] is None or is_above[i - 1] is None:
            continue
        if (not is_above[i - 1]) and is_above[i]:
            crossed_above[i] = True
        if is_above[i - 1] and not is_above[i]:
            crossed_below[i] = True
    return crossed_above, crossed_below


def main():
    fut_contract = dhan_wrapper.get_mcx_futures_contract(SYMBOL)
    print(f"[backtest] Copper futures signal reference: {fut_contract.trading_symbol} "
          f"(security_id={fut_contract.security_id}, expiry={fut_contract.expiry_date})")

    fast = fetch_futures_signal_cached(REGIME_FAST_INTERVAL_MINUTES, fut_contract.security_id)
    slow = fetch_futures_signal_cached(REGIME_SLOW_INTERVAL_MINUTES, fut_contract.security_id)
    # 1-min series of the SAME futures contract used as the master simulation
    # clock (exactly like backtest_swing_adaniports_ema_20day.py's fut_data) -
    # this is what fixes the overlapping-position bug: exits are checked every
    # real minute, in strict chronological order, with only one position open
    # at a time, instead of a 5-min-signal-driven loop that let each entry
    # "fast-forward" to its own resolution on a disconnected time axis.
    master = fetch_futures_signal_cached(1, fut_contract.security_id)
    print(f"  fast({REGIME_FAST_INTERVAL_MINUTES}min): {len(fast['closes'])} bars, "
          f"slow({REGIME_SLOW_INTERVAL_MINUTES}min): {len(slow['closes'])} bars, "
          f"master(1min): {len(master['closes'])} bars")

    ema20 = _compute_ema(fast["closes"], FAST_EMA_PERIOD)
    ema100 = _compute_ema(fast["closes"], MID_EMA_PERIOD)
    ema200_slow = _compute_ema(slow["closes"], SLOW_EMA_PERIOD)

    ema200_on_fast_grid = [None] * len(fast["timestamps"])
    for i, ts in enumerate(fast["timestamps"]):
        s_idx = idx_at_or_before(slow["timestamps"], ts)
        if s_idx is not None and ema200_slow[s_idx] is not None:
            ema200_on_fast_grid[i] = ema200_slow[s_idx]

    entry_crossed_above, entry_crossed_below = compute_cross_series(ema20, ema200_on_fast_grid)
    exit_crossed_above, exit_crossed_below = compute_cross_series(ema20, ema100)

    all_days = sorted({datetime.fromtimestamp(t, tz=IST).date() for t in fast["timestamps"]})
    test_days = set(all_days[-TEST_DAYS_BACK:])
    print(f"[backtest] Testing entries over the last {TEST_DAYS_BACK} trading days: "
          f"{sorted(test_days)[0]} to {sorted(test_days)[-1]}")

    signal_interval_seconds = REGIME_FAST_INTERVAL_MINUTES * 60
    trades = []
    position = None
    consumed_signal_idx = None
    skipped_entries = []
    option_cache: dict = {}  # security_id -> fetched 1-min series, reused if the same ATM contract recurs

    def price_at_or_before(ts_list, closes, target_ts):
        idx = idx_at_or_before(ts_list, target_ts)
        return closes[idx] if idx is not None else None

    # Single master 1-min timeline (Copper futures' own series), exactly
    # matching backtest_swing_adaniports_ema_20day.py's architecture: exit
    # conditions are checked every real minute in strict chronological
    # order with only one position open at a time, and a new entry is only
    # even considered when position is None at that same real-time step -
    # this is what prevents the overlapping-position bug found in the first
    # version of this script (which drove off the 5-min signal grid and let
    # each entry resolve instantly on the option's own disconnected time axis).
    master_ts = [t for t in master["timestamps"] if datetime.fromtimestamp(t, tz=IST).date() in test_days]

    for t in master_ts:
        dt = datetime.fromtimestamp(t, tz=IST)
        fut_price = price_at_or_before(master["timestamps"], master["closes"], t)
        if fut_price is None:
            continue
        sig_idx = idx_at_or_before(fast["timestamps"], t - signal_interval_seconds)

        if position is not None:
            opt_ts_list, opt_closes = position["opt_data"]["timestamps"], position["opt_data"]["closes"]
            premium = price_at_or_before(opt_ts_list, opt_closes, t)
            if premium is not None:
                position["best_price"] = max(position["best_price"], premium)
                loss_rs = -unrealized_pnl_rs("LONG", position["entry_price"], premium, MCX_PNL_MULTIPLIER)
                reason = None
                if loss_rs >= MAX_LOSS_PROTECTION_RS:
                    reason = "MAX_LOSS_HIT"
                elif ENABLE_TARGET_EXIT and price_past_target("LONG", premium, position["target_price"]):
                    reason = "TARGET_HIT"
                else:
                    peak_profit_rs = unrealized_pnl_rs("LONG", position["entry_price"],
                                                        position["best_price"], MCX_PNL_MULTIPLIER)
                    if peak_profit_rs > PROFIT_PROTECTION_RS:
                        floor = giveback_floor("LONG", position["best_price"], PROFIT_PROTECTION_GIVEBACK_PCT)
                        if price_past_giveback_floor("LONG", premium, floor):
                            reason = "PROFIT_PROTECTION_HIT"
                    if reason is None and price_past_hard_stop("LONG", premium, position["hard_stop_loss"]):
                        reason = "STOP_LOSS_HIT"
                    if (reason is None and sig_idx is not None
                            and fast["timestamps"][sig_idx] > position["entry_candle_ts"]):
                        option_type = position["option_type"]
                        if option_type == "CE" and exit_crossed_below[sig_idx]:
                            reason = "EMA_CROSS_SQUAREOFF"
                        elif option_type == "PE" and exit_crossed_above[sig_idx]:
                            reason = "EMA_CROSS_SQUAREOFF"
                if reason:
                    pnl = unrealized_pnl_rs("LONG", position["entry_price"], premium, MCX_PNL_MULTIPLIER)
                    print(f"  EXIT  {position['option_type']} @ {dt} reason={reason} exit_price={premium} pnl={pnl:+.0f}")
                    trades.append({
                        "option_type": position["option_type"], "entry_dt": str(position["entry_dt"]),
                        "entry_price": position["entry_price"], "exit_dt": str(dt), "exit_price": premium,
                        "exit_reason": reason, "pnl_multiplier": MCX_PNL_MULTIPLIER, "pnl": pnl,
                        "status": "closed",
                    })
                    position = None
            # else: this option's own data has no bar at-or-before t (e.g. contract
            # not trading yet at that exact minute) - carry the position forward
            # unresolved on this tick rather than force-closing it.

        if position is None and sig_idx is not None and sig_idx != consumed_signal_idx:
            entry_signal = None
            if entry_crossed_above[sig_idx]:
                entry_signal = "BULLISH"
            elif entry_crossed_below[sig_idx]:
                entry_signal = "BEARISH"
            if entry_signal:
                consumed_signal_idx = sig_idx
                option_type = "CE" if entry_signal == "BULLISH" else "PE"
                try:
                    opt = nearest_mcx_option_for_strike_ref(option_type, fut_price)
                    if opt["security_id"] not in option_cache:
                        option_cache[opt["security_id"]] = fetch_mcx_option_1min_cached(opt["security_id"])
                    opt_data = option_cache[opt["security_id"]]
                except Exception as exc:  # noqa: BLE001
                    print(f"  {dt}: SKIPPED entry signal={entry_signal} - could not resolve/fetch ATM {option_type} ({exc})")
                    skipped_entries.append(str(dt))
                    continue
                entry_price = price_at_or_before(opt_data["timestamps"], opt_data["closes"], t)
                if entry_price is None:
                    print(f"  {dt}: SKIPPED entry signal={entry_signal} - no option price data at entry time")
                    skipped_entries.append(str(dt))
                    continue
                target_price = target_price_for("LONG", entry_price, TARGET_PCT)
                hard_stop_loss = hard_stop_for("LONG", entry_price, HARD_STOP_LOSS_PCT)
                print(f"  ENTER {option_type} @ {dt} signal={entry_signal} "
                      f"contract={opt['trading_symbol']} entry_price={entry_price}")
                position = {
                    "option_type": option_type, "entry_dt": dt, "entry_price": entry_price,
                    "best_price": entry_price, "target_price": target_price, "hard_stop_loss": hard_stop_loss,
                    "entry_candle_ts": fast["timestamps"][sig_idx], "opt": opt, "opt_data": opt_data,
                }

    if position is not None:
        trades.append({
            "option_type": position["option_type"], "entry_dt": str(position["entry_dt"]),
            "entry_price": position["entry_price"],
            "status": "still open through end of available data", "pnl": None,
        })

    (CACHE_DIR / "results_copper_swing_ema_20day.json").write_text(json.dumps(trades, default=str, indent=2))

    print(f"\n[backtest] {len(trades)} trades")
    print("\n=== TRADE-WISE P&L ===")
    total = 0.0
    for i, tr in enumerate(trades, 1):
        pnl = tr.get("pnl")
        if pnl is not None:
            total += pnl
        print(f"{i}. {tr['option_type']} entry={tr['entry_dt']} exit={tr.get('exit_dt', 'N/A')} "
              f"status={tr['status']} exit_reason={tr.get('exit_reason')} "
              f"pnl={pnl if pnl is not None else 'N/A'}")

    print("\n=== DATE-WISE P&L ===")
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
    for d in sorted(by_date):
        s = by_date[d]
        print(f"{d}: trades={s['trades']:2d} wins={s['wins']:2d} losses={s['losses']:2d} net_pnl={s['pnl']:+.0f}")

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    print(f"\n=== SUMMARY ===")
    print(f"Total trades: {len(trades)} (closed={len(closed)}, still-open={len(trades) - len(closed)})")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {(len(wins) / len(closed) * 100) if closed else 0:.1f}%")
    print(f"TOTAL net P&L: Rs {total:+.0f}")
    if skipped_entries:
        print(f"\nSKIPPED entry signals (no usable option data): {skipped_entries}")


if __name__ == "__main__":
    main()
