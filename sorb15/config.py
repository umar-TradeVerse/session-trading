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
    "London":   {"open": dt.time(13, 0), "deadline": dt.time(14, 45), "end": dt.time(18, 45)},
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
DAILY_LOSS_LIMIT_INR = _f("DAILY_LOSS_LIMIT_INR", 500)
MAX_CONSEC_LOSSES = _i("MAX_CONSEC_LOSSES", 4)   # losses in a row, counted across days
HALT_DAYS = _i("HALT_DAYS", 1)                    # after a streak, skip this many extra days

# ── entry timing ────────────────────────────────────────────────────────────
# The backtest enters at the open of the candle after the trigger candle. Live
# can only match that if it acts within seconds of the close. If the bot is
# later than this (restart, outage), it skips the trade rather than chase it.
MAX_ENTRY_LAG_SECONDS = _i("MAX_ENTRY_LAG_SECONDS", 90)

# ── runtime ─────────────────────────────────────────────────────────────────
POLL_SECONDS = _i("POLL_SECONDS", 20)
STATE_FILE = os.getenv("STATE_FILE", "/tmp/sorb15_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

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
