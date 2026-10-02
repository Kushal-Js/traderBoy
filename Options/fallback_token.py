"""
Fallback Dhan access token (2 Oct 2026, user request: "build a fallback system which can use access token and
continue as usual. However, the main system, which is TOTP based, would always and should be used as a primary").

The bot's PRIMARY login is PIN+TOTP (DhanWrapper._login_pin_totp / relogin). This module stores ONE extra access
token the user generated on web.dhan.co (valid ~24 h) for when the primary cannot serve - see DhanWrapper's
"fallback" methods for when it is used and how the bot returns to the primary.

Stored in config.DHAN_FALLBACK_TOKEN_FILE (data/, gitignored), file mode 0600, written atomically. Supplied with
`python3 dhan_fallback_token.py set` (token read from stdin / a hidden prompt - never an argument, never printed).
Read fresh on every use, so a new token takes effect without a restart. Nothing here ever logs or prints the token.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config

EXPIRY_MARGIN_SECONDS = 120     # treat a token this close to its expiry as unusable


def jwt_claims(token: str) -> dict:
    """Claims of a Dhan access token (a JWT: dhanClientId, iat, exp, tokenConsumerType ...). {} if unreadable.
    The signature is not checked - Dhan's own profile call is the real validation."""
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


def _path() -> Path:
    return Path(config.DHAN_FALLBACK_TOKEN_FILE)


def _client_id() -> str:
    return str(config.DHAN_CLIENT_ID).strip().replace(".0", "")


def check(token: str) -> tuple[Optional[dict], str]:
    """(record, "") for a token that belongs to this account and is not expired, else (None, why). The record is
    what gets stored: token, client_id, issued_at, expires_at (epoch seconds), added_at."""
    token = str(token or "").strip()
    claims = jwt_claims(token)
    if not claims:
        return None, "not a Dhan access token (cannot read it)"
    if str(claims.get("dhanClientId") or "") != _client_id():
        return None, f"belongs to client {claims.get('dhanClientId')!r}, not this account"
    try:
        expires_at = float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return None, "has no expiry"
    if expires_at - time.time() <= EXPIRY_MARGIN_SECONDS:
        return None, "already expired"
    return {"token": token, "client_id": _client_id(), "issued_at": claims.get("iat"), "expires_at": expires_at,
            "added_at": time.time()}, ""


def save(token: str) -> dict:
    """Validates and stores the fallback token (atomic write, mode 0600). Raises ValueError with the reason (never
    the token) when it is not usable."""
    rec, why = check(token)
    if rec is None:
        raise ValueError(f"fallback token rejected: it {why}")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(rec, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return rec


def load() -> Optional[dict]:
    """The stored record if it is for this account (expired or not), else None."""
    try:
        rec = json.loads(_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not rec.get("token") or str(rec.get("client_id")) != _client_id():
        return None
    return rec


def usable() -> Optional[dict]:
    """The stored record while it is still usable (not within EXPIRY_MARGIN_SECONDS of expiry), else None."""
    rec = load()
    if rec is None:
        return None
    try:
        if float(rec.get("expires_at")) - time.time() <= EXPIRY_MARGIN_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    return rec


def clear() -> bool:
    try:
        _path().unlink()
        return True
    except FileNotFoundError:
        return False


def describe(rec: Optional[dict], tz) -> dict:
    """Printable summary of a record - never the token."""
    if rec is None:
        return {"present": False}
    exp = rec.get("expires_at")
    left = (float(exp) - time.time()) if exp else None
    return {"present": True,
            "expires": datetime.fromtimestamp(float(exp), tz).strftime("%d %b %Y %H:%M IST") if exp else None,
            "hours_left": round(left / 3600, 1) if left is not None else None,
            "usable": bool(left and left > EXPIRY_MARGIN_SECONDS)}
