"""Configuration. Everything comes from environment variables so nothing
secret is ever committed. Railway injects these from its Variables tab."""
import datetime as dt
import os


def _f(key, default):
    v = os.getenv(key)
    return float(v) if v not in (None, "") else default


def _i(key, default):
    v = os.getenv(key)
    return int(v) if v not in (None, "") else default


def _b(key, default):
    v = os.getenv(key)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── credentials ─────────────────────────────────────────────────────────────
COINDCX_API_KEY = os.getenv("COINDCX_API_KEY", "")
COINDCX_API_SECRET = os.getenv("COINDCX_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── safety ──────────────────────────────────────────────────────────────────
# DRY_RUN=true  -> every signal is detected, logged and sent to Telegram,
#                  but NO order is ever placed. This is the default.
# Set DRY_RUN=false in Railway only when you want it to trade real money.
DRY_RUN = _b("DRY_RUN", True)

# ── market ──────────────────────────────────────────────────────────────────
PAIR = os.getenv("PAIR", "B-BTC_USDT")
RESOLUTION = "15"
TIMEFRAME_MIN = 15
LEVERAGE = _i("LEVERAGE", 10)
MARGIN_CURRENCY = os.getenv("MARGIN_CURRENCY", "INR")   # INR or USDT — whichever FUTURES wallet holds your funds
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "isolated")
# Contract spec. These defaults are what CoinDCX reports for B-BTC_USDT, and the
# bot re-reads the real values from the exchange every time it starts.
QTY_STEP = _f("QTY_STEP", 0.001)              # quantity_increment
PRICE_STEP = _f("PRICE_STEP", 0.1)            # price_increment
MIN_QTY = _f("MIN_QTY", 0.001)                # min_trade_size
MIN_NOTIONAL_USDT = _f("MIN_NOTIONAL_USDT", 60.0)

# ── money ───────────────────────────────────────────────────────────────────
CAPITAL_INR = _f("CAPITAL_INR", 5000)
RISK_PER_TRADE_INR = _f("RISK_PER_TRADE_INR", 100)
USDINR = _f("USDINR", 88.0)          # used only to convert risk into contracts
MAX_NOTIONAL_INR = CAPITAL_INR * LEVERAGE

# ── sessions (IST) ──────────────────────────────────────────────────────────
TZ = "Asia/Kolkata"
SESSIONS = {
    "London":   {"open": dt.time(14, 0), "deadline": dt.time(15, 45), "end": dt.time(19, 0)},
    "New York": {"open": dt.time(19, 0), "deadline": dt.time(20, 45), "end": dt.time(1, 15)},
}

# ── SL / TP rules (identical to the backtest) ───────────────────────────────
SL_LEVEL_BUFFER = _f("SL_LEVEL_BUFFER", 0.0015)
SL_LEVEL_BUFFER_SA2 = _f("SL_LEVEL_BUFFER_SA2", 0.0045)
PARTIAL_TP_R = _f("PARTIAL_TP_R", 0.50)
PARTIAL_TP_FRAC = _f("PARTIAL_TP_FRAC", 0.50)
TRAIL_AFTER_R = _f("TRAIL_AFTER_R", 0.50)
TRAIL_STEP_R = _f("TRAIL_STEP_R", 0.25)
BREAKEVEN_AT_R = _f("BREAKEVEN_AT_R", 1.00)
BREAKEVEN_PLUS_PCT = _f("BREAKEVEN_PLUS_PCT", 0.0016)
TAKE_PROFIT_R = _f("TAKE_PROFIT_R", 0) or None
# Force-close a trade after this many candles if it hasn't already hit its stop,
# trailing stop, or session end. 0 = off (hold until one of those, as before).
MAX_HOLD_CANDLES = _i("MAX_HOLD_CANDLES", 0)
# Skip the session entirely if its range candle's own (high - low), in price points,
# is wider than this. 0 = off (no filter, as before).
MAX_RANGE_POINTS = _f("MAX_RANGE_POINTS", 0)
# SA1 / SA4 only: stop at the range candle's opposite extreme (its low for a LONG,
# its high for a SHORT) instead of the broken level +/- SL_LEVEL_BUFFER. SA2 is
# unaffected (still level +/- SL_LEVEL_BUFFER_SA2). Off by default.
SA1_SA4_RANGE_CANDLE_SL = _b("SA1_SA4_RANGE_CANDLE_SL", False)

# ── detection thresholds ────────────────────────────────────────────────────
CLEAR_CLOSE_STRENGTH = _f("CLEAR_CLOSE_STRENGTH", 0.50)
CLEAR_TOUCH_TOLERANCE = _f("CLEAR_TOUCH_TOLERANCE", 0.15)
TAP_TOLERANCE = _f("TAP_TOLERANCE", 0.0006)
DOJI_BODY_PCT = _f("DOJI_BODY_PCT", 0.10)
PIN_WICK_MULT = _f("PIN_WICK_MULT", 2.0)
MOMENTUM_WINDOW = _i("MOMENTUM_WINDOW", 2)

# ── Optional rules. ALL OFF by default, so the baseline behaves exactly as before. ──
# SA1: a candle that TOUCHES the line (back inside the range by more than
# CLEAR_TOUCH_TOLERANCE) is not a clear breakout, even if it closed beyond it.
SA1_REQUIRE_CLEAN = _b("SA1_REQUIRE_CLEAN", False)
# SA2: count only a doji or a long-wick rejection. Not "any red candle that reached the high".
SA2_STRICT_REJECTION = _b("SA2_STRICT_REJECTION", False)
# SA2: the loosest rejection type ("any red/green candle that reached the line") also
# requires the close to have pulled back at least SA2_MIN_PULLBACK_PCT of the candle's
# own range beyond the level — not just land on the right side of it by any margin.
SA2_REQUIRE_PULLBACK = _b("SA2_REQUIRE_PULLBACK", False)
SA2_MIN_PULLBACK_PCT = _f("SA2_MIN_PULLBACK_PCT", 0.30)
# SA4: baseline gives up on the whole session the moment the FIRST candle that closes
# beyond a line turns out to be messy (still touching it) — it never looks further. When
# on, a messy first attempt is skipped instead, and SA4 keeps scanning for a later candle
# that closes beyond the line cleanly, up to the deadline.
SA4_SCAN_PAST_IMPURE_BREAKOUT = _b("SA4_SCAN_PAST_IMPURE_BREAKOUT", False)
# SA1+SA4+Tokyo engine (backtest.sa134_entry_signal) only: baseline VETOes a SA4 breakout
# the moment momentum reverses against it (session ends, no trade). When on, that same
# confirmed reversal instead FLIPS the trade to the opposite side, mirroring the flip SA1
# already has -- a test of whether the SA4 veto is leaving a real opposite move on the table.
SA4_FLIP_ON_REVERSAL = _b("SA4_FLIP_ON_REVERSAL", False)
# SA1+SA4+Tokyo portfolio (backtest.run_sa134_portfolio / main.py) only: baseline ranks
# a session's competing candidates purely by trigger time then strength when slots run
# out. When on, a candidate whose OWN symbol was just stopped out THE SAME TRADING DAY,
# now signalling the OPPOSITE direction (the stop-hunt-then-reversal pattern), outranks
# every other candidate competing for that slot -- it no longer has to out-race an
# unrelated symbol's breakout on raw timing alone.
SA134_PRIORITIZE_REVERSAL_REENTRY = _b("SA134_PRIORITIZE_REVERSAL_REENTRY", False)
# SA1+SA4+Tokyo trade management (backtest._sa134_manage / main.py manage()) only:
# baseline stops never move and the first target is 2R. When on, a 25% leg is taken at
# 1R too (legs become 25/25/25/25 at 1R/2R/3R/4R) and the stop moves to breakeven the
# moment that first leg fills -- a trade that was genuinely in profit can no longer
# round-trip all the way into a full stop-out loss.
SA134_EARLY_LEG_AND_BREAKEVEN = _b("SA134_EARLY_LEG_AND_BREAKEVEN", False)
# "Good volume" gate shared by the liquidity-hunt rules below: a candle's own volume
# must be at least VOL_MULT times the average of the SA134_VOL_WINDOW candles before it.
# Always causal (only ever looks at candles strictly before the one being judged). The
# entry-side rule (SA134_LIQUIDITY_HUNT_REVERSAL) computes this within the current
# session only, same as every other entry detector; the management-side reentry rules
# (SA134_HUNT_REENTRY) compute it over the full candle history, since by then the trade
# is already open and may run past a session boundary.
SA134_VOL_WINDOW = _i("SA134_VOL_WINDOW", 20)
SA134_VOL_MULT = _f("SA134_VOL_MULT", 1.5)
# SA1+SA4+Tokyo entry (backtest.sa134_entry_signal) only -- Umar's "liquidity hunt" rule.
# Baseline's sweep-reversal flip only looks at the SESSION'S SECOND CANDLE (prefix[0]).
# When on, the SAME flip (a candle closing beyond a line, invalidated by a pullback candle
# right after) is scanned for on EVERY candle of the session, gated by the volume filter
# above so a plain noisy wick doesn't count as a hunt -- real liquidity sweeps print volume.
SA134_LIQUIDITY_HUNT_REVERSAL = _b("SA134_LIQUIDITY_HUNT_REVERSAL", False)
# SA1+SA4+Tokyo trade management (backtest._sa134_manage_hunt / main.py) only -- Umar's
# "book the 1R leg, don't move to breakeven" rule, replacing SA134_EARLY_LEG_AND_BREAKEVEN
# for this specific ask: legs become 25/25/25/25 at 1R/2R/3R/4R but the stop is NEVER moved
# (explicitly not breakeven -- his standing rule). Two watch-for-reentry rules run alongside:
#   scenario 1 (continuation): once the 1R leg is booked, if price later makes a NEW extreme
#     beyond the 1R candle's own high/low on good volume, followed by a pullback candle, add
#     one more 25%-sized leg back in the SAME direction (stop = the pullback candle's own
#     extreme, same buffer convention as everywhere else). At most one such add-back per trade.
#   scenario 2 (reversal): once the ORIGINAL stop is hit, keep watching -- if price breaks
#     further past that same stop level on good volume, followed by a pullback candle, open a
#     brand-new position in that (continuing) direction, sized like a fresh fixed-margin entry,
#     stop = the break candle's own extreme. At most one such reentry per trade.
SA134_HUNT_REENTRY = _b("SA134_HUNT_REENTRY", False)
# SA5 (breakout + flip-on-reversal): baseline stops each leg at the exact signal candle's
# own extreme (the range candle for leg 1, the reversal candle for every flip after). When
# on, each leg's stop instead sits SL_LEVEL_BUFFER beyond that same level, same convention
# as SA1/SA4/SA2 — tests whether SA5's edge survives a less noise-sensitive stop.
SA5_USE_BUFFER_SL = _b("SA5_USE_BUFFER_SL", False)
# Baseline: a veto ends the WHOLE session, even though only that one attempt was cancelled —
# a stronger setup later in the same session (e.g. a clean SA4 breakout) never gets a look.
# When on, a veto only kills that attempt; the scan keeps going for another sub-agent's setup.
CONTINUE_AFTER_VETO = _b("CONTINUE_AFTER_VETO", False)
# Swaps SA4 for SA5 in the router (same entry trigger, but SA5 flips on a confirmed
# reversal instead of standing down, and keeps flipping after entry too). The two share
# one entry trigger so they can never both be active — this picks which one is.
USE_SA5_INSTEAD_OF_SA4 = _b("USE_SA5_INSTEAD_OF_SA4", False)
# Confirmation before entering SA1 / SA2 (SA4 already has its own):
#   off          enter on the open right after the signal candle closes (baseline)
#   next_candle  wait one candle; if it closes AGAINST the trade, cancel
#   sa4_style    wait MOMENTUM_WINDOW candles; a doji/reversal candle then a close past it cancels
VETO_MODE = os.getenv("VETO_MODE", "off")
VETO_AGENTS = os.getenv("VETO_AGENTS", "SA1,SA2")
MAX_TAPS_LOOKBACK = _i("MAX_TAPS_LOOKBACK", 12)

# ── costs (used by the backtest and by paper-mode P&L) ──────────────────────
TAKER_FEE = _f("TAKER_FEE", 0.00059)       # per side — CONFIRM against your CoinDCX account
MAKER_FEE = _f("MAKER_FEE", 0.0)
SLIPPAGE = _f("SLIPPAGE", 0.0002)          # per side
FUNDING_RATE_8H = _f("FUNDING_RATE_8H", 0.0001)
MAINT_MARGIN = _f("MAINT_MARGIN", 0.005)

# ── circuit breakers ────────────────────────────────────────────────────────
# These were never Umar's rule -- added unilaterally (see the old CLAUDE.md
# admission: "Daily loss limit Rs500 and the 4-loss halt are my safety rules"),
# and he explicitly asked (10 Nov) to have them removed from live/paper. Left
# as the raw config.py default here ONLY so the old SA1-5 engine's baseline
# fingerprint test (built against this exact value) stays meaningful -- the
# actual removal is in main()'s C.override(...), the same pattern this project
# already uses for "change a fundamental constant without disturbing the
# fingerprint" (see SESSIONS/CAPITAL_INR there). Live and paper run with BOTH
# off (0) regardless of what's written here.
DAILY_LOSS_LIMIT_INR = _f("DAILY_LOSS_LIMIT_INR", 500)
MAX_CONSEC_LOSSES = _i("MAX_CONSEC_LOSSES", 4)   # losses in a row, counted across days
HALT_DAYS = _i("HALT_DAYS", 1)                    # after a streak, skip this many extra days

# ── entry timing ────────────────────────────────────────────────────────────
# The backtest enters at the open of the candle after the trigger candle. Live
# can only match that if it acts within seconds of the close. If the bot is
# later than this (restart, outage), it skips the trade rather than chase it.
MAX_ENTRY_LAG_SECONDS = _i("MAX_ENTRY_LAG_SECONDS", 90)

# SA1+SA4+Tokyo portfolio (backtest.run_sa134_portfolio / main.py) only -- Umar's
# explicit slot-pairing rule: the two concurrent slots may only ever be one of
# backtest.SA134_COMPANION_PAIRS (BTC+Oil, BTC+NatGas, Oil+NatGas). Gold and
# Silver are in none of them, so each can still open SOLO, just never alongside
# a second symbol. Off by default like every optional rule; main() turns it on
# explicitly for live/paper trading once Umar asked for it.
SA134_ENFORCE_COMPANION_PAIRS = _b("SA134_ENFORCE_COMPANION_PAIRS", False)

# ── live-only order-book overlay (main.py enter()/manage() ONLY) ────────────
# CoinDCX's real, free, public order book (coindcx.orderbook) -- what Umar asked
# for directly: "liquidity or orders which are present, long or short." It is a
# LIVE snapshot only -- CoinDCX does not archive historical order books anywhere
# (checked directly: no such endpoint exists), so NOTHING here has ever been, or
# ever can be, run through the backtest the way every other rule in this project
# has been. Off by default; Umar's explicit instruction to turn both halves on
# despite that gap.
ORDERBOOK_FILTER = _b("ORDERBOOK_FILTER", False)
# CoinDCX's order-book endpoint only serves BTC -- Gold/Silver/Oil/NatGas all
# 422 "Invalid pair" (every naming variant tried; Delta Exchange checked too,
# no fix there either). main.py's overlay functions fail open on those symbols
# already (a 422 is caught and just skips the overlay), so this was never a
# correctness bug -- only a guaranteed-fail HTTP call, and a WARNING log line,
# on every poll for 4 of the 5 symbols. ORDERBOOK_SUPPORTED_PAIRS lets the
# overlay skip straight past those without making the doomed request at all.
ORDERBOOK_SUPPORTED_PAIRS = {"B-BTC_USDT"}
# A resting level counts as a "wall" once its size is at least this many times
# the average size across the levels being compared. Raised from 5.0 after the
# first live BTC case (a 2.19 BTC wall, 5.3x average) blocked a trade whose wall
# got blown straight through in the same candle -- loosened so only a more
# unusual size still counts, while leaving the gate itself on to keep watching.
ORDERBOOK_WALL_MULT = _f("ORDERBOOK_WALL_MULT", 8.0)
# Entry gate: skip the trade if a wall sits on the side AGAINST the breakout
# direction, within this fraction of the entry price. Tightened from 0.3% after
# that same case (the wall sat ~0.07% away) -- only a wall truly adjacent to
# entry still blocks it now.
ORDERBOOK_GATE_PCT = _f("ORDERBOOK_GATE_PCT", 0.0015)
# Stop placement: search up to this far beyond the existing fixed-buffer stop
# (SA134_SL_BUFFER_PCT) for a protective-side wall, and if found, move the stop
# just past it. Only ever pushes the stop FURTHER from entry, never tighter --
# the fixed buffer stays the floor, this can only widen it.
ORDERBOOK_STOP_SEARCH_PCT = _f("ORDERBOOK_STOP_SEARCH_PCT", 0.01)
# How far past the wall itself to park the stop, so a single sweep through the
# wall's own price doesn't immediately clip it.
ORDERBOOK_STOP_PAD_PCT = _f("ORDERBOOK_STOP_PAD_PCT", 0.0005)

# ── daily pivot confluence for SA1/SA4 (backtest.py's sa134_entry_signal) ───
# Umar's explicit ask (9 Nov): combine the session-breakout level with the
# standard-Fibonacci daily pivot he added to his own chart. Unlike the
# order-book overlay, this IS fully backtestable -- a Fibonacci pivot is just
# P/R1-3/S1-3 computed from the PRIOR day's high/low/close (prior day = the
# previous Tokyo-open-to-Tokyo-open window, matching this engine's own day
# boundary), so no live-only data is involved. Off by default, like every
# optional rule here.
SA134_PIVOT_CONFLUENCE = _b("SA134_PIVOT_CONFLUENCE", False)
# A session's broken H/L only counts as "confluent" with a pivot when it sits
# within this fraction of R1/R2/R3/P (for a LONG breakout) or S1/S2/S3/P (for
# a SHORT) -- i.e. the market's own already-drawn line and the session-range
# edge are effectively the same level. 0.3% is MY choice, not tuned against
# the Oct/Nov window it was evaluated on (see CLAUDE.md) -- a guess at "close
# enough to call it the same level," nothing more.
SA134_PIVOT_TOLERANCE_PCT = _f("SA134_PIVOT_TOLERANCE_PCT", 0.003)
# Umar's profit-booking redesign (9 Nov, iterated same day after a hard chart
# review): close the WHOLE open position the moment price reaches a same-side
# pivot (R1-3 for a LONG, S1-3 for a SHORT) AND the TWO candles after that
# keep closing further against the trade (a single reversal-shaped candle
# fires on ordinary pullback noise inside a still-strong trend, not just real
# reversals -- see the 29 Oct Tokyo case in CLAUDE.md) -- "don't let a
# profitable trade turn into a loss." Exit-only: a continuation-re-entry half
# was tried twice (volume gate + a tighter structural stop) and still didn't
# have an edge, so Umar had it dropped rather than kept behind a dead switch
# (full iteration and numbers in CLAUDE.md). Entry rules and liquidity
# detection are untouched; this only changes what happens to a position
# AFTER entry. Off by default like every optional rule.
SA134_PIVOT_PROFIT_BOOKING = _b("SA134_PIVOT_PROFIT_BOOKING", False)
# Real bug found 9 Nov (1 Nov Tokyo, 4 Nov New York): "at a pivot" was being
# satisfied the moment price merely sat within SA134_PIVOT_TOLERANCE_PCT of a
# pivot -- which can already be true AT ENTRY (a session's own breakout level
# often sits close to a pivot; 4 Nov New York's entry was already past R3
# entirely). That let the exit arm itself before the trade had gained
# anything, so an ordinary 1-candle pullback booked a small LOSS labelled
# "pivot_profit_booking" -- the opposite of the feature's whole purpose. Now
# requires the trade to actually be at least this many R in profit before the
# pivot check is even evaluated.
SA134_PIVOT_MIN_PROFIT_R = _f("SA134_PIVOT_MIN_PROFIT_R", 0.3)

# Umar's pivot-reversal re-entry (10 Nov), evidenced on real Gold charts: 30 Oct
# London SA4 LONG stopped out right near the S2 pivot, then fell straight through
# S3; 03 Nov London SA1 SHORT stopped out right near S1, then ran up through P
# and R1. In both, the ORIGINAL trade's own stop doubled as a pivot-zone test --
# the market rejected that zone in the direction the stop-out itself showed, hard.
# Rule: the moment an SA1/SA4 trade closes via stop_loss, check (same helper and
# tolerance as the entry-side confluence filter and pivot-booking) whether the
# exit price sits within SA134_PIVOT_TOLERANCE_PCT of a pivot on the side the
# REVERSE direction would need (R1-3/P for a new LONG, S1-3/P for a new SHORT).
# If so, flip and re-enter once, at the next candle's open, with the stop at the
# ORIGINAL range candle's own opposite extreme (the same H/L the original trade
# was anchored to) plus the usual SA134_SL_BUFFER_PCT hunt tolerance -- not a new,
# invented level. Managed exactly like any other SA134 trade (staged 2R/3R/4R,
# day-end). Off by default like every optional rule -- not yet backtested across
# the full symbol set or deployed live.
SA134_PIVOT_REVERSAL_REENTRY = _b("SA134_PIVOT_REVERSAL_REENTRY", False)

# Umar's SA1/SA4 entry confirmation (10 Nov) -- a deliberately different thesis
# from SA134_PIVOT_CONFLUENCE above, not an extension of it: a SHORT should be
# launched FROM a resistance zone (R1-3 or P -- there's room to fall to the next
# support before anything gets in the way), a LONG FROM a support zone (S1-3 or
# P -- room to rise to the next resistance). A SHORT whose broken level instead
# sits near SUPPORT, or a LONG near RESISTANCE, is rejected outright, not just
# left unconfirmed -- Umar's own words: that's most likely a liquidity hunt,
# with the opposite direction (or at least a change in momentum) most likely to
# fire next. Mirrors _pivot_confluent's keys exactly backwards -- see
# backtest.py's _pivot_zone_confirms. Same SA134_PIVOT_TOLERANCE_PCT tolerance.
# Off by default like every optional rule here -- not yet backtested.
SA134_PIVOT_ZONE_CONFIRM = _b("SA134_PIVOT_ZONE_CONFIRM", False)

# ── runtime ─────────────────────────────────────────────────────────────────
POLL_SECONDS = _i("POLL_SECONDS", 20)
STATE_FILE = os.getenv("STATE_FILE", "/tmp/sorb15_state.json")
# Touched once per main-loop iteration. An external watchdog (systemd timer, cron)
# can restart the service if this goes stale -- catching a genuine hang, which
# Restart=always alone cannot (it only fires once the process actually exits).
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", "/tmp/sorb15_heartbeat")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# Restricts main.py's symbol set to a subset of backtest.SA134_SYMBOLS, comma-separated
# (e.g. "BTC,Oil,NatGas"). Empty (default) = all of them. This is what lets the SAME
# codebase run as two independent systemd services with one DRY_RUN truth each -- a
# real-money service for one subset, a paper-only service for the rest -- since DRY_RUN
# itself is a single process-wide flag with no per-symbol override.
ACTIVE_SYMBOLS = os.getenv("ACTIVE_SYMBOLS", "")

import contextlib as _ctx


@_ctx.contextmanager
def override(**kw):
    """Temporarily change settings. The backtest uses this to sweep parameters
    through exactly the same code path the live bot runs."""
    g = globals()
    unknown = [k for k in kw if k not in g]
    if unknown:
        raise KeyError(f"unknown setting(s): {unknown}")
    old = {k: g[k] for k in kw}
    g.update(kw)
    try:
        yield
    finally:
        g.update(old)


BASE_URL = "https://api.coindcx.com"
PUBLIC_URL = "https://public.coindcx.com"


def validate():
    missing = [k for k, v in {
        "COINDCX_API_KEY": COINDCX_API_KEY,
        "COINDCX_API_SECRET": COINDCX_API_SECRET,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing and not DRY_RUN:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return missing
