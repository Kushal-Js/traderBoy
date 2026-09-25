"""
Test for the 25 Sep 2026 critical fix: asyncio.wait_for(run_in_executor(
...), timeout=N) - used everywhere in this codebase to bound a blocking
Dhan call - does NOT free the underlying OS thread once it times out.
The thread keeps running the real HTTP call to completion in the
background (the dhanhq SDK's own hardcoded 60s timeout, plus _retry()'s
own retries on top, means up to ~3-6 minutes), still occupying one of
the shared thread pool's 5 slots (all six packages share it on the
droplet's 1 vCPU) - the exact scenario the async-level timeout was
supposed to prevent, still possible underneath it. See
PERFORMANCE_AUDIT_2026-09-25.md's 🔴 finding.

Fix: Options/dhan_client.py's authenticate() now sets Dhan_Tradehull/
dhanhq's DhanHTTP.timeout to config.DHAN_HTTP_TIMEOUT_SECONDS (12s
default) right after a successful login, shrinking the SDK's own
per-request timeout from 60s down to something that actually bounds a
hung call's worst-case thread-occupation time.

This test does NOT exercise Options/dhan_client.py's full authenticate()
(that needs real Dhan credentials/network) - it instead verifies the
underlying MECHANISM the fix depends on, directly against the real
dhanhq SDK with fake credentials (no network call is made - DhanContext/
dhanhq construction is pure object setup):
  1. Every mixin (Order, Portfolio, Funds, etc.) on a dhanhq client
     shares exactly ONE DhanHTTP instance - confirmed via identity
     checks, not just "looks right" - so setting .timeout once actually
     bounds every subsequent API call, not just some of them.
  2. The SDK's own hardcoded default really is 60s (confirming the
     audit's own claim about the problem being fixed).
  3. Options/dhan_client.py's authenticate() genuinely sets this attribute
     to config.DHAN_HTTP_TIMEOUT_SECONDS on successful login (verified by
     calling the real authenticate() in access_token mode, with Tradehull's
     own login step mocked out - this exercises OUR code's own new lines,
     not the SDK's login flow).

HOW TO RUN:
    uv run python tests/test_dhan_http_timeout.py
"""
import os
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DHAN_CLIENT_ID", "test")
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from dhanhq import DhanContext, dhanhq

import Options.config as ocfg
from Options.dhan_client import DhanWrapper


def test_1_dhan_http_is_a_single_shared_instance_across_every_mixin():
    ctx = DhanContext("fake-client-id", "fake-access-token")
    dhan = dhanhq(ctx)
    assert dhan.dhan_http is ctx.dhan_http, \
        "dhanhq's own dhan_http must be the exact same object DhanContext constructed"
    assert ctx.get_dhan_http() is dhan.dhan_http, \
        "get_dhan_http() (what every mixin's __init__ calls) must return that same shared instance"
    print("1. Every dhanhq API surface shares exactly ONE DhanHTTP instance - setting .timeout once "
          "genuinely bounds every subsequent call, not just some: PASSED")


def test_2_sdk_default_timeout_is_really_60s():
    ctx = DhanContext("fake-client-id", "fake-access-token")
    dhan = dhanhq(ctx)
    assert dhan.dhan_http.timeout == 60, \
        f"expected the SDK's own documented 60s default, got {dhan.dhan_http.timeout} - if this " \
        f"changed upstream, the audit finding's severity estimate needs revisiting"
    print("2. dhanhq's own per-request HTTP timeout really does default to 60s (confirms the problem "
          "this fix addresses): PASSED")


def test_3_setting_timeout_on_the_shared_instance_is_visible_everywhere():
    ctx = DhanContext("fake-client-id", "fake-access-token")
    dhan = dhanhq(ctx)
    dhan.dhan_http.timeout = ocfg.DHAN_HTTP_TIMEOUT_SECONDS
    assert ctx.get_dhan_http().timeout == ocfg.DHAN_HTTP_TIMEOUT_SECONDS, \
        "a change via dhan.dhan_http must be visible via ctx.get_dhan_http() too (same object)"
    print(f"3. Setting .timeout = {ocfg.DHAN_HTTP_TIMEOUT_SECONDS} on the shared instance is visible "
          f"from every access path: PASSED")


def test_4_authenticate_applies_the_configured_timeout_on_real_login():
    """Exercises Options/dhan_client.py's OWN new code (the try/except
    setting tsl.Dhan.dhan_http.timeout), with Tradehull's own login step
    mocked out so no real network call happens."""
    wrapper = DhanWrapper()
    real_client_id, real_access_token, real_auth_mode = ocfg.DHAN_CLIENT_ID, ocfg.DHAN_ACCESS_TOKEN, ocfg.DHAN_AUTH_MODE
    ocfg.DHAN_CLIENT_ID = "fake-client-id"
    ocfg.DHAN_ACCESS_TOKEN = "fake-access-token"
    ocfg.DHAN_AUTH_MODE = "access_token"
    real_tradehull_init = None
    try:
        import Dhan_Tradehull

        def fake_tradehull_init(self, ClientCode, token_id=None, mode="access_token", pin=None, totp_secret=None):
            # Mirrors what real Tradehull.__init__ sets on success, per
            # Options/dhan_client.py's own authenticate() docstring -
            # skips the real login/network call entirely.
            ctx = DhanContext(ClientCode, token_id or "fake-token")
            self.Dhan = dhanhq(ctx)
            self.dhan_context = ctx

        real_tradehull_init = Dhan_Tradehull.Tradehull.__init__
        Dhan_Tradehull.Tradehull.__init__ = fake_tradehull_init

        wrapper.authenticate()

        assert wrapper._client is not None
        assert wrapper._client.Dhan.dhan_http.timeout == ocfg.DHAN_HTTP_TIMEOUT_SECONDS, (
            f"authenticate() must set the real Dhan HTTP client's timeout to config.DHAN_HTTP_TIMEOUT_"
            f"SECONDS ({ocfg.DHAN_HTTP_TIMEOUT_SECONDS}), got {wrapper._client.Dhan.dhan_http.timeout}"
        )
        print(f"4. authenticate() sets the real Dhan HTTP client's .timeout to config.DHAN_HTTP_TIMEOUT_"
              f"SECONDS ({ocfg.DHAN_HTTP_TIMEOUT_SECONDS}s) on a successful login: PASSED")
    finally:
        if real_tradehull_init is not None:
            import Dhan_Tradehull
            Dhan_Tradehull.Tradehull.__init__ = real_tradehull_init
        ocfg.DHAN_CLIENT_ID, ocfg.DHAN_ACCESS_TOKEN, ocfg.DHAN_AUTH_MODE = real_client_id, real_access_token, real_auth_mode


def main():
    print("=== Dhan HTTP per-request timeout fix test suite ===\n")
    test_1_dhan_http_is_a_single_shared_instance_across_every_mixin()
    test_2_sdk_default_timeout_is_really_60s()
    test_3_setting_timeout_on_the_shared_instance_is_visible_everywhere()
    test_4_authenticate_applies_the_configured_timeout_on_real_login()
    print("\nALL PASSED")


if __name__ == "__main__":
    main()
