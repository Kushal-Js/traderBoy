"""
Pulls a Chartink scan's own CURRENT result list directly, server-side -
user request 1 Sep 2026 ("I am thinking to automate updation of our
watchlist file" -> "A specific Chartink scan URL", "once daily,
pre-market"). Unlike every other Chartink integration in this codebase
(Options/Futures/Luxury/Swing's own two webhooks), which all wait for
Chartink to PUSH an alert TO us, this instead PULLS - it reproduces the
exact request Chartink's own "Run Scan" button makes when you view the
scan page yourself: config.CHARTINK_WATCHLIST_SCAN_URL (the user's own
"LONGTERM" scan). No login or API key exists or is needed for this - it
is the same unauthenticated, public request any visitor's browser makes
to view the scan's results; confirmed live 1 Sep 2026 via a real browser
(Claude_Browser), intercepting `window.fetch`/`XMLHttpRequest` while
manually clicking the page's own "Run Scan" button, then independently
re-verified end-to-end with a bare `requests` script producing the exact
same 4-stock result set Chartink's own page showed at the time.

Two-step flow, mirroring Chartink's own frontend exactly:
  1. GET the scan page - just to receive Chartink's own CSRF cookie
     (`XSRF-TOKEN`, Laravel's standard encrypted-cookie CSRF scheme;
     `requests.Session()` holds it automatically across the two calls).
  2. POST config.CHARTINK_WATCHLIST_SCAN_CLAUSE (the exact query string
     captured from Chartink's own network request) to
     `/screener/process`, echoing the cookie's own value back as the
     `X-XSRF-TOKEN` header - URL-decoded first, since the raw cookie
     value is percent-encoded but Chartink's own frontend (via axios'
     built-in XSRF handling) sends the DECODED value as the header; the
     server rejects a still-encoded token.

STALENESS WARNING: CHARTINK_WATCHLIST_SCAN_CLAUSE (see config.py) is a
frozen SNAPSHOT of this scan's own conditions as they existed 1 Sep
2026 - it does NOT auto-update if the scan is later edited on Chartink's
own site. No public endpoint returns a scan's own live definition
without being logged in as its owner, so there's no way to re-derive it
automatically from just the URL. If the scan's own conditions change on
Chartink, CHARTINK_WATCHLIST_SCAN_CLAUSE needs a manual re-sync - re-run
the same browser-network-capture approach (open the scan page, hook
`XMLHttpRequest.prototype.send`, click "Run Scan", read back the
captured `scan_clause`) against the edited scan.
"""
from __future__ import annotations

import logging
import urllib.parse

import requests

from . import config

logger = logging.getLogger("swing_chartink_scan")

CHARTINK_SCREENER_PROCESS_URL = "https://chartink.com/screener/process"


def fetch_scan_symbols_once() -> list[str]:
    """Blocking (network calls) - always call via run_in_executor.
    Returns the scan's own current result list as NSE trading symbols
    (e.g. "BAJAJ-AUTO") in the order Chartink itself ranked them.
    Raises on any failure (network error, non-200, or an unexpected
    response shape) rather than silently returning an empty list - a
    real fetch problem must never be mistaken for "the scan genuinely
    found nothing today", which would otherwise leave the watchlist
    silently under-fed with no visible sign anything went wrong."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; DhanBoy-Swing-Watchlist-Scan/1.0)"})

    resp = session.get(config.CHARTINK_WATCHLIST_SCAN_URL, timeout=15)
    resp.raise_for_status()

    raw_xsrf = session.cookies.get("XSRF-TOKEN")
    if not raw_xsrf:
        raise RuntimeError("Chartink did not set an XSRF-TOKEN cookie - page structure may have changed")
    xsrf = urllib.parse.unquote(raw_xsrf)

    headers = {
        "X-XSRF-TOKEN": xsrf,
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": config.CHARTINK_WATCHLIST_SCAN_URL,
        "Origin": "https://chartink.com",
        "Accept": "application/json",
    }
    resp = session.post(
        CHARTINK_SCREENER_PROCESS_URL, headers=headers,
        json={"scan_clause": config.CHARTINK_WATCHLIST_SCAN_CLAUSE}, timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()

    rows = payload.get("data")
    if rows is None:
        raise RuntimeError(f"Unexpected Chartink response shape (no 'data' key): {payload}")

    symbols = [row["nsecode"].upper() for row in rows if row.get("nsecode")]
    logger.info("Chartink scan (%s) returned %d symbol(s): %s", config.CHARTINK_WATCHLIST_SCAN_URL, len(symbols), symbols)
    return symbols
