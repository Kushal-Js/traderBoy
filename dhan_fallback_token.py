"""
Fallback Dhan access token - store / check / remove (2 Oct 2026, user request). See Options/fallback_token.py.

The bot's PRIMARY login is PIN+TOTP and stays primary. This stores ONE extra access token (generated on
web.dhan.co, valid ~24 h) that the running bot switches to - without a restart - only while the primary cannot
serve: the PIN+TOTP login fails, or Dhan refuses market data on the primary token but not on this one. It goes back
to the primary as soon as that works again (checked every 15 min; every restart starts on the primary).

    python3 dhan_fallback_token.py set          # paste the token at the hidden prompt (or pipe it on stdin)
    python3 dhan_fallback_token.py status       # expiry, Dhan's check, data access, what the bot uses, PIN+TOTP cap
    python3 dhan_fallback_token.py clear        # remove it
    python3 dhan_fallback_token.py login-reset  # clear the PIN+TOTP failure count / pause (Options/login_budget.py)
                                                # - only once you know logins work again (Dhan locks at 5 failures)

The token is never taken as an argument (shell history / process list) and never printed or logged.
"""
from __future__ import annotations

import getpass
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from Options import fallback_token, login_budget
from Options.dhan_client import IST, DhanWrapper


def _bot_view() -> str:
    try:
        with urllib.request.urlopen("http://localhost:8000/feed-stats", timeout=5) as r:
            s = json.load(r)
        return (f"bot now uses: {s.get('token_source')} token"
                + (f" (fallback because: {s.get('fallback_reason')})" if s.get("token_source") == "fallback" else ""))
    except Exception:  # noqa: BLE001
        return "bot: not reachable on localhost:8000"


def _report(rec) -> None:
    d = fallback_token.describe(rec, IST)
    if not d["present"]:
        print("fallback token: none stored")
    else:
        w = DhanWrapper()
        accepted = w._token_state(rec["token"])
        data = w._data_state(rec["token"])
        print(f"fallback token: expires {d['expires']} ({d['hours_left']} h left, usable={d['usable']})")
        print(f"  Dhan accepts it: {'yes' if accepted else 'NO' if accepted is False else 'unknown (no answer)'}")
        print(f"  market data on it: {'yes' if data else 'REFUSED (DH-902)' if data is False else 'unknown (no answer)'}")
    s = login_budget.state()
    until = login_budget.paused()
    if until:
        from datetime import datetime
        print(f"PIN+TOTP logins: PAUSED until {datetime.fromtimestamp(until, IST):%d %b %H:%M} IST after "
              f"{s['consecutive']} consecutive failures (last: {s['last_error']})")
    else:
        print(f"PIN+TOTP logins: allowed ({s['consecutive']} consecutive failure(s) on record)")
    print(_bot_view())


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "set":
        token = (getpass.getpass("Dhan access token (hidden): ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
        rec, why = fallback_token.check(token)
        if rec is None:
            print(f"NOT stored: the token {why}.")
            return 2
        if DhanWrapper()._token_state(token) is False:
            print("NOT stored: Dhan rejects this token (invalid or expired). Any token stored before is kept.")
            return 2
        rec = fallback_token.save(token)
        print("stored.")
        _report(rec)
        return 0
    if cmd == "status":
        _report(fallback_token.load())
        return 0
    if cmd == "clear":
        print("removed." if fallback_token.clear() else "nothing was stored.")
        return 0
    if cmd == "login-reset":
        print("PIN+TOTP failure count cleared." if login_budget.reset() else "nothing to clear (0 failures on record).")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
