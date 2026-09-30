"""
Expiry-day square-off (30 Sep 2026; user, 29 Sep: "On day of the monthly
expiry of Options, all open positions need to be squared off also at 15:25.
No carry forward after EXPIRY date.").

The gap: Options, Luxury, Swing and Bollinger carry positions overnight and
force them flat only on Fridays (plus Swing/Bollinger's daily index rule). A
stock option's monthly expiry is the last Tuesday of the month - not a
Friday - so a position could be carried straight into its own expiry with no
forced exit (an in-the-money stock option left to expire is settled by
physical delivery). Super Bollinger is flat every day at 15:15 and needs
nothing.

What this module answers, for one open position: "does this contract expire
today, and is it time?" The contract's expiry date comes from Dhan's
instrument master (the same row the entry used, SEM_EXPIRY_DATE - the
exchange's own date, so a holiday-shifted expiry is already right), looked
up once per contract and cached. Each package calls due_today() from its
monitor tick and closes the positions it returns with the reason
EXPIRY_DAY_SQUARE_OFF, through its normal exit path, every tick until flat.

Window: from the package's EXPIRY_DAY_SQUARE_OFF_TIME (15:25) to the NSE
close, or from its MCX time (23:25) to the MCX close for a commodity
contract. A contract whose expiry date is already in the PAST counts as due
too. If the expiry cannot be determined the position is left alone (and it
is logged once a day) - the Friday square-off still applies to it.

New entries need no change: on an expiry day the contract pickers already
roll to the next expiry (dhan_client.get_atm_option and friends).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime
from typing import Optional

logger = logging.getLogger("expiry_square_off")

NSE_CLOSE = dtime(15, 30)
MCX_CLOSE = dtime(23, 55)

_expiry: dict[tuple[str, Optional[str]], date] = {}
_unknown_on: dict[tuple[str, Optional[str]], date] = {}


def contract_expiry(trading_symbol: str, exchange: Optional[str] = None) -> Optional[date]:
    """Blocking on the first call for a contract (scans the instrument
    master); cached afterwards. exchange: "NSE" | "MCX" | None."""
    key = (trading_symbol, exchange)
    if key in _expiry:
        return _expiry[key]
    today = date.today()
    if _unknown_on.get(key) == today:
        return None
    expiry = None
    try:
        from Options.dhan_client import dhan_wrapper
        value = dhan_wrapper._instrument_meta(trading_symbol, exchange).get("expiry_date")
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date) and value == value:          # NaT is a date subclass and is != itself
            expiry = value
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: expiry date not found in the instrument master (%s) - no expiry-day square-off for it",
                       trading_symbol, exc)
    if expiry is None:
        _unknown_on[key] = today
        return None
    _expiry[key] = expiry
    return expiry


def in_window(now: datetime, start_hhmm: str, is_mcx: bool) -> bool:
    hour, minute = map(int, start_hhmm.split(":"))
    return now.weekday() < 5 and dtime(hour, minute) <= now.time() < (MCX_CLOSE if is_mcx else NSE_CLOSE)


async def due_today(trading_symbol: str, is_mcx: bool, now: datetime, nse_time: str, mcx_time: str) -> bool:
    """True when `trading_symbol` expires today (or already has) and the
    square-off window for its exchange is open."""
    if not in_window(now, mcx_time if is_mcx else nse_time, is_mcx):
        return False
    exchange = "MCX" if is_mcx else "NSE"
    key = (trading_symbol, exchange)
    if key in _expiry:
        expiry = _expiry[key]
    else:
        expiry = await asyncio.get_running_loop().run_in_executor(None, contract_expiry, trading_symbol, exchange)
    return expiry is not None and expiry <= now.date()
