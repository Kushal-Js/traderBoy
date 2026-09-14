"""
User request 14 Sep 2026: backtest the OPTIONS strategy's PE (bearish/put-
buying) side against a second real Chartink "Sell" scan alert file ("02
Sell Range Breakout.csv", 26 Aug - 11 Sep 2026, 13 trading days, ~590
unique symbols) - same architecture as backtest_options_chartink_sell_
range_breakout_01.py (the "01" file), reused verbatim below. Same
clarification pattern as that file: the user asked for "CE" on this
"Sell" scan, and confirmed via AskUserQuestion that PE (matching the
scan's own bearish direction) is what's actually wanted - a "sell" scan
alert routes through Options' /chartink/webhook-sell in production,
which buys PE (not CE) - confirmed via that endpoint's own code
(option_type="PE", prefer_highest=False).

Reuses the exact methodology built for backtest_options_chartink_range_
breakout_04.py, with the PE-specific differences called out below - this
is NOT a find-and-replace of "CE" to "PE"; several things genuinely work
differently for a put:

  - Ranking direction: rank_and_pick_top_stocks sorts by day-change% with
    reverse=prefer_highest. CE uses prefer_highest=True (biggest gainers
    first); PE uses prefer_highest=False (biggest DECLINERS first) - same
    "strongest signal in the batch" idea, pointed the other way for a
    bearish scan. SELECT_BOTTOM_N_STOCKS=True then takes the LAST N after
    that sort either way, which works out to "weakest decliners of the
    batch" for PE (a contrarian/laggard bet, mirroring CE's "weakest
    gainers") - see Options/trading_engine.py's rank_and_pick_top_stocks
    docstring for the exact reasoning this mirrors.
  - ATM resolution: nearest PE contract (SEM_OPTION_TYPE=="PE"), not CE.
  - SUPERTREND_EXIT direction: a CE (long call) profits when the
    underlying rises, so a bearish crossover is the reversal-against-it
    signal. A PE (long put) profits when the underlying FALLS, so the
    reversal-against-it signal is the opposite - a BULLISH crossover, not
    bearish (see Options/trading_engine.py's _supertrend_signal_for
    docstring - using the CE check for both would exit PE positions
    exactly backwards, on the move that CONFIRMS the PE thesis rather
    than the one that invalidates it).
  - Dynamic SL step uses DYNAMIC_SL_STEP_PCT_PE, not _CE (a real, separate
    config value, currently the same 0.07 default but independently
    tunable).
  - MAX_LIVE_POSITIONS_PE capacity, not _CE (also independently tunable,
    currently both default 2).
  - The Nifty gap-down/sharp-fall CE cool-off is DELIBERATELY NOT applied
    here - it only ever gates CE entries (see Options/dhan_client.py's
    should_delay_ce_entry docstring: "PE entries must never call this - a
    falling Nifty is exactly when a PE-buying alert should be allowed to
    act"). Applying it to PE would be a real bug, not a missing feature.
  - Target/stop/PP/MAX_LOSS math itself is UNCHANGED between CE and PE -
    both are long option premium, so profit is simply
    (exit_premium - entry_premium) * qty regardless of which side; only
    the entry-selection and exit-signal DIRECTION differ, not the P&L
    formula or the rupee thresholds.

Modeled (same as every prior pass this session, adjusted for PE per
above): batch ranking, MAX_LIVE_POSITIONS_PE capacity, MAX_DAILY_ENTRIES_
PER_SYMBOL cap, same-day RSI-gated loss re-entry block, LOSS_REPEAT_BLOCK,
ENABLE_TRADING_TIME_LIMIT/ALLOWED_TRADING_TIME entry cutoff, full exit
ladder (MAX_LOSS_HIT -> TARGET_HIT -> PROFIT_PROTECTION_HIT -> TRAILING_
SL_HIT/STOP_LOSS_HIT -> SUPERTREND_EXIT). All read live from
Options/config.py at import time.

NOT modeled (disclosed, not silently dropped - same standing limitations
as every prior pass this session):
  - SL-L broker-side stop-loss (real effect can only tighten a loss,
    never worsen it).
  - cross_strategy_registry (Options/Futures/Luxury mutual symbol lock).

Run via SSH on the droplet (never locally while the live bot's own
session is active):
    uv run python backtest_options_chartink_sell_range_breakout_01.py [PP_BEFORE] [PP_AFTER] [GIVEBACK_PCT]
    (all three optional - default to whatever's live in Options/config.py)
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from Options.dhan_client import dhan_wrapper, _compute_supertrend, _compute_rsi, _retry  # noqa: E402
from Options import config as options_config  # noqa: E402
from exit_ladder_backtest_helper import evaluate_exit_reason, compute_ema_cross_series, ExitLadderConfig, build_full_minute_grid  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

CSV_PATH = Path("/root/apps/traderBoy/02 Sell Range Breakout.csv")
CACHE_DIR = Path("/private/tmp/claude-501/-Users-kushalgaur-Desktop-projects-trading-traderBoy/"
                  "60a0e686-2110-4e20-bad5-fe817a57a72a/scratchpad/options_chartink_backtest_cache_sell02")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DAYS_BACK = 90
ST_PERIOD = 10
ST_MULT = 3.0
ENTRY_TF = 5
OPTION_TF = 1
QUANTITY_LOTS = options_config.QUANTITY_LOTS
TOP_N_STOCKS = options_config.TOP_N_STOCKS
SELECT_BOTTOM_N_STOCKS = options_config.SELECT_BOTTOM_N_STOCKS
MAX_LIVE_POSITIONS_PE = options_config.MAX_LIVE_POSITIONS_PE
MAX_DAILY_ENTRIES_PER_SYMBOL = options_config.MAX_DAILY_ENTRIES_PER_SYMBOL
ENABLE_RSI_LOSS_REENTRY_BLOCK = options_config.ENABLE_RSI_LOSS_REENTRY_BLOCK
LOSS_REPEAT_BLOCK_ENABLED = options_config.LOSS_REPEAT_BLOCK_ENABLED
LOSS_REPEAT_BLOCK_COUNT = options_config.LOSS_REPEAT_BLOCK_COUNT
LOSS_REPEAT_BLOCK_EXIT_REASONS = options_config.LOSS_REPEAT_BLOCK_EXIT_REASONS
TARGET_PCT = options_config.TARGET_PCT
STOP_LOSS_PCT = options_config.STOP_LOSS_PCT
# PE-specific values (split from the shared CE/PE default 14 Sep 2026,
# user request: tighter caps for PE only - Rs 3500/1600 - see
# Options/config.py's own MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_PE/
# _AFTER_CUTOFF_PE docstring). This script is PE-only, so it must read
# the PE-specific fields now that they exist, not the shared/CE ones.
MAX_LOSS_BEFORE = options_config.MAX_LOSS_PER_TRADE_RS_BEFORE_CUTOFF_PE
MAX_LOSS_AFTER = options_config.MAX_LOSS_PER_TRADE_RS_AFTER_CUTOFF_PE
PP_BEFORE = float(sys.argv[1]) if len(sys.argv) > 1 else options_config.PROFIT_PROTECTION_THRESHOLD_RS_BEFORE_CUTOFF
PP_AFTER = float(sys.argv[2]) if len(sys.argv) > 2 else options_config.PROFIT_PROTECTION_THRESHOLD_RS_AFTER_CUTOFF
PP_GIVEBACK_PCT = float(sys.argv[3]) if len(sys.argv) > 3 else options_config.PROFIT_PROTECTION_GIVEBACK_PCT
CUTOFF_H, CUTOFF_M = (int(x) for x in options_config.RISK_THRESHOLD_CUTOFF_TIME.split(":"))
ENABLE_DYNAMIC_SL = options_config.ENABLE_DYNAMIC_SL
DYNAMIC_SL_STEP_PCT_PE = options_config.DYNAMIC_SL_STEP_PCT_PE
DYNAMIC_SL_INCREASE_PCT = options_config.DYNAMIC_SL_INCREASE_PCT
ENABLE_SUPERTREND_EXIT = options_config.ENABLE_SUPERTREND_EXIT
ENABLE_TRADING_TIME_LIMIT = options_config.ENABLE_TRADING_TIME_LIMIT
ALLOWED_TRADING_TIME = options_config.ALLOWED_TRADING_TIME
ALLOWED_H, ALLOWED_M = (int(x) for x in ALLOWED_TRADING_TIME.split(":"))
RSI_PERIOD = options_config.RSI_LOSS_REENTRY_PERIOD
RSI_OVERBOUGHT = options_config.RSI_LOSS_REENTRY_OVERBOUGHT

print(f"[backtest] MAX_LIVE_POSITIONS_PE={MAX_LIVE_POSITIONS_PE} TOP_N_STOCKS={TOP_N_STOCKS} "
      f"SELECT_BOTTOM_N_STOCKS={SELECT_BOTTOM_N_STOCKS} MAX_DAILY_ENTRIES_PER_SYMBOL={MAX_DAILY_ENTRIES_PER_SYMBOL} "
      f"TARGET_PCT={TARGET_PCT} STOP_LOSS_PCT={STOP_LOSS_PCT} MAX_LOSS={MAX_LOSS_BEFORE}/{MAX_LOSS_AFTER} "
      f"PROFIT_PROTECTION={PP_BEFORE}/{PP_AFTER} GIVEBACK_PCT={PP_GIVEBACK_PCT} cutoff={CUTOFF_H}:{CUTOFF_M:02d} "
      f"DYNAMIC_SL={ENABLE_DYNAMIC_SL}({DYNAMIC_SL_STEP_PCT_PE}/{DYNAMIC_SL_INCREASE_PCT}) "
      f"OPTION_TF={OPTION_TF}min LOSS_REPEAT_BLOCK={LOSS_REPEAT_BLOCK_ENABLED}(count={LOSS_REPEAT_BLOCK_COUNT}) "
      f"RSI_LOSS_REENTRY_BLOCK={ENABLE_RSI_LOSS_REENTRY_BLOCK}(period={RSI_PERIOD},overbought={RSI_OVERBOUGHT}) "
      f"ENTRY_CUTOFF={ENABLE_TRADING_TIME_LIMIT}({ALLOWED_TRADING_TIME}) "
      f"[PE backtest - Nifty gap-down CE delay N/A, never gates PE per should_delay_ce_entry's own contract]")


def parse_alerts() -> list[tuple[datetime, list[str]]]:
    rows = []
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            dt = datetime.strptime(row["Date"], "%d-%m-%Y %I:%M %p").replace(tzinfo=IST)
            rows.append((dt, row["Symbol"].strip().upper()))
    rows.sort(key=lambda r: r[0])
    grouped: dict[datetime, list[str]] = defaultdict(list)
    for dt, sym in rows:
        if sym not in grouped[dt]:
            grouped[dt].append(sym)
    return sorted(grouped.items(), key=lambda kv: kv[0])


def fetch_5min_cached(symbol: str) -> dict:
    cache_file = CACHE_DIR / f"{symbol}_5min.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    security_id = dhan_wrapper._equity_security_id(symbol)
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                  security_id=security_id, exchange_segment="NSE_EQ", instrument_type="EQUITY",
                  from_date=from_date, to_date=to_date, interval=ENTRY_TF)
    data = resp.get("data") or {}
    result = {"highs": data.get("high") or [], "lows": data.get("low") or [],
              "closes": data.get("close") or [], "timestamps": data.get("timestamp") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def nearest_pe_for_strike_ref(underlying: str, ref_price: float) -> dict:
    df = dhan_wrapper.instruments()
    opts = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_INSTRUMENT_NAME"] == "OPTSTK")
              & (df["SEM_OPTION_TYPE"] == "PE")]
    matches = opts[opts["SEM_TRADING_SYMBOL"].apply(
        lambda s: dhan_wrapper._underlying_from_trading_symbol(str(s)) == underlying
    )]
    if matches.empty:
        raise ValueError(f"No PE contracts found for {underlying}")
    nearest_expiry = matches["SEM_EXPIRY_DATE"].min()
    matches = matches[matches["SEM_EXPIRY_DATE"] == nearest_expiry].copy()
    matches["dist"] = (matches["SEM_STRIKE_PRICE"] - ref_price).abs()
    row = matches.sort_values("dist").iloc[0]
    return {
        "security_id": str(int(row["SEM_SMST_SECURITY_ID"])), "trading_symbol": str(row["SEM_CUSTOM_SYMBOL"]),
        "strike": float(row["SEM_STRIKE_PRICE"]), "lot_size": int(float(row["SEM_LOT_UNITS"])),
    }


def fetch_option_1min_cached(security_id: str) -> dict:
    """Caches closes/timestamps/volumes (volume added 14 Sep 2026 for the
    liquidity-guard exit). A cache file written before that lacks
    "volumes" - treated as a cache miss so it gets re-fetched."""
    cache_file = CACHE_DIR / f"OPT_{security_id}_{OPTION_TF}min.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if "volumes" in cached:
            return cached
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    try:
        resp = _retry(dhan_wrapper.client.Dhan.intraday_minute_data,
                      security_id=security_id, exchange_segment="NSE_FNO", instrument_type="OPTSTK",
                      from_date=from_date, to_date=to_date, interval=OPTION_TF)
        data = resp.get("data") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"    OPTION FETCH FAILED for {security_id}: {exc}")
        data = {}
    result = {"closes": data.get("close") or [], "timestamps": data.get("timestamp") or [],
              "volumes": data.get("volume") or []}
    cache_file.write_text(json.dumps(result))
    time.sleep(1.2)
    return result


def idx_at_or_after(ts_list, target_ts):
    for i, ts in enumerate(ts_list):
        if ts >= target_ts:
            return i
    return None


def idx_at_or_before(ts_list, target_ts):
    best = None
    for i, ts in enumerate(ts_list):
        if ts <= target_ts:
            best = i
        else:
            break
    return best


def price_at_or_before(ts_list, closes, target_ts):
    idx = idx_at_or_before(ts_list, target_ts)
    return closes[idx] if idx is not None else None


def current_max_loss_rs(dt: datetime) -> float:
    return MAX_LOSS_BEFORE if dt.time() < dtime(CUTOFF_H, CUTOFF_M) else MAX_LOSS_AFTER


def current_profit_protection_rs(dt: datetime) -> float:
    return PP_BEFORE if dt.time() < dtime(CUTOFF_H, CUTOFF_M) else PP_AFTER


def current_trailing_sl(entry_price: float, highest_price: float, hard_stop_loss: float) -> float:
    floor = hard_stop_loss
    if ENABLE_DYNAMIC_SL and entry_price:
        pct_up = (highest_price - entry_price) / entry_price
        steps = int(pct_up // DYNAMIC_SL_STEP_PCT_PE) if pct_up > 0 else 0
        if steps > 0:
            dynamic_sl_pct = STOP_LOSS_PCT - steps * DYNAMIC_SL_INCREASE_PCT
            floor = max(floor, entry_price * (1 - dynamic_sl_pct))
    return floor


def main():
    alerts = parse_alerts()
    all_symbols = sorted({sym for _, syms in alerts for sym in syms})
    print(f"[backtest] {len(alerts)} distinct alert events, {len(all_symbols)} unique symbols: {all_symbols}")

    ladder_cfg = ExitLadderConfig(pkg_config=options_config)
    print(f"[backtest] Exit ladder: TARGET_EXIT={ladder_cfg.enable_target_exit} "
          f"MAX_LOSS_BEFORE_CUTOFF={ladder_cfg.enable_max_loss_before_cutoff} "
          f"SUPERTREND_EXIT={ladder_cfg.enable_supertrend_exit} "
          f"EMA_CROSS_EXIT={ladder_cfg.enable_ema_cross_exit}({ladder_cfg.ema_cross_fast}/{ladder_cfg.ema_cross_slow}) "
          f"LIQUIDITY_GUARD={ladder_cfg.liquidity_guard_enabled}(bars={ladder_cfg.liquidity_guard_zero_volume_bars})")

    udata: dict[str, dict] = {}
    ust: dict[str, list] = {}
    ema_cross: dict[str, list] = {}
    rsi_series: dict[str, list] = {}
    day_last_close: dict[str, dict] = {}
    skipped_symbols: set[str] = set()
    for sym in all_symbols:
        try:
            d = fetch_5min_cached(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: SKIPPED - could not fetch underlying data ({exc})")
            skipped_symbols.add(sym)
            continue
        if not d["closes"]:
            print(f"  {sym}: SKIPPED - no underlying candle data returned")
            skipped_symbols.add(sym)
            continue
        s = _compute_supertrend(d["highs"], d["lows"], d["closes"], period=ST_PERIOD, multiplier=ST_MULT)
        r = _compute_rsi(d["closes"], RSI_PERIOD)
        e = compute_ema_cross_series(d["closes"], ladder_cfg.ema_cross_fast, ladder_cfg.ema_cross_slow)
        udata[sym] = d
        ust[sym] = s
        ema_cross[sym] = e
        rsi_series[sym] = r
        by_date: dict = {}
        for ts, c in zip(d["timestamps"], d["closes"]):
            by_date[datetime.fromtimestamp(ts, tz=IST).date()] = c
        day_last_close[sym] = by_date
        print(f"  {sym}: {len(d['closes'])} 5-min bars")

    def day_change_pct_at(sym, dt):
        price_now = price_at_or_before(udata[sym]["timestamps"], udata[sym]["closes"], dt.timestamp())
        if price_now is None:
            return None
        prior_dates = sorted(d for d in day_last_close[sym] if d < dt.date())
        if not prior_dates:
            return None
        prev_close = day_last_close[sym][prior_dates[-1]]
        if not prev_close:
            return None
        return (price_now - prev_close) / prev_close * 100.0

    def rsi_at(sym, dt):
        ts_list = udata[sym]["timestamps"]
        idx = idx_at_or_before(ts_list, dt.timestamp())
        if idx is None or idx < 1:
            return None, None
        return rsi_series[sym][idx], rsi_series[sym][idx - 1]

    # Full 1-minute grid (09:15-15:29) per trading day, NOT derived from
    # the 5-min underlying timestamps - Dhan's 5-min NSE_EQ candles stop at
    # the 15:10 bar, so extending them would blind the sim clock to the
    # last ~15 minutes of each day (the documented sim-clock trap - see
    # exit_ladder_backtest_helper.build_full_minute_grid's own docstring).
    master_ts = build_full_minute_grid(udata)

    alert_queue = list(alerts)
    live: dict[str, dict] = {}
    trades: list[dict] = []
    entries_today: dict[tuple, int] = defaultdict(int)
    max_loss_hits_today: dict[tuple, int] = defaultdict(int)
    loss_repeat_count_today: dict[tuple, int] = defaultdict(int)

    def try_enter_one(alert_dt, sym):
        if sym in live:
            return False
        key = (alert_dt.date(), sym)
        if entries_today[key] >= MAX_DAILY_ENTRIES_PER_SYMBOL:
            print(f"  SKIP  {sym} @ {alert_dt} reason=daily_reentry_cap_reached "
                  f"({entries_today[key]}/{MAX_DAILY_ENTRIES_PER_SYMBOL})")
            return False
        if ENABLE_RSI_LOSS_REENTRY_BLOCK and max_loss_hits_today[key] >= 1:
            rsi, prev_rsi = rsi_at(sym, alert_dt)
            if rsi is not None and prev_rsi is not None and (rsi > RSI_OVERBOUGHT or rsi < prev_rsi):
                reason = "overbought" if rsi > RSI_OVERBOUGHT else "falling"
                print(f"  SKIP  {sym} @ {alert_dt} reason=rsi_loss_reentry_block_active "
                      f"(RSI={rsi:.1f} {reason}, prev={prev_rsi:.1f})")
                return False
        if LOSS_REPEAT_BLOCK_ENABLED and loss_repeat_count_today[key] >= LOSS_REPEAT_BLOCK_COUNT:
            print(f"  SKIP  {sym} @ {alert_dt} reason=loss_repeat_block_active "
                  f"(already {loss_repeat_count_today[key]} loss-designated exit(s) today)")
            return False

        ts_list, closes = udata[sym]["timestamps"], udata[sym]["closes"]
        entry_idx = idx_at_or_after(ts_list, alert_dt.timestamp())
        if entry_idx is None:
            return False
        entry_underlying_price = closes[entry_idx]
        entry_dt = datetime.fromtimestamp(ts_list[entry_idx], tz=IST)
        try:
            pe = nearest_pe_for_strike_ref(sym, entry_underlying_price)
            pe_data = fetch_option_1min_cached(pe["security_id"])
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: SKIPPED entry @ {alert_dt} - could not resolve/fetch its ATM PE ({exc})")
            skipped_symbols.add(sym)
            return False
        entry_price = price_at_or_before(pe_data["timestamps"], pe_data["closes"], entry_dt.timestamp())
        if entry_price is None:
            return False
        hard_stop_loss = entry_price * (1 - STOP_LOSS_PCT)
        target_price = entry_price * (1 + TARGET_PCT)
        print(f"  ENTER {sym} @ {entry_dt} PE={pe['trading_symbol']} entry_price={entry_price}")
        live[sym] = {
            "entry_dt": entry_dt, "entry_price": entry_price, "highest_price": entry_price,
            "hard_stop_loss": hard_stop_loss, "target_price": target_price,
            "pe": pe, "pe_data": pe_data, "entry_ts": entry_dt.timestamp(),
            "entry_candle_ts": ts_list[entry_idx],
        }
        entries_today[key] += 1
        return True

    def try_enter_batch(alert_dt, syms):
        if ENABLE_TRADING_TIME_LIMIT and alert_dt.time() >= dtime(ALLOWED_H, ALLOWED_M):
            print(f"  IGNORE batch @ {alert_dt} {syms} reason=past_allowed_trading_time "
                  f"(cutoff={ALLOWED_TRADING_TIME})")
            return
        # No Nifty gap-down check here - PE is explicitly never gated by it (should_delay_ce_entry's own contract).
        remaining = MAX_LIVE_POSITIONS_PE - len(live)
        if remaining <= 0:
            print(f"  IGNORE batch @ {alert_dt} {syms} reason=max_live_positions_reached "
                  f"({len(live)}/{MAX_LIVE_POSITIONS_PE})")
            return

        candidates = [s for s in syms if s not in skipped_symbols]
        scored = []
        for s in candidates:
            pct = day_change_pct_at(s, alert_dt)
            if pct is None:
                print(f"  {s}: SKIPPED ranking @ {alert_dt} - could not compute day-change "
                      f"(no prior-day close in fetched window)")
                continue
            scored.append((s, pct))
        if not scored:
            print(f"  IGNORE batch @ {alert_dt} {syms} reason=could_not_rank_any_stock")
            return

        scored.sort(key=lambda t: t[1], reverse=False)  # prefer_highest=False for PE - biggest decliners first
        ranked = [s for s, _ in (scored[-TOP_N_STOCKS:] if SELECT_BOTTOM_N_STOCKS else scored[:TOP_N_STOCKS])]
        print(f"  RANKED @ {alert_dt}: batch={len(syms)} scored={len(scored)} -> picked(weakest-decliners)={ranked} "
              f"(day-change%: {dict(scored)})")

        for sym in ranked:
            if len(live) >= MAX_LIVE_POSITIONS_PE:
                break
            try_enter_one(alert_dt, sym)

    for t in master_ts:
        for sym in list(live.keys()):
            pos = live[sym]
            if t <= pos["entry_ts"]:
                continue
            premium = price_at_or_before(pos["pe_data"]["timestamps"], pos["pe_data"]["closes"], t)
            if premium is None:
                continue
            pos["highest_price"] = max(pos["highest_price"], premium)
            dt = datetime.fromtimestamp(t, tz=IST)
            qty = pos["pe"]["lot_size"] * QUANTITY_LOTS
            trailing_sl = current_trailing_sl(pos["entry_price"], pos["highest_price"], pos["hard_stop_loss"])
            reason = evaluate_exit_reason(
                option_type="PE", entry_price=pos["entry_price"], highest_price=pos["highest_price"],
                hard_stop_loss=pos["hard_stop_loss"], target_price=pos["target_price"], trailing_sl=trailing_sl,
                premium=premium, qty=qty, dt=dt,
                current_max_loss_rs=current_max_loss_rs(dt), current_profit_protection_rs=current_profit_protection_rs(dt),
                giveback_pct=PP_GIVEBACK_PCT,
                underlying_ts_list=udata[sym]["timestamps"], underlying_closes=udata[sym]["closes"],
                supertrend_values=ust[sym], ema_cross_series=ema_cross[sym],
                entry_candle_ts=pos["entry_candle_ts"], t=t,
                option_volumes=pos["pe_data"]["volumes"], option_ts_list=pos["pe_data"]["timestamps"],
                ladder_config=ladder_cfg,
            )
            if reason:
                pnl = (premium - pos["entry_price"]) * qty
                print(f"  EXIT  {sym} @ {dt} reason={reason} exit_price={premium} pnl={pnl:+.0f}")
                trades.append({
                    "symbol": sym, "entry_dt": str(pos["entry_dt"]), "entry_price": pos["entry_price"],
                    "exit_dt": str(dt), "exit_price": premium, "exit_reason": reason,
                    "quantity": qty, "pnl": pnl, "status": "closed",
                })
                key = (dt.date(), sym)
                if reason == "MAX_LOSS_HIT":
                    max_loss_hits_today[key] += 1
                if reason in LOSS_REPEAT_BLOCK_EXIT_REASONS:
                    loss_repeat_count_today[key] += 1
                del live[sym]

        while alert_queue and alert_queue[0][0].timestamp() <= t:
            alert_dt, syms = alert_queue.pop(0)
            try_enter_batch(alert_dt, syms)

    for sym, pos in live.items():
        trades.append({
            "symbol": sym, "entry_dt": str(pos["entry_dt"]), "entry_price": pos["entry_price"],
            "status": "still open through end of available data", "pnl": None,
        })

    (CACHE_DIR / "results_sell_range_breakout_02.json").write_text(json.dumps(trades, default=str, indent=2))

    print(f"\n[backtest] {len(trades)} trades")
    print("\n=== TRADE-WISE P&L ===")
    total = 0.0
    for i, tr in enumerate(trades, 1):
        pnl = tr.get("pnl")
        if pnl is not None:
            total += pnl
        print(f"{i}. {tr['symbol']} entry={tr['entry_dt']} exit={tr.get('exit_dt', 'N/A')} "
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
        print(f"{d}: trades={s['trades']:2d} wins={s['wins']:2d} losses={s['losses']:2d} "
              f"net_pnl={s['pnl']:+.0f}")

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    print(f"\n=== SUMMARY ===")
    print(f"Total trades: {len(trades)} (closed={len(closed)}, still-open={len(trades) - len(closed)})")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {(len(wins) / len(closed) * 100) if closed else 0:.1f}%")
    print(f"TOTAL net P&L: Rs {total:+.0f}")
    if skipped_symbols:
        print(f"\nSKIPPED symbols (no usable data/no F&O options found): {sorted(skipped_symbols)}")


if __name__ == "__main__":
    main()
