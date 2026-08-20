"""
Central configuration for the Chartink -> Groww algo trading bot.
All values can be overridden via environment variables (see .env.example).
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Groww API authentication
#   AUTH_MODE = "TOKEN"  -> uses GROWW_ACCESS_TOKEN directly (expires daily)
#   AUTH_MODE = "TOTP"   -> uses GROWW_API_KEY (the TOTP token) + GROWW_TOTP_SECRET
#   AUTH_MODE = "SECRET" -> uses GROWW_API_KEY + GROWW_API_SECRET
# See: https://groww.in/trade-api/docs/python-sdk (Step 3: Authentication)
# ---------------------------------------------------------------------------
AUTH_MODE = os.getenv("GROWW_AUTH_MODE", "TOTP").upper()
GROWW_ACCESS_TOKEN = os.getenv("GROWW_ACCESS_TOKEN", "")
GROWW_API_KEY = os.getenv("GROWW_API_KEY", "")
GROWW_API_SECRET = os.getenv("GROWW_API_SECRET", "")
GROWW_TOTP_SECRET = os.getenv("GROWW_TOTP_SECRET", "")

# Live price / order-update WebSocket feed. Off by default failure mode is
# REST polling (see groww_client.py), so this can be safely disabled if the
# socket connection is unavailable/misbehaving.
ENABLE_WS_FEED = os.getenv("ENABLE_WS_FEED", "true").lower() == "true"

# Shared secret the webhook caller must send back to us, since Chartink
# webhooks are unauthenticated by default. Optional but recommended.
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
TOP_N_STOCKS = int(os.getenv("TOP_N_STOCKS", "3"))
MAX_LIVE_POSITIONS = int(os.getenv("MAX_LIVE_POSITIONS", "3"))

TARGET_PCT = float(os.getenv("TARGET_PCT", "0.20"))          # +10% target
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.10"))    # -3% hard stop loss
TRAILING_SL_PCT = float(os.getenv("TRAILING_SL_PCT", "0.001"))  # 0.1% trailing stop

# Options are bought as ATM CALL (CE) since the strategy trades "Buy" /
# breakout style Chartink alerts. Flip to "PE" if you wire up a bearish scan.
OPTION_TYPE = os.getenv("OPTION_TYPE", "CE").upper()

QUANTITY_LOTS = int(os.getenv("QUANTITY_LOTS", "1"))  # number of lots per leg

# Order product for options: MIS = intraday (auto square-off by broker as a
# safety net; we still explicitly square off ourselves at SQUARE_OFF_TIME).
OPTIONS_PRODUCT = "MIS"

DEFAULT_EXCHANGE = "NSE"

# ---------------------------------------------------------------------------
# Timing (all times are IST / Asia-Kolkata)
# ---------------------------------------------------------------------------
MARKET_TZ = "Asia/Kolkata"
MARKET_OPEN_TIME = "09:15"
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "15:15")
MARKET_CLOSE_TIME = "15:30"

MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
LOT_SIZE_FALLBACK = int(os.getenv("LOT_SIZE_FALLBACK", "1"))
ORDER_REFERENCE_PREFIX = os.getenv("ORDER_REFERENCE_PREFIX", "Cti")  # 8-20 alnum, <=2 hyphens
