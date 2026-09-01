"""
Centralized fund-bucket allocation across every real-money strategy in
this codebase - user request 1 Sep 2026 (verbatim): "create 2 funds
buckets, primary - 85% of total fund, secondary - 15% of total fund.
Primary bucket to be used for 'Swing' strategy based basket trades.
Secondary bucket to be used for Options, Futures or Luxury trades only...
This would help to run strategies in parallel without fund issues,
create a centralized system for fund management and allocate buckets
respectively."

Two buckets, each a PERCENTAGE of the account's own real-time available
balance (Dhan's own `/fundlimit`, via `Options.dhan_client.dhan_wrapper` -
the one shared, already-authenticated Dhan session every package in this
codebase reuses) - NOT a separately tracked/reserved pool of its own
money sitting somewhere, but a live SHARE recomputed fresh on every
check, straight off whatever the account's own real balance currently
is:
  - "primary" (PRIMARY_BUCKET_PCT, default 85%) - Swing's own basket/
    basket_hedge/sequential entries (Swing/trading_engine.py's own
    `_has_sufficient_funds` delegates here).
  - "secondary" (SECONDARY_BUCKET_PCT, default 15%) - Options/Futures/
    Luxury's own single-leg CE/PE entries (each package's own
    `_enter_single_position` calls `has_sufficient_bucket_funds`
    directly).

This is why running Swing alongside Options/Futures/Luxury doesn't risk
one strategy's own real order starving another of capital it needs:
each package's own proactive funds check (see `has_sufficient_bucket_
funds` below) is scoped to ITS OWN bucket's share of the total, not the
whole account's residual balance - a Swing basket that would easily fit
in the WHOLE account's available funds can still correctly get skipped
if it would eat into what the 15% secondary share leaves for Options/
Futures/Luxury, and vice versa.

Configurable (user's own request: "we might change it in future also")
via `FUND_PRIMARY_BUCKET_PCT`/`FUND_SECONDARY_BUCKET_PCT` env vars - NOT
required to sum to exactly 100 (logged as a warning at import time if
they don't, but this never blocks startup: each bucket is independently
computed as its own share of the SAME total balance, not a hard
partition that must be exhaustive or non-overlapping, so a user might
deliberately want them to sum to less than 100 - e.g. keeping some
headroom fully unallocated to either strategy group - or even want them
to overlap temporarily while migrating a percentage change).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Tuple

from Options.dhan_client import dhan_wrapper

logger = logging.getLogger("fund_allocation")

PRIMARY_BUCKET_PCT = float(os.getenv("FUND_PRIMARY_BUCKET_PCT", "85"))
SECONDARY_BUCKET_PCT = float(os.getenv("FUND_SECONDARY_BUCKET_PCT", "15"))


def warn_if_buckets_dont_sum_to_100(primary_pct: float, secondary_pct: float) -> bool:
    """Factored out of module-import time (added 1 Sep 2026) so a test
    can exercise this exact validation with arbitrary values, not just
    whatever happens to be configured. Returns True (and logs a
    warning) if the two DON'T sum to 100% within a small float-rounding
    tolerance; returns False (silently) otherwise. Never raises/blocks
    startup either way - see this module's own docstring for why a
    non-100% split isn't necessarily a misconfiguration."""
    total = primary_pct + secondary_pct
    if abs(total - 100.0) > 0.01:
        logger.warning(
            "Fund buckets don't sum to 100%% (primary=%.2f%% + secondary=%.2f%% = %.2f%%) - each "
            "bucket is still applied as its own independent share of the account's total available "
            "balance, so this isn't a startup error, just worth double-checking FUND_PRIMARY_BUCKET_PCT/"
            "FUND_SECONDARY_BUCKET_PCT if a clean 100%% split was intended.",
            primary_pct, secondary_pct, total,
        )
        return True
    return False


warn_if_buckets_dont_sum_to_100(PRIMARY_BUCKET_PCT, SECONDARY_BUCKET_PCT)

BUCKET_PCTS = {"primary": PRIMARY_BUCKET_PCT, "secondary": SECONDARY_BUCKET_PCT}


def get_bucket_available_funds(bucket: str) -> float:
    """Blocking (a real Dhan REST call, via dhan_wrapper.get_fund_limits)
    - always call via run_in_executor from async code, matching every
    other Dhan-calling function in this codebase. Fetches the account's
    REAL total available balance and returns THIS bucket's own
    allocated share of it. `bucket` must be "primary" or "secondary" -
    raises ValueError otherwise (a typo'd bucket name is a programming
    error, not a runtime condition to fail open on)."""
    if bucket not in BUCKET_PCTS:
        raise ValueError(f"Unknown fund bucket {bucket!r} - must be 'primary' or 'secondary'")
    funds = dhan_wrapper.get_fund_limits()
    total_available = funds.get("availabelBalance") or 0.0
    return total_available * (BUCKET_PCTS[bucket] / 100.0)


def snapshot() -> dict:
    """Blocking - backs the observability endpoint (GET /funds/buckets
    in main.py). Returns both buckets' own current computed share
    alongside the account's real total and the configured percentages,
    so a mismatch between what's configured and what's actually
    available is always visible without doing the arithmetic by hand."""
    funds = dhan_wrapper.get_fund_limits()
    total_available = funds.get("availabelBalance") or 0.0
    return {
        "total_available_balance": total_available,
        "primary_bucket_pct": PRIMARY_BUCKET_PCT,
        "primary_bucket_available": total_available * (PRIMARY_BUCKET_PCT / 100.0),
        "secondary_bucket_pct": SECONDARY_BUCKET_PCT,
        "secondary_bucket_available": total_available * (SECONDARY_BUCKET_PCT / 100.0),
    }


async def has_sufficient_bucket_funds(
    bucket: str, symbol: str, legs: List[Tuple[str, str, int, float]], buffer_rs: float = 0.0,
) -> bool:
    """Shared proactive funds check, used by every real-money package in
    this codebase (Swing via its own `_has_sufficient_funds` wrapper;
    Options/Futures/Luxury directly from their own `_enter_single_
    position`) - checks every leg's own STANDALONE required margin
    (Dhan's own `/margincalculator`, via `get_margin_required` - zero
    combo-awareness, see that function's own docstring) summed together,
    against `bucket`'s own allocated share of the account's real
    available balance (`get_bucket_available_funds` above), plus an
    optional `buffer_rs` on top (Swing's own basket entries pass one -
    see Swing/config.py's own FUNDS_CHECK_BUFFER_RS docstring for why;
    Options/Futures/Luxury's own single-leg entries don't need one,
    there's no multi-leg combo-margin concern for a single CE/PE buy).

    `legs`: (security_id, product_type, quantity, price) for each leg
    about to be BOUGHT - 2 for Swing's basket/basket_hedge modes, 1 for
    Swing's sequential mode AND for Options/Futures/Luxury alike.

    Fails OPEN to "sufficient" (returns True) if the check itself fails
    (a margin-API or funds-API hiccup) - a funds-check OUTAGE must never
    itself block real trading; the broker's own RMS rejection remains
    the final safety net either way if this optimistic assumption turns
    out wrong."""
    loop = asyncio.get_running_loop()
    try:
        total_required = 0.0
        for security_id, product_type, quantity, price in legs:
            margin_data = await loop.run_in_executor(
                None, dhan_wrapper.get_margin_required,
                security_id, "NSE_FNO", "BUY", quantity, product_type, price,
            )
            total_required += margin_data.get("totalMargin") or 0.0
        available = await loop.run_in_executor(None, get_bucket_available_funds, bucket)
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s: could not check %s-bucket funds before entry - proceeding optimistically "
            "(the broker's own RMS rejection remains the final safety net)", symbol, bucket,
        )
        return True

    available_with_buffer = available + buffer_rs
    if total_required > available_with_buffer:
        buffer_note = f" + Rs{buffer_rs:.2f} buffer = Rs{available_with_buffer:.2f}" if buffer_rs else ""
        logger.warning(
            "%s: skipping entry - required margin Rs%.2f (%d leg(s), summed standalone) exceeds "
            "the '%s' bucket's own available Rs%.2f%s (%.1f%% of the account's total)",
            symbol, total_required, len(legs), bucket, available, buffer_note, BUCKET_PCTS[bucket],
        )
        return False
    return True
