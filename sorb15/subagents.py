"""The four sub-agents.

ONE implementation, used by both the backtest and the live bot.

Every detector is CAUSAL: it is handed only the candles that have already
closed and decides from those alone. Nothing here can see the future, so a
backtest that replays history one candle at a time cannot cheat, and the live
bot behaves exactly as the backtest did.

A candle is a dict with keys t (open time, ms), open, high, low, close, volume.

A detector returns None, or a signal dict. Signals carry one of these states:
    ready   - act now: enter at the open of the NEXT candle (i.e. right now)
    pending - the setup exists but is waiting on candles that have not closed yet
    veto    - the setup was cancelled (SA4 momentum reversal)
`trigger_i` is the index of the candle whose close completed the signal.
"""
from . import config as C


# ── candle primitives ───────────────────────────────────────────────────────

def body(c):
    return abs(c["close"] - c["open"])


def rng(c):
    return c["high"] - c["low"]


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def is_doji(c):
    return rng(c) > 0 and body(c) <= C.DOJI_BODY_PCT * rng(c)


def closes_beyond(c, H, L):
    if c["close"] > H:
        return 1
    if c["close"] < L:
        return -1
    return 0


def touches_level(c, level):
    tol = level * C.TAP_TOLERANCE
    return (c["low"] - tol) <= level <= (c["high"] + tol)


def clear_of_level(c, direction, H, L):
    """At most CLEAR_TOUCH_TOLERANCE of the candle may sit back inside the range."""
    size = rng(c)
    if size <= 0:
        return False
    level = H if direction == 1 else L
    inside = (level - c["low"]) if direction == 1 else (c["high"] - level)
    return max(inside, 0.0) / size <= C.CLEAR_TOUCH_TOLERANCE


def close_strength(c, direction, H, L):
    if rng(c) <= 0:
        return 0.0
    level = H if direction == 1 else L
    beyond = (c["close"] - level) if direction == 1 else (level - c["close"])
    return max(0.0, min(1.0, beyond / rng(c)))


def is_clear_breakout_candle(c, H, L):
    d = closes_beyond(c, H, L)
    if d == 0:
        return 0, False, 0.0
    cs = close_strength(c, d, H, L)
    clear = clear_of_level(c, d, H, L)
    return (d if (clear or cs >= C.CLEAR_CLOSE_STRENGTH) else 0), clear, cs


def rejection_at(c, level, side):
    if not touches_level(c, level):
        return None
    k = C.PIN_WICK_MULT
    if side == "high":
        if c["close"] > level:
            return None
        if is_doji(c):
            return "doji_at_high"
        if upper_wick(c) >= k * body(c) and upper_wick(c) > lower_wick(c):
            return "upper_wick_rejection"
        if (not C.SA2_STRICT_REJECTION) and c["close"] < c["open"] and c["high"] >= level:
            if C.SA2_REQUIRE_PULLBACK and rng(c) > 0 and (level - c["close"]) / rng(c) < C.SA2_MIN_PULLBACK_PCT:
                return None
            return "bearish_close_at_high"
    else:
        if c["close"] < level:
            return None
        if is_doji(c):
            return "doji_at_low"
        if lower_wick(c) >= k * body(c) and lower_wick(c) > upper_wick(c):
            return "lower_wick_rejection"
        if (not C.SA2_STRICT_REJECTION) and c["close"] > c["open"] and c["low"] <= level:
            if C.SA2_REQUIRE_PULLBACK and rng(c) > 0 and (c["close"] - level) / rng(c) < C.SA2_MIN_PULLBACK_PCT:
                return None
            return "bullish_close_at_low"
    return None


def reversal_candle(c, direction):
    """A doji, or a long wick pointing against the breakout direction."""
    if is_doji(c):
        return True
    if direction == -1 and lower_wick(c) >= C.PIN_WICK_MULT * body(c):
        return True
    if direction == 1 and upper_wick(c) >= C.PIN_WICK_MULT * body(c):
        return True
    return False


def deadline_index(sess, deadline_ms):
    """Index of the last candle that closed by the deadline (-1 if none)."""
    step = C.TIMEFRAME_MIN * 60 * 1000
    return sum(1 for c in sess if c["t"] + step <= deadline_ms) - 1


# ── SA1 ─────────────────────────────────────────────────────────────────────

def SA1_second_candle_breakout(sess, H, L, deadline_i):
    """The clear breakout forms on the session's SECOND candle (the first candle
    after the range candle). No further confirmation is required."""
    if len(sess) < 1 or deadline_i < 0:
        return None
    d, clear, cs = is_clear_breakout_candle(sess[0], H, L)
    if d == 0:
        return None
    if C.SA1_REQUIRE_CLEAN and not clear:
        return None                     # it closed beyond the line but is still touching it
    return {"sub_agent": "SA1", "setup": "Second-candle clear breakout", "state": "ready",
            "direction": d, "breakout": True, "rejection": False, "momentum_shift": False,
            "trigger_i": 0, "level": H if d == 1 else L,
            "detail": f"second candle closed {'above the high' if d == 1 else 'below the low'} "
                      f"(strength {cs:.0%}{', fully clear' if clear else ''})"}


# ── SA2 ─────────────────────────────────────────────────────────────────────

def SA2_tap_rejection(sess, H, L, deadline_i):
    """Tap or near-tap of a marked line, then wait for the rejection candle.
    Rejection at the HIGH -> SHORT.  Rejection at the LOW -> LONG.
    A tap alone is never an entry."""
    taps_high = taps_low = 0
    for i in range(min(len(sess), C.MAX_TAPS_LOOKBACK)):
        c = sess[i]
        if closes_beyond(c, H, L) != 0:
            return None                 # that is a breakout; SA2 stands down
        th, tl = touches_level(c, H), touches_level(c, L)
        taps_high += th
        taps_low += tl
        if i > deadline_i:
            break                       # a rejection must close by the deadline
        if th:
            r = rejection_at(c, H, "high")
            if r:
                return {"sub_agent": "SA2", "setup": "Line tap / rejection at the HIGH",
                        "state": "ready", "direction": -1, "breakout": False,
                        "rejection": True, "momentum_shift": False, "trigger_i": i,
                        "level": H, "taps": taps_high,
                        "detail": f"{taps_high} tap(s) of the high, rejected by {r}"}
        if tl:
            r = rejection_at(c, L, "low")
            if r:
                return {"sub_agent": "SA2", "setup": "Line tap / rejection at the LOW",
                        "state": "ready", "direction": 1, "breakout": False,
                        "rejection": True, "momentum_shift": False, "trigger_i": i,
                        "level": L, "taps": taps_low,
                        "detail": f"{taps_low} tap(s) of the low, rejected by {r}"}
    return None


# ── SA3 ─────────────────────────────────────────────────────────────────────

def SA3_ny_continuation(sess, H, L, deadline_i, open_trade):
    """Continuation of an SA2 London trade still open at the New York open.
    Never a new position, never a reversal, never a duplicate."""
    if not open_trade or open_trade.get("sub_agent") != "SA2":
        return None
    d = open_trade["direction"]
    sig = SA2_tap_rejection(sess, H, L, deadline_i)
    agrees, why = False, ""
    if sig and sig["direction"] == d:
        agrees, why = True, sig["detail"]
    else:
        for i in range(min(len(sess), deadline_i + 1)):
            bd, _, cs = is_clear_breakout_candle(sess[i], H, L)
            if bd == d:
                agrees = True
                why = f"New York clear breakout in the same direction (strength {cs:.0%})"
                break
    return {"sub_agent": "SA3", "state": "continue",
            "setup": "New York continuation of the open SA2 London trade",
            "direction": d, "breakout": False,
            "rejection": bool(sig and sig["direction"] == d),
            "momentum_shift": False, "continue_trade": agrees,
            "level": open_trade["level"],
            "detail": why if agrees else
                      "New York did not satisfy the same direction — hold, do not add"}


# ── SA4 ─────────────────────────────────────────────────────────────────────

def SA4_breakout_momentum_check(sess, H, L, deadline_i):
    """A clean breakout whose candle is not touching the marked lines.

    The direction follows the breakout UNLESS a doji/reversal candle forms
    afterwards and the next candle confirms the opposite way. Momentum
    reversal takes priority.

    Causal: the bot cannot know whether a reversal is coming, so it waits.
    It watches the next MOMENTUM_WINDOW candles as they close:
      - a reversal candle followed by a candle that closes past it  -> VETO
      - a reversal candle followed by a candle that does not        -> READY
      - no reversal candle in the whole window                      -> READY
    Until then the signal is PENDING and no order is placed.
    """
    for i in range(min(len(sess), deadline_i + 1)):
        c = sess[i]
        d = closes_beyond(c, H, L)
        if d == 0:
            continue
        if not clear_of_level(c, d, H, L):
            return None
        base = {"sub_agent": "SA4", "direction": d, "breakout": True,
                "level": H if d == 1 else L,
                "setup": "Clean breakout, candle not touching the marked lines"}
        return _momentum_check(sess, d, i, base)
    return None


def _momentum_check(sess, d, i, base):
    """The momentum window after a signal candle at index i, direction d.
    pending until the window has closed; veto if a reversal candle is followed by a
    close past it; otherwise ready. Shared by SA4 and the optional SA1/SA2 veto."""
    for j in range(i + 1, i + 1 + C.MOMENTUM_WINDOW):
        if j >= len(sess):
            return {**base, "state": "pending", "rejection": False,
                    "momentum_shift": False, "pending": True, "trigger_i": None,
                    "detail": "waiting for the momentum window to close"}
        cj = sess[j]
        if not reversal_candle(cj, d):
            continue
        if j + 1 >= len(sess):
            return {**base, "state": "pending", "rejection": True,
                    "momentum_shift": False, "pending": True, "trigger_i": None,
                    "detail": "reversal candle formed, waiting for the next candle to confirm"}
        nxt = sess[j + 1]
        flipped = ((d == -1 and nxt["close"] > cj["high"]) or
                   (d == 1 and nxt["close"] < cj["low"]))
        if flipped:
            return {**base, "state": "veto", "veto": True, "rejection": True,
                    "momentum_shift": True, "trigger_i": j + 1,
                    "detail": "doji/reversal then a close past it — momentum flipped "
                              + ("UP" if d == -1 else "DOWN")}
        return {**base, "state": "ready", "rejection": True, "momentum_shift": False,
                "trigger_i": j + 1,
                "detail": "reversal candle formed but the next candle did not confirm — "
                          "momentum held"}
    return {**base, "state": "ready", "rejection": False, "momentum_shift": False,
            "trigger_i": i + C.MOMENTUM_WINDOW,
            "detail": "no reversal candle in the momentum window"}


def _confirm(sig, sess):
    """Optional confirmation step for SA1 / SA2 (VETO_MODE). Off by default.
    Causal: it only ever looks at candles that have already closed, and until they
    have it reports `pending`, exactly as SA4 does."""
    if sig is None or C.VETO_MODE == "off" or sig.get("state") != "ready":
        return sig
    if sig["sub_agent"] not in [a.strip() for a in C.VETO_AGENTS.split(",")]:
        return sig
    d, i = sig["direction"], sig["trigger_i"]
    if C.VETO_MODE == "sa4_style":
        res = _momentum_check(sess, d, i, {})
        keep = {k: res[k] for k in ("state", "trigger_i", "momentum_shift", "pending", "veto")
                if k in res}
        return {**sig, **keep, "detail": sig["detail"] + " — " + res["detail"]}
    if C.VETO_MODE == "next_candle":
        j = i + 1
        if j >= len(sess):
            return {**sig, "state": "pending", "pending": True, "trigger_i": None,
                    "detail": sig["detail"] + " — waiting one candle to confirm"}
        nxt = sess[j]
        against = (nxt["close"] < nxt["open"]) if d == 1 else (nxt["close"] > nxt["open"])
        if against:
            return {**sig, "state": "veto", "veto": True, "momentum_shift": True,
                    "trigger_i": j,
                    "detail": sig["detail"] + " — the next candle closed against the trade"}
        return {**sig, "trigger_i": j, "detail": sig["detail"] + " — the next candle held"}
    return sig


# ── router ──────────────────────────────────────────────────────────────────

def detect(sess, H, L, deadline_i, session_name, open_london):
    """The single signal for this session so far, or None.

    Order: SA3 (NY continuation) -> SA1 -> SA4 -> SA2.
    The first sub-agent with something to say takes the session; the rest stand
    down. That is the duplicate guard.
    """
    if session_name == "New York" and open_london:
        sig = SA3_ny_continuation(sess, H, L, deadline_i, open_london)
        if sig:
            return sig
    return (_confirm(SA1_second_candle_breakout(sess, H, L, deadline_i), sess)
            or SA4_breakout_momentum_check(sess, H, L, deadline_i)
            or _confirm(SA2_tap_rejection(sess, H, L, deadline_i), sess))
