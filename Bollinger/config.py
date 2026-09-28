"""
Central configuration for the Bollinger strategy (added 26 Sep 2026, user
request: "create another trading strategy as 'bollinger'... deploy in our
bot"). Mirrors Swing/config.py's shape/conventions - see this repo's
established pattern of one config module per strategy package.

STRATEGY, PRECISELY: a Bollinger-ribbon (5 Bollinger Bands, all period=20,
deviations 0.1/0.2/0.3/0.4/0.5) trend filter combined with a Vortex
Indicator (period=14) confirmation, entering via a software-simulated
"pending stop order" once a genuine multi-candle pullback forms against a
confirmed trend, with a swing-distance-derived dynamic stop-loss and a
1/3-of-stop/1/5-of-that trailing stop. NO PROFIT TARGET - pure trend-
following trailing-stop exit. Full derivation, every interpretation call,
and the 30-day backtest results (the "5min / lookback=2 (original)"
variant this deploys) are in trading-skills' learnings/bollinger-vortex-
strategy-30day-backtest.md and this repo's own
backtest_bollinger_vortex_9symbols_30day.py, which this package's
Bollinger/signals.py ports its indicator math from verbatim.

SCOPE: NSE EQUITY symbols, plus MCX commodities and NSE INDEX options
(NIFTY/BANKNIFTY) - added 26 Sep 2026, user request ("Update Bollinger
strategy to trade in MCX and Index Options also"), direct follow-up to
that day's own MCX/index backtest (see trading-skills' learnings/
bollinger-vortex-strategy-30day-backtest.md's "MCX (COPPER, NATURALGAS) +
index (NIFTY, BANKNIFTY) backtest" section). Underlying-reference/market-
hours dispatch (Bollinger/signals.py) and instrument resolution/pnl_
multiplier (Bollinger/trading_engine.py) mirror Swing's own MCX/INDEX
handling exactly - MCX pnl_multiplier is read from the SAME shared
Swing.mcx_registry (not a duplicate Bollinger-local copy - it's a
physical-instrument fact, not a strategy parameter). data/bollinger_
watchlist starts empty; the user adds symbols explicitly post-deploy -
same file already holds NIFTY/BANKNIFTY as of this change.

PAPER_MODE_ENABLED defaults to FALSE, deliberately, unlike every other
strategy in this codebase's history (Options/Futures/Luxury/Swing all
launched with paper mode ON first). User was shown this exact tradeoff -
zero live/paper track record for this strategy, and the backtest itself
showed real parameter sensitivity across timeframe/lookback variants - and
explicitly reconfirmed real trades twice. paper_mode_control.py's runtime
POST /paper-mode override still works for Bollinger (see that module's
STRATEGIES tuple) as a redeploy-free kill switch if this needs to change
fast.

FUND_BUCKET = "primary" (NOT a new bucket) - shares Swing's own bucket per
explicit user decision: "use primary only... both SWING and BOLLINGER use
primary, no sharing between these 2" - see .env's FUND_PRIMARY_BUCKET_PCT
(raised 85 -> 100 alongside this deploy) and fund_allocation.py's own
docstring for why buckets are independent % ceilings against total
balance, not a partitioned/contended pool - Swing and Bollinger each still
enforce their OWN separate MAX_CONCURRENT_TRADES independently.
"""
import os

# ---------------------------------------------------------------------------
# Strategy on/off
# ---------------------------------------------------------------------------
STRATEGY_ENABLED = os.getenv("BOLLINGER_STRATEGY_ENABLED", "true").lower() == "true"
ENTRY_ENABLED = os.getenv("BOLLINGER_ENTRY_ENABLED", "true").lower() == "true"
# See module docstring - deliberately FALSE by default, unlike every other
# strategy's own launch history in this repo.
PAPER_MODE_ENABLED = os.getenv("BOLLINGER_PAPER_MODE_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Capacity - independent of Swing's own MAX_CONCURRENT_TRADES
# ---------------------------------------------------------------------------
MAX_CONCURRENT_TRADES = int(os.getenv("BOLLINGER_MAX_CONCURRENT_TRADES", "5"))

# ---------------------------------------------------------------------------
# Order/position basics
# ---------------------------------------------------------------------------
BASKET_TYPE = "OPTIONS"  # only mode this package implements - always a LONG CE/PE, never futures/equity/short
QUANTITY_LOTS = int(os.getenv("BOLLINGER_QUANTITY_LOTS", "1"))
OPTIONS_PRODUCT = os.getenv("BOLLINGER_OPTIONS_PRODUCT", "MARGIN")
# MCX's own product type (26 Sep 2026, MCX support) - same default as
# Swing's own SWING_MCX_PRODUCT, independently configurable.
MCX_PRODUCT = os.getenv("BOLLINGER_MCX_PRODUCT", "MARGIN")
ORDER_TAG_PREFIX = os.getenv("BOLLINGER_ORDER_TAG_PREFIX", "Bol")
FUND_BUCKET = "primary"  # see module docstring - shares Swing's bucket, not a new one
FUNDS_CHECK_ENABLED = os.getenv("BOLLINGER_FUNDS_CHECK_ENABLED", "true").lower() == "true"
FUNDS_CHECK_BUFFER_RS = float(os.getenv("BOLLINGER_FUNDS_CHECK_BUFFER_RS", "0"))

# Which watchlist symbols are NSE index options rather than plain NSE
# equity options (26 Sep 2026, MCX/index support) - same default set and
# same "just a symbol-name set, membership resolved live" convention as
# Swing's own config.INDEX_SYMBOLS. MCX membership itself is NEVER a
# static set (see Swing/mcx_registry.py's own docstring) - it's resolved
# live via dhan_wrapper.is_mcx_commodity, config-free, and reused as-is
# here rather than duplicated.
INDEX_SYMBOLS = {s.strip().upper() for s in os.getenv("BOLLINGER_INDEX_SYMBOLS", "NIFTY,BANKNIFTY").split(",") if s.strip()}

# ---------------------------------------------------------------------------
# Risk - MAX_LOSS_PROTECTION_RS is an absolute rupee circuit-breaker, same
# role as Swing's own (not present in the source video at all - this
# repo's own real-money discipline, not the strategy's design).
# BROKER_STOP_LOSS_ENABLED defaults TRUE here (unlike Swing's own more
# cautious rollout history) since paper mode is off from day one - a
# brand-new real-money strategy should have the resting broker-side
# protection live from its very first trade, not phased in later.
# ---------------------------------------------------------------------------
MAX_LOSS_PROTECTION_RS = float(os.getenv("BOLLINGER_MAX_LOSS_PROTECTION_RS", "4500"))
BROKER_STOP_LOSS_ENABLED = os.getenv("BOLLINGER_BROKER_STOP_LOSS_ENABLED", "true").lower() == "true"
BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE = float(os.getenv("BOLLINGER_BROKER_STOP_LOSS_LIMIT_GAP_MULTIPLE", "0.05"))
ENTRY_RETRY_COOLDOWN_SECONDS = int(os.getenv("BOLLINGER_ENTRY_RETRY_COOLDOWN_SECONDS", "180"))
LTP_STALE_FORCE_EXIT_MINUTES = float(os.getenv("BOLLINGER_LTP_STALE_FORCE_EXIT_MINUTES", "5"))
MARKET_OPEN_TIME = os.getenv("BOLLINGER_MARKET_OPEN_TIME", "09:00")

# ---------------------------------------------------------------------------
# Market hours / square-off policy (26 Sep 2026, user request: "Update
# market hours and timings and Square OFF policies as we have for SWING
# strategy currently") - same three rules, same default timings, same
# reasoning as Swing/config.py's own FRIDAY_SQUARE_OFF_TIME/MCX_FRIDAY_
# SQUARE_OFF_TIME/INDEX_DAILY_SQUARE_OFF_TIME (see those docstrings for
# the full incident history - weekend-gap protection for every
# non-MCX/non-index symbol, MCX's own later Friday close, and NIFTY/
# BANKNIFTY's own daily (not just weekly) no-overnight-carry rule).
# Independently configurable from Swing's own via the BOLLINGER_ prefix,
# but same defaults - see Swing/trading_engine.py's _monitor_tick for the
# enforcement logic this package's own _monitor_tick now mirrors.
# ---------------------------------------------------------------------------
FRIDAY_SQUARE_OFF_ENABLED = os.getenv("BOLLINGER_FRIDAY_SQUARE_OFF_ENABLED", "true").lower() == "true"
FRIDAY_SQUARE_OFF_TIME = os.getenv("BOLLINGER_FRIDAY_SQUARE_OFF_TIME", "15:25")
MCX_FRIDAY_SQUARE_OFF_TIME = os.getenv("BOLLINGER_MCX_FRIDAY_SQUARE_OFF_TIME", "23:25")
INDEX_DAILY_SQUARE_OFF_ENABLED = os.getenv("BOLLINGER_INDEX_DAILY_SQUARE_OFF_ENABLED", "true").lower() == "true"
INDEX_DAILY_SQUARE_OFF_TIME = os.getenv("BOLLINGER_INDEX_DAILY_SQUARE_OFF_TIME", "15:25")

# ---------------------------------------------------------------------------
# Monitor loop cadence - own values, independent of Swing's own tick loop
# (no shared tick, only the candle_feed data underneath is shared).
# ---------------------------------------------------------------------------
MONITOR_INTERVAL_SECONDS = int(os.getenv("BOLLINGER_MONITOR_INTERVAL_SECONDS", "5"))
SYMBOL_PACING_SECONDS = float(os.getenv("BOLLINGER_SYMBOL_PACING_SECONDS", "0.35"))

# ---------------------------------------------------------------------------
# Strategy parameters - the exact "5min / lookback=2 (original)" backtested
# variant. See backtest_bollinger_vortex_9symbols_30day.py's own
# INTERPRETATION-CALLS docstring for what each of these means precisely.
# ---------------------------------------------------------------------------
SIGNAL_INTERVAL_MINUTES = int(os.getenv("BOLLINGER_SIGNAL_INTERVAL_MINUTES", "5"))
BB_PERIOD = int(os.getenv("BOLLINGER_BB_PERIOD", "20"))
BB_DEVIATIONS = (0.1, 0.2, 0.3, 0.4, 0.5)  # video's stated 5 bands, widest = 0.5
VORTEX_PERIOD = int(os.getenv("BOLLINGER_VORTEX_PERIOD", "14"))  # not stated in the video - standard default
SWING_FRACTAL_LOOKBACK = int(os.getenv("BOLLINGER_SWING_FRACTAL_LOOKBACK", "2"))
MIN_PULLBACK_CANDLES = int(os.getenv("BOLLINGER_MIN_PULLBACK_CANDLES", "2"))
# MIN_STOP_PCT raised 0.01 -> 0.05 on 28 Sep 2026. WHY: the per-trade stop
# is computed from the underlying's swing distance but then applied to the
# OPTION PREMIUM (hard_stop = entry_premium * (1 - stop_pct)). At the old 1%
# floor that was ~7 option ticks on a ~Rs 38 premium, and the trailing stop
# armed after only ~2-3 ticks of profit - inside normal bid-ask noise, so
# the median trade lasted 2-3 minutes and most exits were noise. 5% gives the
# trade room to work (trailing arms at +1.67%, trails 1.67% behind the best
# price). Chosen from bollinger_research.py's pre-registered variants:
# resting entry + 5% premium stop was the best on real option prices for
# 28 Aug-25 Sep (+Rs 11,229 on the 15-stock watchlist, entry AND exit
# slippage included) - roughly breakeven, NOT a proven edge. See
# trading-skills learnings/bollinger-backtest-lookahead-bias-entry-timing.md.
MIN_STOP_PCT = float(os.getenv("BOLLINGER_MIN_STOP_PCT", "0.05"))
TRAILING_STOP_FRACTION = 1.0 / 3.0   # video's stated ratio
TRAILING_STEP_FRACTION = 1.0 / 5.0   # video's stated ratio (of the trailing-stop distance)

# ---------------------------------------------------------------------------
# WS candle feed - Bollinger's OWN flag (independently configurable), but
# defaults to matching Swing's own current live value (SWING_USE_WS_
# CANDLES=true in .env) since Bollinger calls into Swing.candle_feed
# directly (shared module, shared subscriptions - see Bollinger/signals.py).
# ---------------------------------------------------------------------------
USE_WS_CANDLES = os.getenv("BOLLINGER_USE_WS_CANDLES", "true").lower() == "true"
WS_STALE_AFTER_SECONDS = float(os.getenv("BOLLINGER_WS_STALE_AFTER_SECONDS", "90"))
# Throttle on recomputing the full pullback-state-machine replay (see
# Bollinger/signals.py's own module docstring for why this must replay the
# FULL retained series every time it runs, not a short window) - the
# underlying 5-min candle it depends on can't have changed more often than
# this anyway, so re-running the O(n) replay on every single 5s monitor
# tick would be pure waste. Close to Swing's own SUPERTREND_REFRESH_
# SECONDS (15) cadence.
SIGNAL_REFRESH_SECONDS = float(os.getenv("BOLLINGER_SIGNAL_REFRESH_SECONDS", "15"))
# REST fallback's own lookback window (days) when the WS candle feed isn't
# fresh/long enough yet - generous on purpose so a REST-served signal is
# never computed over materially LESS history than the WS path would give
# (candle_feed.py keeps up to DISK_RESTORE_LOOKBACK_DAYS=55 days/
# MAX_BARS_KEPT=2600 bars), avoiding the two paths silently disagreeing.
REST_LOOKBACK_DAYS = int(os.getenv("BOLLINGER_REST_LOOKBACK_DAYS", "60"))

# ---------------------------------------------------------------------------
# Entry mode (28 Sep 2026) - HOW a pending order becomes a trade.
#
#   "resting"   (default) - behaves like a real resting stop order. When a
#               5-min bar CLOSES, the pending order's trigger price is known
#               (e.g. "buy if price goes above 1864.70"). During the NEXT
#               bar, the moment the live underlying price touches that
#               trigger, we enter - we don't wait for that bar to close.
#               This is the source video's actual design ("place a pending
#               stop order at the swing point") and only uses information
#               that already exists when the order is armed.
#   "bar_close" - the original live behaviour: wait for the 5-min bar that
#               crossed the trigger to CLOSE, then enter. By then the
#               breakout has usually already run, so the entry is late.
#
# WHY this changed: every earlier Bollinger backtest entered DURING the bar
# that crossed the trigger while already using that bar's full high/low -
# i.e. it knew the breakout would happen before it did (lookahead). That is
# where the reported +Rs 155,652 came from. With honest timing the old
# bar_close logic lost -Rs 1,25,156 over 28 Aug-25 Sep on real option prices
# (entry+exit slippage); resting entry improved results in every period and
# price model tested. Needs Swing's WS candle feed to see the forming bar -
# if the feed is stale for a symbol, that symbol simply isn't entered.
# ---------------------------------------------------------------------------
ENTRY_MODE = os.getenv("BOLLINGER_ENTRY_MODE", "resting").strip().lower()

# ---------------------------------------------------------------------------
# Minimum ATM option premium (28 Sep 2026). An entry is skipped if the
# chosen ATM option's live price is below this. WHY: NSE option prices move
# in Rs 0.05 ticks, so on a cheap option the bid-ask spread is a big % of
# the price - on a ~Rs 1 option, entering and exiting costs ~8-10% each way
# before the trade does anything. SUZLON (Rs ~1 premiums) lost on all 23
# backtested trades; MOTHERSON (low premium) was the worst symbol in every
# honest variant. Checked on the REAL price at the moment of entry, so it
# adapts automatically (e.g. premiums shrink near expiry). NSE options only
# - MCX has different tick sizes/lot economics that weren't studied, so MCX
# is exempt.
# ---------------------------------------------------------------------------
MIN_ATM_PREMIUM_RS = float(os.getenv("BOLLINGER_MIN_ATM_PREMIUM_RS", "5"))

# ---------------------------------------------------------------------------
# Volume-floor entry gate (28 Sep 2026, user request after Bollinger's first
# real trade: PHOENIXLTD 29 SEP 1940 PUT, stopped out in 2 min for -Rs 367.50
# with a shadow reversal-filter VolRatio of 0.92 - recommended_combo_blocks=
# True, logged but not enforced). Skips a MAIN-strategy entry (paper or real)
# when the last CLOSED 5-min candle's volume is below RATIO_MIN x its own
# 20-bar average - same check and thresholds as Swing's three gates (see
# Swing/config.py's MCX/NSE/INDEX_VOLUME_FLOOR_* docstrings for the evidence
# behind 1.2x and why the index floor is a separate 0.6x). Fails OPEN when
# the ratio can't be computed (no volume data, <21 bars). Not applied to the
# separate Bollinger Hold-Long paper strategy. Not backtested on Bollinger.
# ---------------------------------------------------------------------------
NSE_VOLUME_FLOOR_GATE_ENABLED = os.getenv("BOLLINGER_NSE_VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
NSE_VOLUME_FLOOR_RATIO_MIN = float(os.getenv("BOLLINGER_NSE_VOLUME_FLOOR_RATIO_MIN", "1.2"))
INDEX_VOLUME_FLOOR_GATE_ENABLED = os.getenv("BOLLINGER_INDEX_VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
INDEX_VOLUME_FLOOR_RATIO_MIN = float(os.getenv("BOLLINGER_INDEX_VOLUME_FLOOR_RATIO_MIN", "0.6"))
MCX_VOLUME_FLOOR_GATE_ENABLED = os.getenv("BOLLINGER_MCX_VOLUME_FLOOR_GATE_ENABLED", "true").lower() == "true"
MCX_VOLUME_FLOOR_RATIO_MIN = float(os.getenv("BOLLINGER_MCX_VOLUME_FLOOR_RATIO_MIN", "1.2"))
VOLUME_FLOOR_LOOKBACK_BARS = 20

# ---------------------------------------------------------------------------
# Paper book (28 Sep 2026). Before this, paper mode only LOGGED that an
# entry was skipped - no trade was simulated, so paper mode produced no
# evidence at all. Now paper mode runs the full entry and exit logic on live
# option prices without placing orders (see Bollinger/paper_book.py).
# Open paper positions persist here across restarts; closed paper trades go
# to history/<date>_bollinger_paper_trades.log.
# ---------------------------------------------------------------------------
PAPER_POSITIONS_FILE = os.getenv("BOLLINGER_PAPER_POSITIONS_FILE", "data/bollinger_paper_positions.json")

# ---------------------------------------------------------------------------
# Trade direction and exit style (28 Sep 2026). Defaults keep the behaviour
# deployed earlier the same day; the research-backed combination is
# BOLLINGER_SIDES=long + BOLLINGER_EXIT_MODE=hold_to_close ("variant B" in
# trading-skills learnings/bollinger-backtest-lookahead-bias-entry-timing.md).
# Per user instruction (28 Sep 2026) that combination is NOT switched on
# here - it runs as its own separate paper strategy, Bollinger Hold-Long
# (see the HOLD_LONG_* block below), so its results never mix with this
# deployed strategy's. Leave these two at their defaults.
#
# SIDES
#   "both" - take BULLISH entries (buy CE) and BEARISH entries (buy PE).
#   "long" - take BULLISH entries only. WHY: measured on the underlying,
#            after a resting-order BULLISH entry the stock moved on average
#            +0.18% to +0.23% by the day's close (3-4x what buying the same
#            stocks at a random minute gave), consistently in both test
#            periods. BEARISH entries showed no edge (about 0% or negative).
#
# EXIT_MODE
#   "trailing"      - hard stop at stop_pct of the premium plus the
#                     1/3-trailing stop (see MIN_STOP_PCT). Exits within
#                     minutes.
#   "hold_to_close" - no percentage stop and no trailing stop: the position
#                     is held until DAILY_SQUARE_OFF_TIME and closed then.
#                     The ONLY early exit is MAX_LOSS_PROTECTION_RS (the
#                     rupee circuit-breaker). WHY: the edge above builds up
#                     over hours; tight stops were closing trades in minutes,
#                     before it showed up. An option also needs a bigger move
#                     than the stock to beat time decay + spread, and holding
#                     gives the move time to happen.
#   Research result for long + hold_to_close, 15-stock watchlist, entry+exit
#   slippage, replayed with the 5-position cap: +Rs 43,234 over 20 days on
#   real option prices (about +Rs 2,160/day), worst drawdown -Rs 26,774,
#   daily results ranged -Rs 18k to +Rs 39k. Positive in every period and
#   price model tested, but only ~1 month of real option data - confirm in
#   paper trading before real money.
#
# DAILY_SQUARE_OFF_TIME - used only in hold_to_close mode: every non-MCX
# position (real and paper) is closed at this time each trading day, and no
# new non-MCX entries are taken after it. 15:15 matches the research (Dhan's
# 1-min NSE data, which the research replayed, ends at 15:14). MCX keeps its
# existing Friday-only rule - MCX wasn't part of this research.
# ---------------------------------------------------------------------------
SIDES = os.getenv("BOLLINGER_SIDES", "both").strip().lower()
EXIT_MODE = os.getenv("BOLLINGER_EXIT_MODE", "trailing").strip().lower()
DAILY_SQUARE_OFF_TIME = os.getenv("BOLLINGER_DAILY_SQUARE_OFF_TIME", "15:15")

# ---------------------------------------------------------------------------
# Expiry roll (28 Sep 2026). If the nearest-expiry ATM option has this many
# TRADING days or fewer left (counting weekdays after today up to and
# including the expiry day; exchange holidays are ignored), buy the NEXT
# expiry's ATM option instead. 0 disables the rule (the shared contract
# picker still rolls on expiry day itself, as before).
#
# WHY: with hold_to_close, a trade is held for hours. An option with 1-2
# days left loses value to time decay very fast and swings wildly with
# small price moves, so the same correct stock move can still lose money.
# Next month's option decays much more slowly. Example: on Mon 28 Sep with
# stock options expiring Tue 29 Sep (1 trading day left), entries use the
# October contract.
#
# If next month's contract can't be found, or it and its nearby strikes all
# fail the usual liquidity checks, the entry is SKIPPED - we never fall back
# to the near-expiry contract, so the rule's effect can be measured cleanly.
# NSE only; MCX is exempt (different expiry cycle, not studied).
#
# NOT BACKTESTED: next-month option prices weren't in the research data, so
# this rule's first test is the paper book itself (the trade log shows which
# contract each trade used).
# ---------------------------------------------------------------------------
# Default 0 (OFF) for this deployed Bollinger strategy, so its behaviour stays
# exactly as deployed on the morning of 28 Sep. The roll is tested in the
# separate Bollinger Hold-Long paper strategy below (HOLD_LONG_ROLL_...).
ROLL_EXPIRY_WITHIN_TRADING_DAYS = int(os.getenv("BOLLINGER_ROLL_EXPIRY_WITHIN_TRADING_DAYS", "0"))

# ---------------------------------------------------------------------------
# Bollinger Hold-Long - a SEPARATE paper-only strategy (28 Sep 2026).
#
# WHAT: the same Bollinger/Vortex pending-order signal as the deployed
# strategy above, traded with different rules:
#   - BULLISH entries only (buy CE), never BEARISH;
#   - resting-order entry (always - independent of ENTRY_MODE above);
#   - no percentage or trailing stop - held until HOLD_LONG_DAILY_SQUARE_OFF_
#     TIME, with MAX_LOSS_PROTECTION_RS as the only early exit;
#   - rolls to the next expiry when HOLD_LONG_ROLL_EXPIRY_WITHIN_TRADING_DAYS
#     or fewer trading days remain (see ROLL_EXPIRY_WITHIN_TRADING_DAYS above
#     for how the roll works);
#   - same Rs 5 minimum premium, same ATM contract selection and liquidity
#     checks, same Friday/index square-off times.
# See config.SIDES / config.EXIT_MODE above for the research behind each
# rule.
#
# SEPARATE BY DESIGN (user instruction: "live deployed strategy results
# shouldn't be mixed with this new approach"): its own paper positions file,
# its own trade log (history/<date>_bollinger_hold_long_paper_trades.log),
# its own event log (history/<date>_bollinger_hold_long_events.log) and its
# own endpoint (GET /bollinger/hold-long/paper-trades). It only READS the
# shared signal; acting on a signal here never consumes it for the deployed
# strategy or vice versa, and an open position in one never blocks the
# other. PAPER ONLY: this strategy has no real-order path at all.
# ---------------------------------------------------------------------------
HOLD_LONG_ENABLED = os.getenv("BOLLINGER_HOLD_LONG_ENABLED", "true").lower() == "true"
HOLD_LONG_ROLL_EXPIRY_WITHIN_TRADING_DAYS = int(os.getenv("BOLLINGER_HOLD_LONG_ROLL_EXPIRY_WITHIN_TRADING_DAYS", "2"))
HOLD_LONG_DAILY_SQUARE_OFF_TIME = os.getenv("BOLLINGER_HOLD_LONG_DAILY_SQUARE_OFF_TIME", "15:15")
HOLD_LONG_PAPER_POSITIONS_FILE = os.getenv("BOLLINGER_HOLD_LONG_PAPER_POSITIONS_FILE",
                                           "data/bollinger_hold_long_paper_positions.json")
