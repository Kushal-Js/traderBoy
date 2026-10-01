"""
Parabolic SAR for Unified Momentum and the Scalper - can it give EARLIER entries and EARLIER reversal exits than
the Supertrend signals the two strategies use now? (1 Oct 2026, user: "Analyze and deep dive to use Parabolic SAR
technical indicator which can help our UM and Scalper strategy to identify early entry and reversals",
https://www.investopedia.com/trading/introduction-to-parabolic-sar/).

PSAR (J. Welles Wilder, 1978): SAR(next) = SAR + AF x (EP - SAR), EP = the trend's extreme point, AF starts at af0
and grows by `step` at every new EP up to af_max; in an uptrend the SAR may not rise above the two prior lows (in a
downtrend: not below the two prior highs); when a bar's low (high) crosses the SAR the trend flips and the new SAR
starts at the old EP. Read here on CLOSED candles (a flip is known at the close of the bar that crossed) or, for
"trail", as a resting stop at the SAR level of the forming candle (known from closed candles) checked minute by
minute. Parameters fixed BEFORE looking at results: the textbook 0.02/0.02/0.20 and one slower set 0.01/0.01/0.10.

Questions:
  1. EARLY ENTRY - is the PSAR already on the signal's side when the Supertrend signal fires, and how many minutes
     earlier did it flip? Variants: PSAR agreement as a FILTER on today's entries; PSAR flips taken in the
     direction of the trend filter the strategy already uses (EMA200 regime; the article's own advice) as EXTRA
     entries, or as the ONLY entries.
  2. REVERSALS - does a PSAR exit close trades earlier and better? Added on top of today's exits (first one wins):
     "flip" = the PSAR turned against the position on a closed candle after having been with it; "trail" = the
     price touched the SAR level while it was on the protective side.

Scalper: BANKNIFTY 1-min, 1 Sep - 1 Oct, the LIVE settings (1 lot = 30, max loss 1,500, profit protection above
3,000 peak at 2%, no target, 20% hard stop, Supertrend exit on a candle CLOSE, daily stop 3,000, fresh-formation
re-entry, 15:25 out); option 1-min OHLC exits; "est. real" = Dhan charges + 1 point slippage per fill
(research_scalper_banknifty_2lot_variants.real_cost). PSAR on the same 1-min candles.
Unified Momentum: the deployed config (research_unified_super_strategy.portfolio: A 2 slots pullback calls + hedge,
B 2 slots momentum PUTs, NIFTY chop gate 0.10, premium >= 10, Rs 1.2L), 3 Aug - 29 Sep walk-forward HYBRID picks,
modelled P&L. PSAR on each stock's 5-min candles (both engines read 5-min signals). Engine A's PSAR exit can only
come EARLIER than its original exit (else the original stands); a hedge that would have started after the new
exit is dropped (the supervisor only hedges an open call).
In-sample caveat: the same months the strategies were built on - read the halves (UM Aug/Sep, Scalper 1-15 Sep /
16 Sep - 1 Oct), not just the totals.

Cache only (no Dhan calls, no login):  .venv/bin/python research_psar_um_scalper.py
"""
from __future__ import annotations

import bisect
import contextlib
import io
import json
import statistics
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path

OUT = Path(f"research_results/{date.today().isoformat()}_psar_um_scalper.txt")
PARAMS = {"std": (0.02, 0.02, 0.20), "slow": (0.01, 0.01, 0.10)}


# --------------------------------------------------------------------------- #
# Wilder's Parabolic SAR
# --------------------------------------------------------------------------- #
def psar(h: list, l: list, af0: float = 0.02, step: float = 0.02, af_max: float = 0.20) -> tuple[list, list]:
    """-> (sar, trend). sar[k] = the stop level IN FORCE during bar k (computed from bars < k only);
    trend[k] = +1 / -1 AFTER bar k (a flip happens during bar k when its low / high crosses sar[k])."""
    n = len(h)
    sar, trend = [None] * n, [None] * n
    if n < 3:
        return sar, trend
    tr = 1 if h[1] + l[1] >= h[0] + l[0] else -1
    ep = max(h[0], h[1]) if tr == 1 else min(l[0], l[1])
    s = min(l[0], l[1]) if tr == 1 else max(h[0], h[1])
    af = af0
    trend[1] = tr
    for k in range(2, n):
        s = s + af * (ep - s)
        if tr == 1:
            s = min(s, l[k - 1], l[k - 2])
            sar[k] = s
            if l[k] <= s:                                  # penetrated -> flip to a downtrend
                tr, s, ep, af = -1, ep, l[k], af0
            elif h[k] > ep:
                ep, af = h[k], min(af + step, af_max)
        else:
            s = max(s, h[k - 1], h[k - 2])
            sar[k] = s
            if h[k] >= s:                                  # penetrated -> flip to an uptrend
                tr, s, ep, af = 1, ep, h[k], af0
            elif l[k] < ep:
                ep, af = l[k], min(af + step, af_max)
        trend[k] = tr
    return sar, trend


def psar_talib_port(h: list, l: list, acc: float = 0.02, af_max: float = 0.20) -> tuple[list, list]:
    """Line-by-line port of TA-Lib's TA_SAR (what blog.quantinsti.com/parabolic-sar/ calls with acceleration=0.02,
    maximum=0.2) - an independent reference for psar(). -> (out, is_long_after_bar). out[i] is TA-Lib's value
    for bar i (on a reversal bar: the NEW, overriding SAR on the other side of the price)."""
    n = len(h)
    out, longs = [None] * n, [None] * n
    diff_p, diff_m = h[1] - h[0], l[0] - l[1]               # TA-Lib: initial direction from +DM / -DM of bar 1
    is_long = not (diff_m > 0 and diff_p < diff_m)
    af = acc
    new_h, new_l = h[0], l[0]
    if is_long:
        ep, sar = h[1], new_l
    else:
        ep, sar = l[1], new_h
    for i in range(1, n):
        prev_l, prev_h = new_l, new_h
        new_l, new_h = l[i], h[i]
        if is_long:
            if new_l <= sar:
                is_long, sar = False, max(ep, prev_h, new_h)
                out[i] = sar
                af, ep = acc, new_l
                sar = max(sar + af * (ep - sar), prev_h, new_h)
            else:
                out[i] = sar
                if new_h > ep:
                    ep, af = new_h, min(af + acc, af_max)
                sar = min(sar + af * (ep - sar), prev_l, new_l)
        else:
            if new_h >= sar:
                is_long, sar = True, min(ep, prev_l, new_l)
                out[i] = sar
                af, ep = acc, new_h
                sar = min(sar + af * (ep - sar), prev_l, new_l)
            else:
                out[i] = sar
                if new_l < ep:
                    ep, af = new_l, min(af + acc, af_max)
                sar = max(sar + af * (ep - sar), prev_h, new_h)
        longs[i] = 1 if is_long else -1
    return out, longs


def crosscheck_vs_talib(h: list, l: list, c: list, label: str) -> str:
    """psar() trend vs (1) the TA-Lib port's state and (2) the QuantInsti signal (close > SAR -> +1, < -> -1),
    after both have passed their first common reversal (only the start-up differs)."""
    _sar, trend = psar(h, l, 0.02, 0.02, 0.20)
    out, longs = psar_talib_port(h, l, 0.02, 0.20)
    start = next(i for i in range(3, len(h)) if longs[i] != longs[i - 1] and trend[i] == longs[i]) + 1
    state = sum(1 for i in range(start, len(h)) if trend[i] != longs[i])
    sig = [None if out[i] is None or c[i] == out[i] else (1 if c[i] > out[i] else -1) for i in range(len(c))]
    close_sig = sum(1 for i in range(start, len(h)) if sig[i] is not None and sig[i] != trend[i])
    ties = sum(1 for i in range(start, len(h)) if sig[i] is None)
    same_sar = sum(1 for i in range(start, len(h)) if _sar[i] is not None and longs[i] == longs[i - 1]
                   and abs(_sar[i] - out[i]) > 1e-6 * max(1.0, abs(out[i])))
    return (f"PSAR cross-check on {label} ({len(h) - start:,} bars after the first common reversal): trend vs TA-Lib "
            f"port mismatches {state}; vs QuantInsti close-vs-SAR signal mismatches {close_sig} (close == SAR ties: "
            f"{ties}); SAR value differences on non-reversal bars {same_sar}")


def _selftest() -> list[str]:
    """Hand-checkable properties on a rise-then-fall series."""
    h = [10, 11, 12, 13, 14, 15, 16, 15, 14, 13, 12, 11, 10, 11, 12, 13, 14, 15, 16]
    l = [9, 10, 11, 12, 13, 14, 15, 14, 13, 12, 11, 10, 9, 10, 11, 12, 13, 14, 15]
    sar, tr = psar(h, l)
    out = []
    assert tr[2:7] == [1] * 5, tr
    for k in range(2, 7):                                  # uptrend: below price, never above the prior two lows
        assert sar[k] <= min(l[k - 1], l[k - 2]) + 1e-9 and sar[k] < l[k]
    assert all(sar[k] < sar[k + 1] for k in range(2, 6)), "the SAR must rise every bar in an uptrend"
    flip = next(k for k in range(3, len(tr)) if tr[k] == -1)
    assert l[flip] <= sar[flip] and sar[flip + 1] == max(h[:flip + 1]) or sar[flip + 1] >= max(h[flip], h[flip - 1])
    up_again = next(k for k in range(flip + 1, len(tr)) if tr[k] == 1)
    assert h[up_again] >= sar[up_again]
    out.append(f"PSAR self-test OK: uptrend bars 2-6, flip down at bar {flip} (low {l[flip]} <= SAR {sar[flip]:.2f}), "
               f"flip up at bar {up_again}")
    return out


# --------------------------------------------------------------------------- #
# Scalper (BANKNIFTY 1-min, live settings, 1 lot)
# --------------------------------------------------------------------------- #
def scalper_part(say) -> None:
    import research_scalper_banknifty_2lot_variants as V
    import research_swing_index_1min_vs_5min as S
    IST, QTY = V.IST, V.LOT
    ML, ARM, GB, HARD, DAILY = 1500.0, 3000.0, 0.02, 0.20, 3000.0
    spot = V.load_spot()
    sig = S.signals(spot, S.resample(spot, 15), 1)
    n = len(spot["ts"])
    say(crosscheck_vs_talib(spot["h"], spot["l"], spot["c"], "BANKNIFTY 1-min"))
    say("")
    above = [None if sig["st"][k] is None else spot["c"][k] > sig["st"][k] for k in range(n)]
    pss = {name: psar(spot["h"], spot["l"], *p) for name, p in PARAMS.items()}
    missing: set[str] = set()

    def sim(cfg: dict) -> list[dict]:
        trend = pss[cfg.get("ps", "std")][1]
        trades, pos, consumed, consumed_k, realised = [], None, None, None, {}

        def close(now, px, reason):
            pos.update(t_out=now, exit=px, reason=reason, pnl=(px - pos["p0"]) * QTY)
            realised[pos["day"]] = realised.get(pos["day"], 0.0) + pos["pnl"]
            trades.append(pos)

        for i, t in enumerate(spot["ts"]):
            now = t + 60
            day = datetime.fromtimestamp(t, IST).date()
            if not (V.FIRST_DAY <= day <= V.LAST_DAY) or i < 2:
                continue
            hhmm = datetime.fromtimestamp(now, IST).time()
            k = i
            if consumed is not None:                       # fresh-formation release (live since 6a66544)
                reg = sig["regime"][k]
                if reg is not None and (1 if reg else -1) != consumed:
                    consumed = None
                elif k > consumed_k and above[k] is not None and above[k - 1] is not None:
                    st_side = 1 if above[k] else -1
                    fresh = (consumed == 1 and not above[k - 1] and above[k]) or (consumed == -1 and above[k - 1] and not above[k])
                    if st_side != consumed or fresh:
                        consumed = None
            if pos is not None and pos["day"] != day:
                close(pos["last_t"], pos["last_px"], "DAY_END_NO_DATA")
                pos = None
            if pos is not None:
                j = V.bar_at(pos["opt"], t)
                c, reason, px = pos["last_px"], None, None
                if j >= 0:
                    o, hh, ll, c = (pos["opt"][x][j] for x in ("o", "h", "l", "c"))
                    pos["last_t"], pos["last_px"] = now, c
                    p0, best0 = pos["p0"], pos["best"]
                    if ll <= p0 - ML / QTY:
                        reason, px = "MAX_LOSS_HIT", min(o, p0 - ML / QTY)
                    elif (best0 - p0) * QTY > ARM and ll <= best0 * (1 - GB):
                        reason, px = "PROFIT_PROTECTION_HIT", min(o, best0 * (1 - GB))
                    elif ll <= p0 * (1 - HARD):
                        reason, px = "STOP_LOSS_HIT", min(o, p0 * (1 - HARD))
                    else:
                        pos["best"] = max(best0, hh)
                if reason is None and k > pos["k_in"]:
                    if cfg.get("st_exit", True) and sig["st"][k] is not None and (
                            (pos["side"] == 1 and spot["c"][i] < sig["st"][k]) or (pos["side"] == -1 and spot["c"][i] > sig["st"][k])):
                        reason, px = "SUPERTREND_REVERSAL_CLOSE", c
                    elif (cfg.get("psar_exit") and pos["with"] and trend[k] == -pos["side"]
                          and (not cfg.get("losing_only") or c < pos["p0"])):
                        reason, px = "PSAR_REVERSAL", c
                if trend[k] == pos["side"]:
                    pos["with"] = True
                if reason is None and hhmm >= V.SQUARE_OFF:
                    reason, px = "DAILY_SQUARE_OFF", c
                if reason:
                    close(now, px, reason)
                    pos = None
            if pos is None and hhmm < V.SQUARE_OFF and realised.get(day, 0.0) > -DAILY:
                side, src = 0, None
                st_side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
                if cfg["entry"] in ("st", "st+psar") and st_side and st_side != consumed:
                    if not cfg.get("filter") or trend[k] == st_side:
                        side, src = st_side, "ST"
                if (not side and cfg["entry"] in ("psar", "st+psar") and trend[k] is not None and trend[k - 1] is not None
                        and trend[k] != trend[k - 1] and sig["regime"][k] is not None
                        and (1 if sig["regime"][k] else -1) == trend[k]):
                    side, src = trend[k], "PSAR"
                if side:
                    got = V.option_for(day, side, t, spot["c"][i])
                    jj = V.bar_at(got[1], t) if got else -1
                    if jj < 0 and got:
                        j2 = bisect.bisect_right(got[1]["ts"], t) - 1
                        jj = j2 if j2 >= 0 and t - got[1]["ts"][j2] <= 120 else -1
                    if jj < 0:
                        missing.add(f"{datetime.fromtimestamp(now, IST):%d %b %H:%M}")
                    else:
                        p0 = got[1]["c"][jj]
                        pos = {"contract": got[0], "side": side, "day": day, "t_in": now, "k_in": k, "p0": p0, "best": p0,
                               "opt": got[1], "last_t": now, "last_px": p0, "src": src, "with": trend[k] == side,
                               "legs": None}
                        if src == "ST":
                            consumed, consumed_k = side, k
        if pos is not None:
            close(pos["last_t"], pos["last_px"], "OPEN_AT_DATA_END")
        for tr in trades:                                   # V.real_cost reads the legs
            tr["legs"] = [{"qty": QTY, "exit": tr["exit"]}]
        return trades

    def stats(trades: list[dict]) -> dict:
        by_day: dict = defaultdict(float)
        for tr in trades:
            by_day[tr["day"]] += tr["pnl"] - V.real_cost(tr)
        eq = pk = dd = 0.0
        for d in sorted(by_day):
            eq += by_day[d]; pk = max(pk, eq); dd = min(dd, eq - pk)
        half = date(2026, 9, 16)
        return {"n": len(trades), "raw": sum(t["pnl"] for t in trades), "real": sum(by_day.values()), "dd": dd,
                "wins": sum(1 for t in trades if t["pnl"] - V.real_cost(t) > 0),
                "h1": sum(v for d, v in by_day.items() if d < half), "h2": sum(v for d, v in by_day.items() if d >= half),
                "worst": min(by_day.values(), default=0), "pos": sum(1 for v in by_day.values() if v > 0),
                "days": len(by_day), "psar_src": sum(1 for t in trades if t["src"] == "PSAR"),
                "psar_exits": sum(1 for t in trades if t["reason"] == "PSAR_REVERSAL")}

    configs = [("BASELINE - live Scalper (Supertrend entries + exits)", {"entry": "st"})]
    for ps in PARAMS:
        configs += [
            (f"[{ps}] filter: take a Supertrend entry only if PSAR agrees", {"entry": "st", "filter": True, "ps": ps}),
            (f"[{ps}] exit: + PSAR reversal exit (first wins)", {"entry": "st", "psar_exit": True, "ps": ps}),
            (f"[{ps}] exit: PSAR reversal REPLACES the Supertrend exit", {"entry": "st", "psar_exit": True, "st_exit": False, "ps": ps}),
            (f"[{ps}] entry: + PSAR flips with the EMA200 regime", {"entry": "st+psar", "ps": ps}),
            (f"[{ps}] entry: PSAR flips (regime) only, Supertrend exit", {"entry": "psar", "ps": ps}),
            (f"[{ps}] PSAR system: regime PSAR flips in, PSAR flip out", {"entry": "psar", "psar_exit": True, "st_exit": False, "ps": ps}),
            (f"[{ps}] FOLLOW-UP: + PSAR exit only while the trade is LOSING", {"entry": "st", "psar_exit": True, "losing_only": True, "ps": ps}),
        ]
    say("=" * 118)
    say("SCALPER - BANKNIFTY 1-min, 1 Sep - 1 Oct 2026, live settings, 1 lot (30). est. real = after Dhan charges + 1 pt slippage/fill")
    say("=" * 118)
    say(f"{'variant':62s} {'trades':>6s} {'wins':>5s} {'raw':>9s} {'est.real':>9s} {'1-15 Sep':>9s} {'16S-1Oct':>9s} "
        f"{'max DD':>8s} {'worst':>8s} {'+days':>6s}")
    res = {}
    for name, cfg in configs:
        tr = sim(cfg)
        res[name] = tr
        s = stats(tr)
        extra = (f"  [{s['psar_src']} PSAR entries]" if s["psar_src"] else "") + (f"  [{s['psar_exits']} PSAR exits]" if s["psar_exits"] else "")
        say(f"{name:62s} {s['n']:>6d} {s['wins']:>5d} {s['raw']:>+9,.0f} {s['real']:>+9,.0f} {s['h1']:>+9,.0f} {s['h2']:>+9,.0f} "
            f"{s['dd']:>+8,.0f} {s['worst']:>+8,.0f} {s['pos']:>3d}/{s['days']:<2d}{extra}")
    if missing:
        say(f"(signals without cached option prices, skipped: {len(missing)})")

    # ---- lead / lag: is the PSAR earlier than the Supertrend? ----
    say("")
    say("Lead/lag on the Scalper's own Supertrend ENTRY signals (1-min, trading hours, 1 Sep - 1 Oct):")
    for ps, (sar, trend) in pss.items():
        agree, leads, lags, never = 0, [], [], 0
        for k in range(2, n):
            day = datetime.fromtimestamp(spot["ts"][k], IST).date()
            if not (V.FIRST_DAY <= day <= V.LAST_DAY) or datetime.fromtimestamp(spot["ts"][k] + 60, IST).time() >= V.SQUARE_OFF:
                continue
            side = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
            if not side:
                continue
            if trend[k] == side:
                agree += 1
                kk = k
                while kk > 0 and trend[kk - 1] == side and datetime.fromtimestamp(spot["ts"][kk - 1], IST).date() == day:
                    kk -= 1
                leads.append(k - kk)
            else:
                later = next((x for x in range(k + 1, min(n, k + 61)) if trend[x] == side), None)
                if later is None:
                    never += 1
                else:
                    lags.append(later - k)
        tot = agree + len(lags) + never
        say(f"  [{ps}] {tot} signals: PSAR already on that side {agree} ({agree / tot:.0%}), median {statistics.median(leads) if leads else 0:.0f} min "
            f"since its flip | PSAR flipped LATER in {len(lags)} ({len(lags) / tot:.0%}), median {statistics.median(lags) if lags else 0:.0f} min "
            f"later | not within 60 min {never}")
    base = res[configs[0][0]]
    st_exits = [t for t in base if t["reason"] == "SUPERTREND_REVERSAL_CLOSE"]
    for ps, (sar, trend) in pss.items():
        earlier, gain = [], []
        for t in st_exits:
            k_out = next(k for k in range(t["k_in"], n) if spot["ts"][k] + 60 == t["t_out"])
            ks = next((k for k in range(t["k_in"] + 1, k_out) if trend[k] == -t["side"] and any(
                trend[x] == t["side"] for x in range(t["k_in"], k))), None)
            if ks is None:
                continue
            j = V.bar_at(t["opt"], spot["ts"][ks])
            if j < 0:
                continue
            earlier.append(k_out - ks)
            gain.append((t["opt"]["c"][j] - t["exit"]) * QTY)
        if earlier:
            say(f"  [{ps}] Supertrend-close exits {len(st_exits)}: PSAR turned against the trade EARLIER in {len(earlier)} "
                f"(median {statistics.median(earlier):.0f} min earlier); exiting there instead would have changed raw P&L by "
                f"{sum(gain):+,.0f} ({sum(1 for g in gain if g > 0)} better / {sum(1 for g in gain if g < 0)} worse)")


# --------------------------------------------------------------------------- #
# Unified Momentum (deployed config)
# --------------------------------------------------------------------------- #
def um_part(say) -> None:
    import research_unified_super_strategy as U
    from Swing import regime
    IST, m, S, u = U.IST, U.m, U.S, U.u
    days = sorted(U.rsim.allowed)
    by_sym_days = defaultdict(set)
    for d, syms in U.rsim.allowed.items():
        for s_ in syms:
            by_sym_days[s_].add(d)
    with contextlib.redirect_stdout(io.StringIO()):
        cand_a = U.engine_a()
    ner = U.nifty_er()
    DEPLOYED = {"budget": U.BUDGET, "min_premium": U.MIN_PREMIUM, "max_leg": 1e9, "daily_stop": None, "addons": "hedge",
                "engines": "AB", "slots": 4, "slots_a": 2, "slots_b": 2, "b_sides": ("PUT",), "market_gate": True,
                "gate_th": 0.10}

    # ---------- 5-min PSAR per stock ----------
    five: dict = {}

    def bars5(sym):
        if sym not in five:
            b = json.loads((u.ROOT / "underlying" / f"{sym}_5min.json").read_text())
            m1 = json.loads((u.ROOT / "underlying_1m" / f"{sym}_1min.json").read_text())
            five[sym] = {"ts": b["timestamps"], "h": b["highs"], "l": b["lows"], "c": b["closes"],
                         "ps": {name: psar(b["highs"], b["lows"], *p) for name, p in PARAMS.items()},
                         "m1": m1}
        return five[sym]

    # ---------- engine A variants: earlier exit / entry filter ----------
    def a_variant(x: dict, mode: str, ps: str) -> dict | None:
        """mode: flip / trail / filter. Returns the modified candidate, or None to drop it (filter)."""
        f = bars5(x["sym"])
        sar, trend = f["ps"][ps]
        ts5 = f["ts"]
        kb = bisect.bisect_right(ts5, x["t0"] - 300) - 1             # last 5-min bar CLOSED at entry
        if mode == "filter":
            return x if kb >= 0 and trend[kb] == 1 else None
        with contextlib.redirect_stdout(io.StringIO()):
            ev = U.rs.prep({"symbol": x["sym"], "day": x["day"].isoformat(),
                            "entry_time": datetime.fromtimestamp(x["t0"], IST).strftime("%H:%M"),
                            "exit_time": datetime.fromtimestamp(x["t1"], IST).strftime("%H:%M"),
                            "entry": x["p0"], "qty": x["qty"], "exit": x["exit"]})
        tm = ev.get("tm")
        if tm is None or kb < 0:
            return x
        t_exit = None
        with_ = trend[kb] == 1
        if mode in ("flip", "flip_losing"):
            for kl in range(kb + 1, len(ts5)):
                tc = ts5[kl] + 300
                if tc > x["t1"] or datetime.fromtimestamp(tc, IST).date() != x["day"]:
                    break
                if with_ and trend[kl] == -1:
                    io_ = bisect.bisect_right(tm.ts, tc - 60) - 1
                    if mode == "flip" or (io_ >= 0 and tm.c[io_] < x["p0"]):
                        t_exit = tc
                        break
                if trend[kl] == 1:
                    with_ = True
        else:                                                       # trail: 1-min low touches the forming bar's SAR
            m1 = f["m1"]
            i0 = bisect.bisect_right(m1["timestamps"], x["t0"]) - 1
            for i in range(max(i0, 0), len(m1["timestamps"])):
                tt = m1["timestamps"][i]
                if tt < x["t0"]:
                    continue
                if tt + 60 > x["t1"]:
                    break
                kf = bisect.bisect_right(ts5, tt) - 1                 # the 5-min bar this minute belongs to
                if kf < 1 or sar[kf] is None:
                    continue
                if trend[kf - 1] == 1 and m1["lows"][i] <= sar[kf]:
                    t_exit = tt + 60
                    break
        if t_exit is None or t_exit >= x["t1"]:
            return x
        i = bisect.bisect_right(tm.ts, t_exit - 60) - 1
        if i < 0:
            return x
        px = tm.c[i]
        y = dict(x)
        y.update(t1=t_exit, exit=px, reason=f"PSAR_{mode.upper()}", pnl=m.mod(x["p0"], px, x["qty"]),
                 pnl_raw=(px - x["p0"]) * x["qty"])
        if x.get("hi") and x["hi"].get("a_t") and x["hi"]["a_t"] >= t_exit:
            y["hi"], y["hedge"] = {}, 0.0                              # the hedge would never have started
        return y

    # ---------- engine B (copy of U.engine_b with PSAR options; baseline when cfg has none) ----------
    gate_cache: dict = {}

    def engine_b(sym: str, allowed: set, cfg: dict) -> list[dict]:
        j = json.loads(Path(f"history/bt_walkforward_long/underlying_1m/{sym}_1min.json").read_text())
        spot = {"ts": [int(t) for t in j["timestamps"]], "o": j["opens"], "h": j["highs"], "l": j["lows"],
                "c": j["closes"], "v": j.get("volumes") or [0] * len(j["timestamps"])}
        fast, slow = U.resample(spot, 5), U.resample(spot, 15)
        if len(fast["ts"]) < 260:
            return []
        sig = S.signals(fast, slow, 5)
        sar, trend = psar(fast["h"], fast["l"], *PARAMS[cfg.get("ps", "std")])
        close_t = [t + 300 for t in fast["ts"]]
        b5 = {"timestamp": fast["ts"], "open": fast["o"], "high": fast["h"], "low": fast["l"], "close": fast["c"]}
        b15 = {"timestamp": slow["ts"], "open": slow["o"], "high": slow["h"], "low": slow["l"], "close": slow["c"]}
        sidx = {t: i for i, t in enumerate(spot["ts"])}
        lot = m.pr.lot(sym)
        trades, consumed, consumed_k, busy_until = [], None, None, 0
        for k in range(1, len(fast["ts"])):
            c_t = close_t[k]
            dt = datetime.fromtimestamp(c_t, IST)
            day = dt.date()
            if consumed is not None:
                reg, line, pline = sig["regime"][k], sig["st"][k], sig["st"][k - 1]
                st_side = None if line is None else (1 if fast["c"][k] > line else -1)
                prev_side = None if pline is None else (1 if fast["c"][k - 1] > pline else -1)
                if (reg is not None and (1 if reg else -1) != consumed) or (k > consumed_k and (
                        (st_side is not None and st_side != consumed) or (st_side == consumed and prev_side == -consumed))):
                    consumed = None
            if day not in allowed or c_t < busy_until or dt.time() > U.B_CUTOFF:
                continue
            side, src = 0, None
            st_sig = 1 if sig["bull"][k] else -1 if sig["bear"][k] else 0
            if cfg["entry"] in ("st", "st+psar") and st_sig and st_sig != consumed:
                if not cfg.get("filter") or trend[k] == st_sig:
                    side, src = st_sig, "ST"
            if (not side and cfg["entry"] in ("psar", "st+psar") and trend[k] is not None and trend[k - 1] is not None
                    and trend[k] != trend[k - 1] and sig["regime"][k] is not None
                    and (1 if sig["regime"][k] else -1) == trend[k]):
                side, src = trend[k], "PSAR"
            if not side:
                continue
            if k >= 20:
                avg = sum(fast["v"][k - 20:k]) / 20
                if avg and fast["v"][k] / avg < U.VOL_MIN:
                    continue
            key = (sym, k)
            if key not in gate_cache:
                gate_cache[key] = regime.classify(b5, b15, dt).allows_entry
            if not gate_cache[key]:
                continue
            sq = U.ts_of(day, U.SQUARE_OFF)
            i_sp = bisect.bisect_right(spot["ts"], c_t - 60) - 1
            spot0 = spot["c"][i_sp] if i_sp >= 0 else fast["c"][k]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    legx = m.pr.open_leg(sym, day, "LONG" if side == 1 else "SHORT", c_t, sq, spot0)
            except Exception:  # noqa: BLE001
                legx = None
            mins = legx["mins"] if legx else None
            contract = (f"{sym} {legx['expiry']:%d %b} {float(legx['strike']):g} {'CALL' if side == 1 else 'PUT'}"
                        if legx else None)
            if mins is None or not lot:
                continue
            jj = bisect.bisect_right(mins.ts, c_t - 60) - 1
            if jj < 0 or c_t - 60 - mins.ts[jj] > 300:
                continue
            p0 = mins.c[jj]
            if p0 <= 0:
                continue
            best, reason, px, t_exit = p0, None, None, None
            stop = max(p0 - U.B_MAX_LOSS / lot, p0 * (1 - U.B_HARD))
            with_ = trend[k] == side
            for i in range(jj + 1, len(mins.ts)):
                t = mins.ts[i]
                if t < c_t:
                    continue
                o, h, l, c = mins.o[i], mins.h[i], mins.l[i], mins.c[i]
                if t >= sq:
                    reason, px, t_exit = "SQUARE_OFF_15:15", o, t
                    break
                if l <= stop:
                    reason, px, t_exit = ("MAX_LOSS_HIT" if stop == p0 - U.B_MAX_LOSS / lot else "STOP_LOSS_HIT"), min(o, stop), t
                    break
                if (best - p0) * lot > U.B_PP and l <= best * (1 - U.B_GIVEBACK):
                    reason, px, t_exit = "PROFIT_PROTECTION_HIT", min(o, best * (1 - U.B_GIVEBACK)), t
                    break
                if h >= p0 * (1 + U.B_TARGET):
                    reason, px, t_exit = "TARGET_HIT", max(o, p0 * (1 + U.B_TARGET)), t
                    break
                best = max(best, h)
                if (best - p0) * lot > U.B_PP and c <= best * (1 - U.B_GIVEBACK):
                    reason, px, t_exit = "PROFIT_PROTECTION_HIT", c, t
                    break
                kl = bisect.bisect_right(close_t, t) - 1
                si = sidx.get(t)
                if cfg.get("psar_exit") == "flip" and kl > k:
                    if with_ and trend[kl] == -side and (not cfg.get("losing_only") or c < p0):
                        reason, px, t_exit = "PSAR_REVERSAL", c, t
                        break
                    if trend[kl] == side:
                        with_ = True
                if cfg.get("psar_exit") == "trail" and si is not None and kl >= k and kl + 1 < len(sar) and sar[kl + 1] is not None:
                    lvl = sar[kl + 1]                              # the SAR of the forming 5-min bar
                    if trend[kl] == side and ((side == 1 and spot["l"][si] <= lvl) or (side == -1 and spot["h"][si] >= lvl)):
                        reason, px, t_exit = "PSAR_TRAIL", c, t
                        break
                if cfg.get("st_exit", True) and kl > k and si is not None and sig["st"][kl] is not None and (
                        (side == 1 and spot["l"][si] < sig["st"][kl]) or (side == -1 and spot["h"][si] > sig["st"][kl])):
                    reason, px, t_exit = "SUPERTREND_REVERSAL_TICK", c, t
                    break
            if reason is None:
                reason, px, t_exit = "DATA_END", mins.c[-1], mins.ts[-1]
            trades.append({"engine": "B", "sym": sym, "day": day, "t0": c_t, "t1": t_exit + 60, "p0": p0, "qty": lot,
                           "contract": contract, "side": "CALL" if side == 1 else "PUT", "reason": reason, "exit": px,
                           "pnl": m.mod(p0, px, lot), "pnl_raw": (px - p0) * lot, "src": src})
            consumed, consumed_k, busy_until = side, k, t_exit + 60
        return trades

    def all_b(cfg):
        return [t for s_ in sorted(by_sym_days) for t in engine_b(s_, by_sym_days[s_], cfg)]

    def run(name, cands):
        r = U.portfolio(sorted(cands, key=lambda x: (x["t0"], x["engine"])), DEPLOYED, ner)
        st_ = U.stats(r, days)
        took = r["trades"]
        say(f"{name:64s} {st_['total']:>+9,.0f} {st_['aug']:>+8,.0f} {st_['sep']:>+8,.0f} {st_['dd']:>+8,.0f} "
            f"{st_['worst']:>+7,.0f} {st_['win_days']:>3d}/{len(days):<3d} {st_['A']:>3d}/{st_['B']:<3d}"
            + (f"  [{sum(1 for t in took if str(t.get('reason', '')).startswith('PSAR'))} PSAR exits"
               f", {sum(1 for t in took if t.get('src') == 'PSAR')} PSAR entries]"))
        return st_

    base_b = all_b({"entry": "st"})
    say("")
    say("=" * 118)
    say("UNIFIED MOMENTUM - deployed config, Rs 1.2L, 3 Aug - 29 Sep 2026 (41 days), modelled P&L (portfolio: slots, chop gate, cash)")
    say("=" * 118)
    say(f"{'variant':64s} {'total':>9s} {'Aug':>8s} {'Sep':>8s} {'max DD':>8s} {'worst':>7s} {'+days':>7s} {'A/B':>7s}")
    say("(a variant counts as robust only if BOTH PSAR settings beat the baseline in BOTH months)")
    run("BASELINE - deployed (A: SB pullback calls + hedge, B: Supertrend puts)", cand_a + base_b)
    for ps in PARAMS:
        for mode in ("flip", "trail", "filter", "flip_losing"):
            a_v = [y for y in (a_variant(x, mode, ps) for x in cand_a) if y is not None]
            label = {"flip": "exit: + PSAR flip on a closed 5-min candle", "trail": "exit: + PSAR trailing stop (1-min touch)",
                     "filter": "filter: call only if the 5-min PSAR is bullish",
                     "flip_losing": "FOLLOW-UP: + PSAR flip exit only while LOSING"}[mode]
            run(f"[{ps}] A {label}", a_v + base_b)
        for name, cfg in (("filter: put only if the 5-min PSAR is bearish", {"entry": "st", "filter": True}),
                          ("exit: + PSAR flip on a closed 5-min candle", {"entry": "st", "psar_exit": "flip"}),
                          ("exit: + PSAR trailing stop (1-min touch)", {"entry": "st", "psar_exit": "trail"}),
                          ("entry: + PSAR flips with the EMA200 regime", {"entry": "st+psar"}),
                          ("entry: PSAR flips (regime) only", {"entry": "psar"}),
                          ("PSAR system: PSAR flips in, PSAR flip out", {"entry": "psar", "psar_exit": "flip", "st_exit": False}),
                          ("FOLLOW-UP: + PSAR flip exit only while LOSING", {"entry": "st", "psar_exit": "flip", "losing_only": True})):
            run(f"[{ps}] B {name}", cand_a + all_b({**cfg, "ps": ps}))

    # ---------- lead / lag on engine B's own Supertrend put signals ----------
    say("")
    say("Lead/lag on engine B's Supertrend signals (5-min candles, every signal in the trading window - before slots/gates):")
    for ps in PARAMS:
        agree, leads, lags, never = 0, [], [], 0
        for sym in sorted(by_sym_days):
            j = json.loads(Path(f"history/bt_walkforward_long/underlying_1m/{sym}_1min.json").read_text())
            spot = {"ts": [int(t) for t in j["timestamps"]], "o": j["opens"], "h": j["highs"], "l": j["lows"],
                    "c": j["closes"], "v": j.get("volumes") or [0] * len(j["timestamps"])}
            fast, slow = U.resample(spot, 5), U.resample(spot, 15)
            if len(fast["ts"]) < 260:
                continue
            sig = S.signals(fast, slow, 5)
            _sar, trend = psar(fast["h"], fast["l"], *PARAMS[ps])
            for k in range(1, len(fast["ts"])):
                dt = datetime.fromtimestamp(fast["ts"][k] + 300, IST)
                if dt.date() not in by_sym_days[sym] or dt.time() > U.B_CUTOFF or not sig["bear"][k]:
                    continue
                if trend[k] == -1:
                    agree += 1
                    kk = k
                    while kk > 0 and trend[kk - 1] == -1:
                        kk -= 1
                    leads.append((k - kk) * 5)
                else:
                    later = next((x for x in range(k + 1, min(len(trend), k + 13)) if trend[x] == -1), None)
                    if later is None:
                        never += 1
                    else:
                        lags.append((later - k) * 5)
        tot = agree + len(lags) + never
        say(f"  [{ps}] {tot} bearish signals: PSAR already bearish {agree} ({agree / tot:.0%}), median {statistics.median(leads) if leads else 0:.0f} min "
            f"since its flip | PSAR flipped LATER {len(lags)} ({len(lags) / tot:.0%}), median {statistics.median(lags) if lags else 0:.0f} min | "
            f"not within 60 min {never}")
    a_states = defaultdict(int)
    for x in cand_a:
        f = bars5(x["sym"])
        kb = bisect.bisect_right(f["ts"], x["t0"] - 300) - 1
        a_states["bullish" if kb >= 0 and f["ps"]["std"][1][kb] == 1 else "bearish"] += 1
    tot_a = sum(a_states.values())
    say(f"  Engine A pullback-call candidates at entry (std PSAR on the last closed 5-min candle): bullish "
        f"{a_states['bullish']} ({a_states['bullish'] / tot_a:.0%}), bearish {a_states['bearish']} - the pullback "
        f"trigger mostly fires while the PSAR is still bullish, so a PSAR filter can only remove the few others")


def main() -> None:
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say(f"PARABOLIC SAR for Unified Momentum and the Scalper - run {datetime.now():%Y-%m-%d %H:%M}, cache only")
    say(f"PSAR params: {', '.join(f'{k} = AF {v[0]} step {v[1]} max {v[2]}' for k, v in PARAMS.items())}")
    for s in _selftest():
        say(s)
    say("")
    scalper_part(say)
    um_part(say)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"\nwritten {OUT}")


if __name__ == "__main__":
    main()
