"""
Climactic-entry cooldown guard (prototype, added 24 Sep 2026, user
request after the day's reversal_filter_shadow.log showed a clean
pattern: every real PE trade entered today with RSI(14) < 15 AND
Kaufman Efficiency Ratio(10) > 0.75 on the underlying's 5-min candles
went on to STOP_LOSS_HIT - ADANIENSOL -Rs.2,598.75, INDUSINDBK
-Rs.2,205.00, NAUKRI -Rs.1,787.50, HDFCLIFE -Rs.2,365.00 (4/4, -Rs.
8,956.25 combined). The two trades that landed at a non-extreme RSI
(~31) and lower ER (~0.6) split win/loss instead. See
backtest_climactic_entry_guard.py for the full same-day backtest this
threshold is drawn from, and reversal_filters.py's own module docstring
for why "RSI-extreme alone" was previously found net-negative over a
139-trade/15-day sample - THIS module deliberately requires RSI-extreme
AND high-ER together (mirrors reversal_filters.py's own climax_combo:
RSI-extreme alone is too blunt, but RSI-extreme co-occurring with a
second exhaustion signal is a much more surgical signature).

LIVE INTEGRATION (added 24 Sep 2026, explicit user go-ahead after
reviewing the same-day backtest - see [[feedback-live-trading-safety]]):
guard_entry()/poll_pending() below wire this into Options/Futures/
Luxury's shared _breakout_entry_fn (the SOLE entry path for all three
packages since 21 Sep 2026 - see each package's own option_main.py/
futures_main.py/luxury_main.py), gated per-package behind
config.CLIMACTIC_GUARD_ENABLED (default false - an explicit kill switch,
same pattern as BREAKOUT_PAPER_MODE_ENABLED, given this has exactly ONE
day/7 trades of backtest evidence behind it and zero live shadow-mode
track record before being wired in as a real gate). Deployed alongside
all 3 packages' BREAKOUT_PAPER_MODE_ENABLED turned on, so while this
guard now runs against real live alerts, no REAL money is at risk while
paper mode stays on - see breakout_paper_engine.py's own docstring for
paper mode's own real-vs-simulated boundary.

DECISION LOGGING (added 24 Sep 2026, user request: "everything should be
logged... stored in a file so that restart shouldn't kill the records"):
_log_event() writes ONE line per alert this guard evaluates - not just
the climactic ones - to history/<date>_climactic_entry_guard.log via
trade_history.append_jsonl, the exact same on-disk mechanism real_trades.
log/breakout_paper_trades.log/reversal_filter_shadow.log already use.
Four event types: "entered_immediately" (not climactic - the common
case), "deferred" (climactic, cooldown started), "entered_after_cooldown"
(cleared - carries rsi/er at both alert and resolution, minutes_waited,
resolved_option_type, and `flipped` if that differs from the alert's own
side), "skipped_timeout" (never cleared within COOLDOWN_MAX_WAIT_MINUTES).
Each record also carries entry_result_status/entry_result_reason (resolve_
fn's own return value), so this log alone shows what the guard decided
AND what actually happened to the trade - joinable against real_trades.
log/breakout_paper_trades.log by (strategy, symbol, option_type, time) for
end-of-day analysis. Being a file under history/, it survives a restart by
construction - this does NOT fix _pending itself still being in-memory
(see KNOWN LIMITATION above): a deferred alert lost to a restart gets its
"deferred" row but never a matching resolution row, which is itself a
visible, analyzable signal of the gap rather than a silent one.

KNOWN LIMITATION: `_pending` below is in-memory only, like most other
per-session state in this codebase (position_store's own caches, the
duplicate-order guard, etc.) - a dhanboy.service restart while an alert
is deferred silently drops it (no order ever placed for that alert, no
error surfaced). Given today alone saw 3 restarts, this is a real,
non-theoretical gap, not just a theoretical one - flagged here rather
than silently accepted.

WHAT THIS ADDS ON TOP OF reversal_filters.py's existing climax_combo:
that filter's answer is binary (block / don't block) forever for this
alert. This module instead treats a climactic reading as "not yet, not
never" - it re-checks the SAME underlying's RSI/ER on each subsequent
5-min candle close, and:
  - clears the moment RSI has returned to a normal, non-extreme band
    (COOLDOWN_RSI_LOW/HIGH) OR ER has dropped back out of "extended-move"
    territory (COOLDOWN_ER_MAX) - either condition alone is enough,
    since either one independently describes "the exhaustion signature
    is gone";
  - at that moment, reads CURRENT momentum off the same RSI reading
    (>50 => bullish/CE, <50 => bearish/PE) rather than blindly re-firing
    the original alert's direction - the point being that by the time a
    climactic move has actually cooled off, the tradeable direction may
    no longer be the one the original alert fired on (a sharp PE alert
    born out of a one-way flush can, once it stops falling, resolve into
    either "still falling, just calmer" (stay PE) or "that was the
    flush, now bouncing" (flip to CE) - this only commits to a side
    once the cooldown itself has revealed which);
  - if neither condition clears within COOLDOWN_MAX_WAIT_MINUTES, gives
    up entirely (SKIP) rather than holding the alert open indefinitely -
    same hard-cap safety-valve shape as Options/dhan_client.py's own
    GAP_DOWN_MAX_DELAY_MINUTES (nothing here should be able to sit
    "pending" for a full session)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Literal, Optional
from zoneinfo import ZoneInfo

from reversal_filters import _compute_rsi, _efficiency_ratio_at, _fetch_indicators_sync
from trade_history import append_jsonl

logger = logging.getLogger("climactic_entry_guard")
IST = ZoneInfo("Asia/Kolkata")

# Every decision this guard makes (not just the climactic ones - see
# _log_event) - added 24 Sep 2026, user request: "everything should be
# logged... so that restart shouldn't kill the records". Written via
# trade_history.append_jsonl, the SAME history/<date>_<name>.log-on-disk
# mechanism real_trades.log/breakout_paper_trades.log/reversal_filter_
# shadow.log already use - survives a dhanboy.service restart by
# construction (it's a file, not process memory), unlike `_pending`
# itself (see module docstring's KNOWN LIMITATION, still unresolved -
# this only makes the DECISION HISTORY durable, not in-flight state).
DECISION_LOG_NAME = "climactic_entry_guard"

# Thresholds to CALL something climactic in the first place. RSI legs are
# deliberately tighter than reversal_filters.py's own RSI_OVERBOUGHT/
# RSI_OVERSOLD (70/30) - this guard only wants to catch the genuinely
# extreme tail, not the same broad "strong trend" band that module's own
# docstring already found net-negative as a standalone filter (see this
# module's own docstring).
#
# ER_CLIMAX_MIN raised 0.75->0.80 (user request, 24 Sep 2026) - deliberate
# tightening, NOT backtest-optimal for that specific day: at 0.80, two of
# today's four real climactic losers (ADANIENSOL ER=0.789, NAUKRI
# ER=0.771) fall BELOW the bar and would no longer be flagged - in the
# same-day backtest those two accounted for ~Rs.2,428.75 of the guard's
# ~Rs.4,633.75 improvement (NAUKRI fully avoided via cooldown-timeout
# skip, ADANIENSOL's loss cut via delayed entry). Trades this stricter
# for fewer false positives / higher-confidence flags against catching
# less of the same-day tail - see backtest_climactic_entry_guard.py for
# the full trade-by-trade comparison at both thresholds.
CLIMAX_RSI_OVERBOUGHT, CLIMAX_RSI_OVERSOLD = 75.0, 25.0
ER_CLIMAX_MIN = 0.80

# Thresholds to call the cooldown CLEARED. Deliberately NOT the mirror
# image of CLIMAX_RSI_OVERBOUGHT/CLIMAX_RSI_OVERSOLD (75/25) - clearing
# at exactly 75/25 again would let a move that's merely stopped getting
# MORE extreme (still pinned at RSI 26) through immediately, which is not
# the same as "back to normal". 40-60 is the traditional RSI "no
# directional bias" band; ER_COOLDOWN_MAX sits well below ER_CLIMAX_MIN
# so a move has to genuinely lose its straight-line character, not just
# ease off slightly.
COOLDOWN_RSI_LOW, COOLDOWN_RSI_HIGH = 40.0, 60.0
COOLDOWN_ER_MAX = 0.45

# Hard safety cap - mirrors GAP_DOWN_MAX_DELAY_MINUTES's role (Options/
# config.py): past this, give up rather than hold an alert open all day.
# Lowered 30->20 (user request, 24 Sep 2026) - both trades that actually
# triggered the guard in the same-day backtest (INDUSINDBK, HDFCLIFE)
# never cleared within 30min either, so this tightens how long a deferred
# alert sits pending before being dropped, not observed to change either
# backtested outcome at 20min specifically (would need a rerun to confirm
# - both were still climactic at the 20min mark in that data).
COOLDOWN_MAX_WAIT_MINUTES = 20

RSI_PERIOD = 14
ER_PERIOD = 10

Decision = Literal["ENTER_NOW", "DEFER", "ENTER_AFTER_COOLDOWN", "SKIP_COOLDOWN_TIMEOUT"]


@dataclass
class GuardResult:
    decision: Decision
    resolved_option_type: Optional[str] = None  # only set for ENTER_NOW / ENTER_AFTER_COOLDOWN
    rsi: Optional[float] = None
    er: Optional[float] = None
    minutes_waited: float = 0.0


def is_climactic(rsi: Optional[float], er: Optional[float], option_type: str) -> bool:
    """True if this alert's own direction is already RSI-extreme AND the
    underlying's recent move has been highly efficient (straight-line,
    little pullback) - the same two-signal combo backtest_climactic_
    entry_guard.py validates. `option_type` is the ALERT's requested
    side ("CE"/"PE"), not necessarily what ends up being traded."""
    if rsi is None or er is None:
        return False
    rsi_extreme = rsi > CLIMAX_RSI_OVERBOUGHT if option_type == "CE" else rsi < CLIMAX_RSI_OVERSOLD
    return rsi_extreme and er > ER_CLIMAX_MIN


def cooldown_cleared(rsi: Optional[float], er: Optional[float]) -> bool:
    """True once EITHER signal alone says the exhaustion condition that
    triggered the defer is gone - see module docstring for why either is
    sufficient on its own."""
    if rsi is not None and COOLDOWN_RSI_LOW <= rsi <= COOLDOWN_RSI_HIGH:
        return True
    if er is not None and er < COOLDOWN_ER_MAX:
        return True
    return False


def momentum_direction(rsi: Optional[float]) -> Optional[str]:
    """Reads CE/PE off which side of 50 RSI currently sits - only called
    once cooldown_cleared() is already True, i.e. RSI is either back in
    the 40-60 neutral band (in which case this still leans on which side
    of 50 it landed) or the ER condition cleared instead (RSI could still
    be anywhere). Returns None only if RSI itself is unavailable."""
    if rsi is None:
        return None
    return "CE" if rsi >= 50.0 else "PE"


def evaluate(rsi: Optional[float], er: Optional[float], alert_option_type: str) -> GuardResult:
    """Single-shot entry point for a FRESH alert (no pending cooldown yet).
    Real integration (once approved - see module docstring) would call
    this first; a DEFER result means register the (strategy, symbol,
    option_type) tuple for cooldown_step() polling on each subsequent
    5-min candle instead of placing the order now."""
    if not is_climactic(rsi, er, alert_option_type):
        return GuardResult(decision="ENTER_NOW", resolved_option_type=alert_option_type, rsi=rsi, er=er)
    return GuardResult(decision="DEFER", rsi=rsi, er=er)


def cooldown_step(rsi: Optional[float], er: Optional[float], deferred_since: datetime, now: datetime) -> GuardResult:
    """Re-check for an alert already in DEFER. Called once per subsequent
    5-min candle close for as long as the pending cooldown is being
    watched."""
    waited = (now - deferred_since).total_seconds() / 60.0
    if cooldown_cleared(rsi, er):
        direction = momentum_direction(rsi)
        if direction is None:
            return GuardResult(decision="SKIP_COOLDOWN_TIMEOUT", rsi=rsi, er=er, minutes_waited=waited)
        return GuardResult(decision="ENTER_AFTER_COOLDOWN", resolved_option_type=direction, rsi=rsi, er=er, minutes_waited=waited)
    if waited >= COOLDOWN_MAX_WAIT_MINUTES:
        return GuardResult(decision="SKIP_COOLDOWN_TIMEOUT", rsi=rsi, er=er, minutes_waited=waited)
    return GuardResult(decision="DEFER", rsi=rsi, er=er, minutes_waited=waited)


def simulate_series(
    closes: list[float], timestamps: list[datetime], alert_idx: int, alert_option_type: str,
) -> dict:
    """Pure-function replay used by the backtest: given a full closes/
    timestamps series (5-min bars) and the index of the ALERT candle,
    returns what the guard would have decided and, if it wasn't ENTER_NOW,
    the index/time it cleared (or timed out) at. No I/O, no side effects -
    everything the backtest needs to then go look up a real option
    premium at the resulting index."""
    rsi_series = _compute_rsi(closes, RSI_PERIOD)
    first = evaluate(rsi_series[alert_idx], _efficiency_ratio_at(closes, alert_idx, ER_PERIOD), alert_option_type)
    if first.decision == "ENTER_NOW":
        return {"decision": "ENTER_NOW", "resolved_option_type": alert_option_type,
                "entry_idx": alert_idx, "entry_time": timestamps[alert_idx],
                "rsi_at_alert": first.rsi, "er_at_alert": first.er}

    deferred_since = timestamps[alert_idx]
    for i in range(alert_idx + 1, len(closes)):
        rsi_i = rsi_series[i]
        er_i = _efficiency_ratio_at(closes, i, ER_PERIOD)
        step = cooldown_step(rsi_i, er_i, deferred_since, timestamps[i])
        if step.decision == "ENTER_AFTER_COOLDOWN":
            return {"decision": "ENTER_AFTER_COOLDOWN", "resolved_option_type": step.resolved_option_type,
                    "entry_idx": i, "entry_time": timestamps[i], "minutes_waited": step.minutes_waited,
                    "rsi_at_alert": first.rsi, "er_at_alert": first.er,
                    "rsi_at_entry": step.rsi, "er_at_entry": step.er}
        if step.decision == "SKIP_COOLDOWN_TIMEOUT":
            return {"decision": "SKIP_COOLDOWN_TIMEOUT", "minutes_waited": step.minutes_waited,
                    "rsi_at_alert": first.rsi, "er_at_alert": first.er}
    return {"decision": "SKIP_COOLDOWN_TIMEOUT", "minutes_waited": None,
            "rsi_at_alert": first.rsi, "er_at_alert": first.er}


# --------------------------------------------------------------------- #
# Live integration - see module docstring's "LIVE INTEGRATION" section.
# --------------------------------------------------------------------- #
RECHECK_INTERVAL_SECONDS = 300  # matches the 5-min bar RSI/ER are computed on

ResolveFn = Callable[[str, str], Awaitable[dict]]


def _log_event(**fields) -> None:
    """Appends one line to history/<date>_climactic_entry_guard.log -
    called for EVERY alert the guard evaluates, not just the climactic
    ones (an `event="entered_immediately"` row for a normal alert is what
    makes this log a complete audit trail rather than only showing the
    interesting cases). `entry_result` (when present) is resolve_fn's own
    return dict, unpacked as entry_result_status/entry_result_reason -
    same convention breakout_signal.py's own "breakout_signals" log
    already uses to link a decision to what actually happened to the
    trade, so this log can be joined against real_trades.log/
    breakout_paper_trades.log by (strategy, symbol, option_type, time)."""
    entry_result = fields.pop("entry_result", None) or {}
    record = {
        **fields,
        "entry_result_status": entry_result.get("status"),
        "entry_result_reason": entry_result.get("reason"),
        "logged_at": datetime.now(IST).isoformat(),
    }
    try:
        append_jsonl(DECISION_LOG_NAME, record)
    except Exception:  # noqa: BLE001
        logger.exception("climactic_entry_guard: could not append decision-log row - no other effect")


@dataclass
class PendingEntry:
    strategy: str
    symbol: str
    alert_option_type: str
    deferred_since: datetime
    last_checked: datetime
    rsi_at_alert: Optional[float]
    er_at_alert: Optional[float]
    resolve_fn: ResolveFn = field(repr=False)


# Keyed by (strategy, symbol, alert_option_type). Module-level, in-memory -
# see module docstring's KNOWN LIMITATION (does not survive a restart).
_pending: dict[tuple[str, str, str], PendingEntry] = {}


async def _fetch_indicators(symbol: str) -> Optional[dict]:
    """Async wrapper around reversal_filters._fetch_indicators_sync (the
    SAME blocking fetch+RSI/ADX/VolRatio/ER computation the shadow-log
    filter already uses for every real entry) - run off the event loop
    exactly like reversal_filters.evaluate_and_log already does, for the
    identical reason (a live Dhan REST call has no business blocking the
    async loop)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_indicators_sync, symbol)


async def guard_entry(strategy: str, symbol: str, option_type: str, resolve_fn: ResolveFn) -> dict:
    """Call this FIRST from _breakout_entry_fn, in place of calling
    resolve_fn(symbol, option_type) directly - behaves as a pure passthrough
    (zero added latency/behavior beyond one extra indicator fetch) for any
    non-climactic alert, and defers (places NO order, registers for
    poll_pending() instead) for a climactic one. `resolve_fn` is the
    package's own real-vs-paper dispatch closure (see each package's
    _breakout_entry_fn) - called with the ORIGINAL (symbol, option_type)
    for an immediate ENTER_NOW, or with the RESOLVED (symbol,
    resolved_option_type) once/if a deferred alert's cooldown clears -
    resolved_option_type may differ from option_type (see module
    docstring)."""
    key = (strategy, symbol, option_type)
    if key in _pending:
        logger.info("%s %s %s: alert ignored - a climactic-guard cooldown is already pending for this exact "
                    "(strategy, symbol, option_type)", strategy, symbol, option_type)
        _log_event(strategy=strategy, symbol=symbol, alert_option_type=option_type, event="ignored_already_pending")
        return {"symbol": symbol, "option_type": option_type, "status": "skipped",
                "reason": "climactic_guard_already_pending"}

    indicators = await _fetch_indicators(symbol)
    rsi = indicators.get("rsi") if indicators else None
    er = indicators.get("er") if indicators else None
    result = evaluate(rsi, er, option_type)

    if result.decision == "ENTER_NOW":
        entry_result = await resolve_fn(symbol, option_type)
        _log_event(
            strategy=strategy, symbol=symbol, alert_option_type=option_type, event="entered_immediately",
            rsi_at_alert=result.rsi, er_at_alert=result.er, alert_time=datetime.now(IST).isoformat(),
            entry_result=entry_result,
        )
        return entry_result

    now = datetime.now(IST)
    _pending[key] = PendingEntry(
        strategy=strategy, symbol=symbol, alert_option_type=option_type,
        deferred_since=now, last_checked=now, rsi_at_alert=result.rsi, er_at_alert=result.er,
        resolve_fn=resolve_fn,
    )
    logger.info(
        "%s %s %s: CLIMACTIC ENTRY DEFERRED (RSI=%s ER=%s, RSI-extreme+high-ER combo) - "
        "re-checking every ~%ds, dropped if not cleared within %dmin",
        strategy, symbol, option_type, result.rsi, result.er, RECHECK_INTERVAL_SECONDS, COOLDOWN_MAX_WAIT_MINUTES,
    )
    _log_event(
        strategy=strategy, symbol=symbol, alert_option_type=option_type, event="deferred",
        rsi_at_alert=result.rsi, er_at_alert=result.er, alert_time=now.isoformat(),
    )
    return {"symbol": symbol, "option_type": option_type, "status": "deferred",
            "reason": "climactic_entry_cooldown", "rsi": result.rsi, "er": result.er}


async def poll_pending(strategy: str) -> None:
    """Call once per monitor-loop tick (e.g. alongside _sync_pending_orders
    in each package's monitor_loop) - cheap no-op when nothing is pending
    for this strategy, and internally throttles each pending entry's own
    indicator re-fetch to RECHECK_INTERVAL_SECONDS regardless of how often
    this itself is called, so calling it on a fast (e.g. 2s) tick cadence
    is safe and does not hammer Dhan's REST endpoint."""
    now = datetime.now(IST)
    for key in [k for k in _pending if k[0] == strategy]:
        entry = _pending.get(key)
        if entry is None:
            continue  # resolved by a concurrent call between listing and here
        if (now - entry.last_checked).total_seconds() < RECHECK_INTERVAL_SECONDS:
            continue

        try:
            indicators = await _fetch_indicators(entry.symbol)
        except Exception:  # noqa: BLE001
            logger.exception("%s %s: climactic cooldown re-check fetch failed - leaving pending, retrying next cycle",
                              strategy, entry.symbol)
            continue

        rsi = indicators.get("rsi") if indicators else None
        er = indicators.get("er") if indicators else None
        entry.last_checked = now
        step = cooldown_step(rsi, er, entry.deferred_since, now)

        if step.decision == "ENTER_AFTER_COOLDOWN":
            del _pending[key]
            flipped = step.resolved_option_type != entry.alert_option_type
            logger.info(
                "%s %s: climactic cooldown CLEARED after %.1fmin (alert RSI=%s ER=%s -> now RSI=%s ER=%s) - "
                "entering %s (original alert was %s)",
                strategy, entry.symbol, step.minutes_waited, entry.rsi_at_alert, entry.er_at_alert,
                step.rsi, step.er, step.resolved_option_type, entry.alert_option_type,
            )
            entry_result = await entry.resolve_fn(entry.symbol, step.resolved_option_type)
            _log_event(
                strategy=strategy, symbol=entry.symbol, alert_option_type=entry.alert_option_type,
                event="entered_after_cooldown", resolved_option_type=step.resolved_option_type, flipped=flipped,
                rsi_at_alert=entry.rsi_at_alert, er_at_alert=entry.er_at_alert,
                rsi_at_resolution=step.rsi, er_at_resolution=step.er, minutes_waited=step.minutes_waited,
                alert_time=entry.deferred_since.isoformat(), resolved_time=now.isoformat(),
                entry_result=entry_result,
            )
        elif step.decision == "SKIP_COOLDOWN_TIMEOUT":
            del _pending[key]
            logger.info(
                "%s %s: climactic cooldown TIMED OUT after %.1fmin (alert RSI=%s ER=%s) - "
                "alert dropped, no entry placed",
                strategy, entry.symbol, step.minutes_waited, entry.rsi_at_alert, entry.er_at_alert,
            )
            _log_event(
                strategy=strategy, symbol=entry.symbol, alert_option_type=entry.alert_option_type,
                event="skipped_timeout", rsi_at_alert=entry.rsi_at_alert, er_at_alert=entry.er_at_alert,
                rsi_at_resolution=step.rsi, er_at_resolution=step.er, minutes_waited=step.minutes_waited,
                alert_time=entry.deferred_since.isoformat(), resolved_time=now.isoformat(),
            )
        # else DEFER - leave pending, try again next RECHECK_INTERVAL_SECONDS


def snapshot(strategy: str) -> list[dict]:
    """Read-only view of everything currently pending for this strategy -
    for a GET /climactic-guard/pending-style observability endpoint."""
    now = datetime.now(IST)
    return [
        {
            "symbol": e.symbol, "alert_option_type": e.alert_option_type,
            "deferred_since": e.deferred_since.isoformat(),
            "minutes_waited": round((now - e.deferred_since).total_seconds() / 60.0, 1),
            "rsi_at_alert": e.rsi_at_alert, "er_at_alert": e.er_at_alert,
        }
        for k, e in _pending.items() if k[0] == strategy
    ]
