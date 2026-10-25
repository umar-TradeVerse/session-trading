"""Backtest — replays history through the SAME detectors the live bot runs.

How it stays honest
-------------------
For every session the engine walks the candles one at a time. After candle k
closes it calls subagents.detect() with ONLY candles 0..k. The first time a
sub-agent reports "ready", the trade enters at the OPEN of candle k+1. Nothing
after k is ever passed to a detector, so no rule can see the future.

Trade management (stop, partial, breakeven, trail) then runs forward from the
entry candle. Settings are read from config at call time, so a parameter sweep
is just `run(candles, MOMENTUM_WINDOW=3)`.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from . import config as C
from . import sizing
from . import subagents as sa

IST = ZoneInfo(C.TZ)
STEP_MS = C.TIMEFRAME_MIN * 60 * 1000


# ── time helpers ────────────────────────────────────────────────────────────

def _at(day, t, ref=None):
    ts = dt.datetime.combine(day, t, tzinfo=IST)
    if ref is not None and ts <= ref:
        ts += dt.timedelta(days=1)
    return ts


def _ms(d):
    return int(d.timestamp() * 1000)


def session_windows(day):
    for name, s in C.SESSIONS.items():
        o = _at(day, s["open"])
        yield name, o, _at(day, s["deadline"], o), _at(day, s["end"], o)


# ── sizing and costs ────────────────────────────────────────────────────────

def leg_pnl(d, entry, exit_px, notional, hours, frac):
    gross = d * (exit_px - entry) / entry
    funding = C.FUNDING_RATE_8H * (hours / 8.0)
    net = gross - 2 * C.TAKER_FEE - funding
    return gross * frac, notional * net * frac


# ── trade management ────────────────────────────────────────────────────────

def simulate(sess, entry_i, d, entry, sl, entry_ms, end_ms, qty):
    """Existing SL / partial / breakeven / trail rules, run candle by candle.
    `qty` is the real, step-quantised BTC size; the partial is quantised too."""
    slip = C.SLIPPAGE
    risk_px = abs(entry - sl)
    liq = entry * (1 - d * (1 / C.LEVERAGE - C.MAINT_MARGIN))
    stop, stop_reason = sl, "stop_loss"
    if (d == 1 and liq > sl) or (d == -1 and liq < sl):
        stop, stop_reason = liq, "liquidation"

    tp = entry + d * C.TAKE_PROFIT_R * risk_px if C.TAKE_PROFIT_R else None
    notional = qty * entry * C.USDINR                      # INR, from the real quantity
    part_q = sizing.partial_qty(qty) if C.PARTIAL_TP_R else 0.0
    part_frac = part_q / qty if part_q else 0.0
    ptp = entry + d * C.PARTIAL_TP_R * risk_px if part_q else None

    legs, open_frac, partial_done = [], 1.0, False
    best, moved_be = entry, False
    exit_px = exit_ms = reason = None

    for j in range(entry_i, len(sess)):
        c = sess[j]
        t_c = c["t"]
        hrs = (t_c - entry_ms) / 3_600_000
        fav = c["high"] if d == 1 else c["low"]
        adv = c["low"] if d == 1 else c["high"]

        # 1. the standing stop, against this candle's worst price
        if (adv <= stop) if d == 1 else (adv >= stop):
            gap = (c["open"] < stop) if d == 1 else (c["open"] > stop)
            fill = c["open"] if gap else stop
            exit_px, exit_ms, reason = fill * (1 - slip * d), t_c, stop_reason
            break
        # 2. hard target (off by default)
        if tp is not None and ((c["high"] >= tp) if d == 1 else (c["low"] <= tp)):
            exit_px, exit_ms, reason = tp * (1 - slip * d), t_c, "take_profit"
            break
        # 3. partial
        if ptp is not None and not partial_done and \
                ((c["high"] >= ptp) if d == 1 else (c["low"] <= ptp)):
            f = min(part_frac, open_frac)
            legs.append((ptp * (1 - slip * d), f, hrs))
            open_frac -= f
            partial_done = True
        # 4. ratchet from the best price reached
        best = max(best, fav) if d == 1 else min(best, fav)
        r_now = d * (best - entry) / risk_px
        if C.BREAKEVEN_AT_R and not moved_be and r_now >= C.BREAKEVEN_AT_R:
            be = entry * (1 + d * C.BREAKEVEN_PLUS_PCT)
            if (d == 1 and be > stop) or (d == -1 and be < stop):
                stop, moved_be, stop_reason = be, True, "breakeven_stop"
        if C.TRAIL_AFTER_R and r_now >= C.TRAIL_AFTER_R:
            tr = best - d * C.TRAIL_STEP_R * risk_px
            if (d == 1 and tr > stop) or (d == -1 and tr < stop):
                stop, stop_reason = tr, "trailing_stop"
        # 5. the ratcheted stop, live inside the same candle
        if stop_reason in ("trailing_stop", "breakeven_stop"):
            if (c["close"] <= stop) if d == 1 else (c["close"] >= stop):
                exit_px, exit_ms, reason = stop * (1 - slip * d), t_c, stop_reason
                break
        # 6. forced timeout (optional — off by default)
        if C.MAX_HOLD_CANDLES and (j - entry_i + 1) >= C.MAX_HOLD_CANDLES:
            exit_px, exit_ms, reason = c["close"] * (1 - slip * d), t_c + STEP_MS, "max_hold_timeout"
            break
        # 7. session end
        if t_c + STEP_MS >= end_ms:
            exit_px, exit_ms, reason = c["close"] * (1 - slip * d), t_c + STEP_MS, "session_end"
            break

    if exit_px is None:
        c = sess[-1]
        exit_px, exit_ms, reason = c["close"] * (1 - slip * d), c["t"] + STEP_MS, "ran_out_of_data"

    hours = (exit_ms - entry_ms) / 3_600_000
    if open_frac > 1e-9:
        legs.append((exit_px, open_frac, hours))
    gross = inr = 0.0
    for px, frac, hh in legs:
        g, v = leg_pnl(d, entry, px, notional, hh, frac)
        gross += g
        inr += v
    inr = max(inr, -C.CAPITAL_INR)
    if reason == "liquidation":
        inr = -C.CAPITAL_INR
    risk_pct = risk_px / entry
    return dict(exit_ms=exit_ms, exit=exit_px, exit_reason=reason, legs=len(legs),
                notional_inr=round(notional), risk_inr=notional * risk_pct,
                mfe_R=d * (best - entry) / risk_px,
                candles_held=int((exit_ms - entry_ms) / STEP_MS),
                gross_inr=gross * notional, partial_hit=partial_done, qty_btc=qty,
                R=(d * (exit_px - entry) / entry) / risk_pct, pnl_inr=inr)


# ── the replay ──────────────────────────────────────────────────────────────

def run(candles, **overrides):
    """Replay `candles` (oldest first, closed only). Returns (trades, signals),
    both lists of dicts. Keyword arguments override config for this run only."""
    with C.override(**overrides):
        return _run(candles)


def _run(candles):
    if not candles:
        return [], []
    by_t = {c["t"]: i for i, c in enumerate(candles)}
    t0 = candles[0]["t"]
    t_end_data = candles[-1]["t"] + STEP_MS
    days = sorted({dt.datetime.fromtimestamp(c["t"] / 1000, IST).date() for c in candles})

    trades, signals = [], []
    equity = C.CAPITAL_INR
    day_pnl, consec, halt_through = {}, 0, None

    for day in days:
        if halt_through is not None and day > halt_through:
            halt_through, consec = None, 0

        for name, o, dl, e in session_windows(day):
            o_ms, dl_ms, e_ms = _ms(o), _ms(dl), _ms(e)
            if o_ms < t0 or e_ms > t_end_data or o_ms not in by_t:
                continue
            i0 = by_t[o_ms]
            rc = candles[i0]
            sess, j = [], i0 + 1
            while j < len(candles) and candles[j]["t"] < e_ms:
                sess.append(candles[j])
                j += 1
            if len(sess) < 3:
                continue
            H, L = rc["high"], rc["low"]
            log = {"date": day, "session": name, "range_high": H, "range_low": L,
                   "range_pct": (H - L) / L, "sub_agent": "-", "setup": "no setup",
                   "direction": "-", "breakout": False, "rejection": False,
                   "momentum_shift": False, "tp": "trail/partial"}

            def skip(reason, action="AVOID", sig=None):
                if sig:
                    log.update(sub_agent=sig["sub_agent"], setup=sig["setup"],
                               direction="LONG" if sig["direction"] == 1 else "SHORT",
                               breakout=sig["breakout"], rejection=sig["rejection"],
                               momentum_shift=sig["momentum_shift"])
                log.update(action=action, reason=reason)
                signals.append(log)

            if C.MAX_RANGE_POINTS and (H - L) > C.MAX_RANGE_POINTS:
                skip(f"Range candle wider than the MAX_RANGE_POINTS filter ({H - L:.1f} pts)")
                continue
            if halt_through is not None and day <= halt_through:
                skip("Halted after a losing streak"); continue
            if C.DAILY_LOSS_LIMIT_INR and day_pnl.get(day, 0) <= -C.DAILY_LOSS_LIMIT_INR:
                skip("Daily loss limit reached"); continue

            done = False
            vetoed_at = None        # candle index of the most recently logged veto, so a
                                     # persistent veto isn't re-logged on every later candle
            for k in range(len(sess)):
                prefix = sess[:k + 1]                      # <- the only data any rule sees
                di = sa.deadline_index(prefix, dl_ms)
                sig = sa.detect(prefix, H, L, di, name, None)
                if sig is None:
                    continue
                state = sig.get("state")
                if state == "pending":
                    continue
                if state == "veto":
                    if C.CONTINUE_AFTER_VETO:
                        if vetoed_at != sig.get("trigger_i"):
                            skip("Momentum reversed against the breakout — " + sig["detail"]
                                 + " (still scanning the rest of the session)", sig=sig)
                            vetoed_at = sig.get("trigger_i")
                        continue      # this attempt is dead; keep scanning for another setup
                    skip("Momentum reversed against the breakout — " + sig["detail"], sig=sig)
                    done = True
                    break
                # state == "ready"
                assert sig["trigger_i"] == k, "a signal must fire on its trigger candle"
                if sig["trigger_i"] > di:
                    skip("Trigger candle closed after the entry deadline", sig=sig)
                    done = True
                    break
                entry_i = k + 1
                if entry_i >= len(sess):
                    skip("No candle left to enter on", sig=sig)
                    done = True
                    break
                d = sig["direction"]
                level = sig["level"]
                entry = sess[entry_i]["open"] * (1 + C.SLIPPAGE * d)
                if C.SA1_SA4_RANGE_CANDLE_SL and sig["sub_agent"] in ("SA1", "SA4"):
                    sl = L if d == 1 else H
                else:
                    buf = C.SL_LEVEL_BUFFER_SA2 if sig["sub_agent"] == "SA2" else C.SL_LEVEL_BUFFER
                    sl = level * (1 - d * buf)
                if (d == 1 and entry <= sl) or (d == -1 and entry >= sl):
                    skip("Entry is already beyond its own stop", sig=sig)
                    done = True
                    break

                qty = sizing.qty_for(entry, sl)
                if qty <= 0:
                    skip("Size below the exchange minimum (qty / notional)", sig=sig)
                    done = True
                    break

                entry_ms = sess[entry_i]["t"]
                res = simulate(sess, entry_i, d, entry, sl, entry_ms, e_ms, qty)
                equity += res["pnl_inr"]
                day_pnl[day] = day_pnl.get(day, 0) + res["pnl_inr"]
                consec = consec + 1 if res["pnl_inr"] <= 0 else 0
                if C.MAX_CONSEC_LOSSES and consec >= C.MAX_CONSEC_LOSSES:
                    halt_through = day + dt.timedelta(days=C.HALT_DAYS)

                risk_pct = abs(entry - sl) / entry
                cost_in_R = (2 * C.TAKER_FEE + 2 * C.SLIPPAGE) / risk_pct
                skip(sig["detail"], action="ENTER", sig=sig)
                log.update(entry=round(entry, 2), sl=round(sl, 2),
                           pnl_inr=round(res["pnl_inr"], 2), exit_reason=res["exit_reason"])
                trades.append(dict(
                    date=day, weekday=day.strftime("%a"), session=name,
                    sub_agent=sig["sub_agent"], setup=sig["setup"],
                    side="LONG" if d == 1 else "SHORT",
                    range_high=H, range_low=L, range_pct=(H - L) / L, level=level,
                    breakout=sig["breakout"], rejection=sig["rejection"],
                    momentum_shift=sig["momentum_shift"],
                    trigger_time=dt.datetime.fromtimestamp(sess[k]["t"] / 1000, IST),
                    entry_time=dt.datetime.fromtimestamp(entry_ms / 1000, IST),
                    entry=entry, sl=sl, risk_pct=risk_pct, cost_in_R=cost_in_R,
                    exit_time=dt.datetime.fromtimestamp(res["exit_ms"] / 1000, IST),
                    equity_after=round(equity), reason=sig["detail"],
                    **{k2: v for k2, v in res.items() if k2 != "exit_ms"}))
                done = True
                break

            if not done:
                # Ended the session without a trade. Say why.
                last = sa.detect(sess, H, L, sa.deadline_index(sess, dl_ms), name, None)
                if last and last.get("state") == "pending":
                    skip("Still waiting on SA4 confirmation when the session ended", sig=last)
                else:
                    skip("No sub-agent condition met this session")
    return trades, signals


# ── experimental: range-size-banded router ──────────────────────────────────
# A separate entry point, not part of the default run()/detect() path, so the
# baseline is untouched. Routes each session to a sub-agent subset chosen by
# the range candle's own (H - L) in points, instead of the fixed SA3->SA1->SA4->SA2
# priority order:
#   100-200 pts  -> SA1 / SA4 only (follow the breakout)
#   200-500 pts  -> SA2 only (wait for a tap + rejection, not the breakout)
#   outside those bands -> skip, untested
# Also skips a session outright if price taps BOTH lines twice or more before
# any resolution — read as a sideways/chop session regardless of band.
# Reuses the same detectors, sizing and trade management as run().

def run_range_banded(candles, **overrides):
    with C.override(**overrides):
        return _run_range_banded(candles)


def _run_range_banded(candles):
    if not candles:
        return [], []
    by_t = {c["t"]: i for i, c in enumerate(candles)}
    t0 = candles[0]["t"]
    t_end_data = candles[-1]["t"] + STEP_MS
    days = sorted({dt.datetime.fromtimestamp(c["t"] / 1000, IST).date() for c in candles})

    trades, signals = [], []
    equity = C.CAPITAL_INR
    day_pnl, consec, halt_through = {}, 0, None

    def band_signal(band, prefix, H, L, di):
        if band == "SA1_SA4":
            return sa.SA1_second_candle_breakout(prefix, H, L, di) \
                   or sa.SA4_breakout_momentum_check(prefix, H, L, di)
        return sa.SA2_tap_rejection(prefix, H, L, di)

    for day in days:
        if halt_through is not None and day > halt_through:
            halt_through, consec = None, 0

        for name, o, dl, e in session_windows(day):
            o_ms, dl_ms, e_ms = _ms(o), _ms(dl), _ms(e)
            if o_ms < t0 or e_ms > t_end_data or o_ms not in by_t:
                continue
            i0 = by_t[o_ms]
            rc = candles[i0]
            sess, j = [], i0 + 1
            while j < len(candles) and candles[j]["t"] < e_ms:
                sess.append(candles[j])
                j += 1
            if len(sess) < 3:
                continue
            H, L = rc["high"], rc["low"]
            range_pts = H - L
            log = {"date": day, "session": name, "range_high": H, "range_low": L,
                   "range_pct": (H - L) / L, "range_points": round(range_pts, 1),
                   "sub_agent": "-", "setup": "no setup", "direction": "-",
                   "breakout": False, "rejection": False, "momentum_shift": False,
                   "tp": "trail/partial"}

            def skip(reason, action="AVOID", sig=None):
                if sig:
                    log.update(sub_agent=sig["sub_agent"], setup=sig["setup"],
                               direction="LONG" if sig["direction"] == 1 else "SHORT",
                               breakout=sig["breakout"], rejection=sig["rejection"],
                               momentum_shift=sig["momentum_shift"])
                log.update(action=action, reason=reason)
                signals.append(log)

            if 100 <= range_pts <= 200:
                band = "SA1_SA4"
            elif 200 < range_pts <= 500:
                band = "SA2"
            else:
                skip(f"Outside the tested range bands ({range_pts:.1f} pts)")
                continue

            if halt_through is not None and day <= halt_through:
                skip("Halted after a losing streak"); continue
            if C.DAILY_LOSS_LIMIT_INR and day_pnl.get(day, 0) <= -C.DAILY_LOSS_LIMIT_INR:
                skip("Daily loss limit reached"); continue

            di_full = sa.deadline_index(sess, dl_ms)
            done = False
            taps_h = taps_l = 0
            for k in range(len(sess)):
                prefix = sess[:k + 1]
                di = sa.deadline_index(prefix, dl_ms)
                if k <= di_full:
                    taps_h += sa.touches_level(sess[k], H)
                    taps_l += sa.touches_level(sess[k], L)
                    if taps_h >= 2 and taps_l >= 2:
                        skip(f"Sideways market — both lines tapped repeatedly "
                             f"(H x{taps_h}, L x{taps_l}) with no resolution")
                        done = True
                        break
                sig = band_signal(band, prefix, H, L, di)
                if sig is None:
                    continue
                state = sig.get("state")
                if state == "pending":
                    continue
                if state == "veto":
                    skip("Momentum reversed against the breakout — " + sig["detail"], sig=sig)
                    done = True
                    break
                assert sig["trigger_i"] == k, "a signal must fire on its trigger candle"
                if sig["trigger_i"] > di_full:
                    skip("Trigger candle closed after the entry deadline", sig=sig)
                    done = True
                    break
                entry_i = k + 1
                if entry_i >= len(sess):
                    skip("No candle left to enter on", sig=sig)
                    done = True
                    break
                d = sig["direction"]
                level = sig["level"]
                entry = sess[entry_i]["open"] * (1 + C.SLIPPAGE * d)
                buf = C.SL_LEVEL_BUFFER_SA2 if sig["sub_agent"] == "SA2" else C.SL_LEVEL_BUFFER
                sl = level * (1 - d * buf)
                if (d == 1 and entry <= sl) or (d == -1 and entry >= sl):
                    skip("Entry is already beyond its own stop", sig=sig)
                    done = True
                    break
                qty = sizing.qty_for(entry, sl)
                if qty <= 0:
                    skip("Size below the exchange minimum (qty / notional)", sig=sig)
                    done = True
                    break
                entry_ms = sess[entry_i]["t"]
                res = simulate(sess, entry_i, d, entry, sl, entry_ms, e_ms, qty)
                equity += res["pnl_inr"]
                day_pnl[day] = day_pnl.get(day, 0) + res["pnl_inr"]
                consec = consec + 1 if res["pnl_inr"] <= 0 else 0
                if C.MAX_CONSEC_LOSSES and consec >= C.MAX_CONSEC_LOSSES:
                    halt_through = day + dt.timedelta(days=C.HALT_DAYS)

                risk_pct = abs(entry - sl) / entry
                cost_in_R = (2 * C.TAKER_FEE + 2 * C.SLIPPAGE) / risk_pct
                skip(sig["detail"], action="ENTER", sig=sig)
                log.update(entry=round(entry, 2), sl=round(sl, 2),
                           pnl_inr=round(res["pnl_inr"], 2), exit_reason=res["exit_reason"])
                trades.append(dict(
                    date=day, weekday=day.strftime("%a"), session=name,
                    sub_agent=sig["sub_agent"], setup=sig["setup"],
                    side="LONG" if d == 1 else "SHORT",
                    range_high=H, range_low=L, range_pct=(H - L) / L,
                    range_points=round(range_pts, 1), level=level,
                    breakout=sig["breakout"], rejection=sig["rejection"],
                    momentum_shift=sig["momentum_shift"],
                    trigger_time=dt.datetime.fromtimestamp(sess[k]["t"] / 1000, IST),
                    entry_time=dt.datetime.fromtimestamp(entry_ms / 1000, IST),
                    entry=entry, sl=sl, risk_pct=risk_pct, cost_in_R=cost_in_R,
                    exit_time=dt.datetime.fromtimestamp(res["exit_ms"] / 1000, IST),
                    equity_after=round(equity), reason=sig["detail"],
                    **{k2: v for k2, v in res.items() if k2 != "exit_ms"}))
                done = True
                break

            if not done:
                last = band_signal(band, sess, H, L, di_full)
                if last and last.get("state") == "pending":
                    skip("Still waiting on SA4 confirmation when the session ended", sig=last)
                else:
                    skip("No sub-agent condition met this session")
    return trades, signals


# ── experimental: SA2 candle-pattern exit ───────────────────────────────────
# Built at the user's request: no R-based partial/breakeven/trail for SA2.
# Instead, reading candle-by-candle, causally, from the entry forward:
#   - stop = the rejection (trigger) candle's own low (LONG) / high (SHORT),
#     fixed for the life of the trade (not trailed).
#   - after at least one candle closes in the trade's favour, the first candle
#     that closes against it is "rejection #1" — 50% is booked at the NEXT
#     candle's open.
#   - watching the remaining 50%, from the candle after that:
#       * two consecutive candles each break the prior candle's low (LONG) /
#         high (SHORT) -> reversal confirmed, close the rest there.
#       * instead, a candle closes back past rejection #1's high (LONG) / low
#         (SHORT) -> the pullback failed to continue; hold, and look for a
#         second rejection candle (same definition) -> close the rest at the
#         NEXT candle's open once it forms.
#   - never flattened at session end: the position can run across session and
#     day boundaries until one of the above fires, or data runs out.
# SA1 / SA4 are untouched here (still simulate(), still one trade per session,
# still flattened at session end) — only SA2's management differs. While an
# SA2 trade from this engine is open, no new session (any sub-agent) can enter.

def manage_sa2_pattern(candles, entry_i, d, entry, sl0, qty):
    slip = C.SLIPPAGE
    notional = qty * entry * C.USDINR
    entry_ms = candles[entry_i]["t"]
    legs = []
    stop = sl0
    phase = "await_r1"          # await_r1 -> book_partial -> watch -> await_r2
    seen_favorable = False
    rejection1 = None
    consec_breaks = 0
    n = len(candles)
    part_q = sizing.partial_qty(qty)            # exchange-step-rounded, 0.0 if it can't split
    part_frac = part_q / qty if part_q else 0.0

    def hours(t):
        return (t - entry_ms) / 3_600_000

    j = entry_i + 1
    while j < n:
        c = candles[j]
        adv = c["low"] if d == 1 else c["high"]
        if (adv <= stop) if d == 1 else (adv >= stop):
            gap = (c["open"] < stop) if d == 1 else (c["open"] > stop)
            px = (c["open"] if gap else stop) * (1 - slip * d)
            open_frac = 1.0 - sum(f for _, f, _ in legs)
            legs.append((px, open_frac, hours(c["t"])))
            return _sa2_pattern_result(d, entry, notional, qty, legs, c["t"] + STEP_MS, "stop_loss")

        is_favorable = (c["close"] > c["open"]) if d == 1 else (c["close"] < c["open"])
        is_counter = (c["close"] < c["open"]) if d == 1 else (c["close"] > c["open"])

        if phase == "await_r1":
            if is_favorable:
                seen_favorable = True
            elif seen_favorable and is_counter:
                rejection1 = c
                phase = "book_partial"

        elif phase == "book_partial":
            if part_frac > 0:
                px = c["open"] * (1 - slip * d)
                legs.append((px, part_frac, hours(c["t"])))
            phase, consec_breaks = "watch", 0

        elif phase == "watch":
            prior = candles[j - 1]
            broke = (c["low"] < prior["low"]) if d == 1 else (c["high"] > prior["high"])
            consec_breaks = consec_breaks + 1 if broke else 0
            if consec_breaks >= 2:
                px = c["close"] * (1 - slip * d)
                open_frac = 1.0 - sum(f for _, f, _ in legs)
                legs.append((px, open_frac, hours(c["t"])))
                return _sa2_pattern_result(d, entry, notional, qty, legs, c["t"] + STEP_MS,
                                            "pattern_reversal_confirmed")
            recovered = (c["close"] > rejection1["high"]) if d == 1 else (c["close"] < rejection1["low"])
            if recovered:
                phase, seen_favorable = "await_r2", False

        elif phase == "await_r2":
            if is_favorable:
                seen_favorable = True
            elif seen_favorable and is_counter:
                open_frac = 1.0 - sum(f for _, f, _ in legs)
                if j + 1 < n:
                    nxt = candles[j + 1]
                    px = nxt["open"] * (1 - slip * d)
                    legs.append((px, open_frac, hours(nxt["t"])))
                    return _sa2_pattern_result(d, entry, notional, qty, legs, nxt["t"] + STEP_MS,
                                                "second_rejection_exit")
                px = c["close"] * (1 - slip * d)
                legs.append((px, open_frac, hours(c["t"])))
                return _sa2_pattern_result(d, entry, notional, qty, legs, c["t"] + STEP_MS,
                                            "second_rejection_exit")
        j += 1

    c = candles[-1]
    open_frac = 1.0 - sum(f for _, f, _ in legs)
    if open_frac > 1e-9:
        legs.append((c["close"] * (1 - slip * d), open_frac, hours(c["t"])))
    return _sa2_pattern_result(d, entry, notional, qty, legs, c["t"] + STEP_MS, "ran_out_of_data")


def _sa2_pattern_result(d, entry, notional, qty, legs, exit_ms, reason):
    gross = inr = 0.0
    for px, frac, hh in legs:
        g, v = leg_pnl(d, entry, px, notional, hh, frac)
        gross += g
        inr += v
    inr = max(inr, -C.CAPITAL_INR)
    return dict(exit_ms=exit_ms, exit=legs[-1][0], exit_reason=reason, legs=len(legs),
                notional_inr=round(notional), risk_inr=notional * abs(entry - legs[0][0]) / entry,
                mfe_R=0.0, candles_held=0, gross_inr=gross * notional, partial_hit=len(legs) > 1,
                qty_btc=qty, R=(d * (legs[-1][0] - entry) / entry), pnl_inr=inr)


def run_sa2_custom_exit(candles, **overrides):
    with C.override(**overrides):
        return _run_sa2_custom_exit(candles)


def _run_sa2_custom_exit(candles):
    if not candles:
        return [], []
    by_t = {c["t"]: i for i, c in enumerate(candles)}
    t0 = candles[0]["t"]
    t_end_data = candles[-1]["t"] + STEP_MS
    days = sorted({dt.datetime.fromtimestamp(c["t"] / 1000, IST).date() for c in candles})

    trades, signals = [], []
    equity = C.CAPITAL_INR
    day_pnl, consec, halt_through = {}, 0, None
    busy_until_ms = None        # an open SA2 trade blocks all sessions until this time

    for day in days:
        if halt_through is not None and day > halt_through:
            halt_through, consec = None, 0

        for name, o, dl, e in session_windows(day):
            o_ms, dl_ms, e_ms = _ms(o), _ms(dl), _ms(e)
            if o_ms < t0 or e_ms > t_end_data or o_ms not in by_t:
                continue
            if busy_until_ms is not None and o_ms < busy_until_ms:
                signals.append({"date": day, "session": name, "sub_agent": "-", "setup": "no setup",
                                 "direction": "-", "breakout": False, "rejection": False,
                                 "momentum_shift": False, "action": "AVOID",
                                 "reason": "A carried-over SA2 trade from an earlier session is still open"})
                continue
            busy_until_ms = None

            i0 = by_t[o_ms]
            rc = candles[i0]
            sess, j = [], i0 + 1
            while j < len(candles) and candles[j]["t"] < e_ms:
                sess.append(candles[j])
                j += 1
            if len(sess) < 3:
                continue
            H, L = rc["high"], rc["low"]
            log = {"date": day, "session": name, "range_high": H, "range_low": L,
                   "range_pct": (H - L) / L, "sub_agent": "-", "setup": "no setup",
                   "direction": "-", "breakout": False, "rejection": False,
                   "momentum_shift": False, "tp": "trail/partial"}

            def skip(reason, action="AVOID", sig=None):
                if sig:
                    log.update(sub_agent=sig["sub_agent"], setup=sig["setup"],
                               direction="LONG" if sig["direction"] == 1 else "SHORT",
                               breakout=sig["breakout"], rejection=sig["rejection"],
                               momentum_shift=sig["momentum_shift"])
                log.update(action=action, reason=reason)
                signals.append(log)

            if halt_through is not None and day <= halt_through:
                skip("Halted after a losing streak"); continue
            if C.DAILY_LOSS_LIMIT_INR and day_pnl.get(day, 0) <= -C.DAILY_LOSS_LIMIT_INR:
                skip("Daily loss limit reached"); continue

            done = False
            vetoed_at = None
            for k in range(len(sess)):
                prefix = sess[:k + 1]
                di = sa.deadline_index(prefix, dl_ms)
                sig = sa.detect(prefix, H, L, di, name, None)
                if sig is None:
                    continue
                state = sig.get("state")
                if state == "pending":
                    continue
                if state == "veto":
                    if C.CONTINUE_AFTER_VETO:
                        if vetoed_at != sig.get("trigger_i"):
                            skip("Momentum reversed against the breakout — " + sig["detail"]
                                 + " (still scanning the rest of the session)", sig=sig)
                            vetoed_at = sig.get("trigger_i")
                        continue
                    skip("Momentum reversed against the breakout — " + sig["detail"], sig=sig)
                    done = True
                    break
                assert sig["trigger_i"] == k, "a signal must fire on its trigger candle"
                if sig["trigger_i"] > di:
                    skip("Trigger candle closed after the entry deadline", sig=sig)
                    done = True
                    break
                entry_i = k + 1
                if entry_i >= len(sess):
                    skip("No candle left to enter on", sig=sig)
                    done = True
                    break
                d = sig["direction"]
                level = sig["level"]
                entry = sess[entry_i]["open"] * (1 + C.SLIPPAGE * d)

                if sig["sub_agent"] == "SA2":
                    trigger_c = sess[k]
                    sl = trigger_c["low"] if d == 1 else trigger_c["high"]
                elif sig["sub_agent"] == "SA5":
                    if sig.get("momentum_shift"):
                        reversal_c = sess[k - 1]        # the flip was confirmed BY this candle
                        level0 = reversal_c["low"] if d == 1 else reversal_c["high"]
                    else:
                        level0 = L if d == 1 else H
                    sl = level0 * (1 - d * C.SL_LEVEL_BUFFER) if C.SA5_USE_BUFFER_SL else level0
                else:
                    if C.SA1_SA4_RANGE_CANDLE_SL and sig["sub_agent"] in ("SA1", "SA4"):
                        sl = L if d == 1 else H
                    else:
                        buf = C.SL_LEVEL_BUFFER
                        sl = level * (1 - d * buf)

                if (d == 1 and entry <= sl) or (d == -1 and entry >= sl):
                    skip("Entry is already beyond its own stop", sig=sig)
                    done = True
                    break
                qty = sizing.qty_for(entry, sl)
                if qty <= 0:
                    skip("Size below the exchange minimum (qty / notional)", sig=sig)
                    done = True
                    break

                entry_ms = sess[entry_i]["t"]
                entry_global_i = by_t[entry_ms]

                if sig["sub_agent"] == "SA5":
                    skip(sig["detail"], action="ENTER", sig=sig)
                    leg_entry_i, leg_d, leg_entry, leg_sl, leg_qty = entry_i, d, entry, sl, qty
                    leg_num = 1
                    while True:
                        res, flip = _sa5_manage_leg(sess, leg_entry_i, leg_d, leg_entry,
                                                     leg_sl, leg_qty, e_ms)
                        equity += res["pnl_inr"]
                        day_pnl[day] = day_pnl.get(day, 0) + res["pnl_inr"]
                        consec = consec + 1 if res["pnl_inr"] <= 0 else 0
                        if C.MAX_CONSEC_LOSSES and consec >= C.MAX_CONSEC_LOSSES:
                            halt_through = day + dt.timedelta(days=C.HALT_DAYS)
                        leg_risk_pct = abs(leg_entry - leg_sl) / leg_entry
                        leg_cost_in_R = (2 * C.TAKER_FEE + 2 * C.SLIPPAGE) / leg_risk_pct
                        trades.append(dict(
                            date=day, weekday=day.strftime("%a"), session=name,
                            sub_agent="SA5", setup=f"{sig['setup']}, leg {leg_num}",
                            side="LONG" if leg_d == 1 else "SHORT",
                            range_high=H, range_low=L, range_pct=(H - L) / L,
                            level=(L if leg_d == 1 else H),
                            breakout=(leg_num == 1), rejection=(leg_num > 1),
                            momentum_shift=(leg_num > 1),
                            trigger_time=dt.datetime.fromtimestamp(
                                sess[max(leg_entry_i - 1, 0)]["t"] / 1000, IST),
                            entry_time=dt.datetime.fromtimestamp(sess[leg_entry_i]["t"] / 1000, IST),
                            entry=leg_entry, sl=leg_sl, risk_pct=leg_risk_pct,
                            cost_in_R=leg_cost_in_R,
                            exit_time=dt.datetime.fromtimestamp(res["exit_ms"] / 1000, IST),
                            equity_after=round(equity), reason=f"leg {leg_num}: {res['exit_reason']}",
                            **{k2: v for k2, v in res.items() if k2 != "exit_ms"}))
                        if flip is None or flip["entry_i"] >= len(sess):
                            break
                        leg_num += 1
                        leg_d = -leg_d
                        leg_entry_i, leg_entry, leg_sl, leg_qty = (
                            flip["entry_i"], flip["entry"], flip["sl"], flip["qty"])
                    done = True
                    break

                if sig["sub_agent"] == "SA2":
                    res = manage_sa2_pattern(candles, entry_global_i, d, entry, sl, qty)
                    busy_until_ms = res["exit_ms"]
                else:
                    res = simulate(sess, entry_i, d, entry, sl, entry_ms, e_ms, qty)

                equity += res["pnl_inr"]
                day_pnl[day] = day_pnl.get(day, 0) + res["pnl_inr"]
                consec = consec + 1 if res["pnl_inr"] <= 0 else 0
                if C.MAX_CONSEC_LOSSES and consec >= C.MAX_CONSEC_LOSSES:
                    halt_through = day + dt.timedelta(days=C.HALT_DAYS)

                risk_pct = abs(entry - sl) / entry
                cost_in_R = (2 * C.TAKER_FEE + 2 * C.SLIPPAGE) / risk_pct
                skip(sig["detail"], action="ENTER", sig=sig)
                log.update(entry=round(entry, 2), sl=round(sl, 2),
                           pnl_inr=round(res["pnl_inr"], 2), exit_reason=res["exit_reason"])
                trades.append(dict(
                    date=day, weekday=day.strftime("%a"), session=name,
                    sub_agent=sig["sub_agent"], setup=sig["setup"],
                    side="LONG" if d == 1 else "SHORT",
                    range_high=H, range_low=L, range_pct=(H - L) / L, level=level,
                    breakout=sig["breakout"], rejection=sig["rejection"],
                    momentum_shift=sig["momentum_shift"],
                    trigger_time=dt.datetime.fromtimestamp(sess[k]["t"] / 1000, IST),
                    entry_time=dt.datetime.fromtimestamp(entry_ms / 1000, IST),
                    entry=entry, sl=sl, risk_pct=risk_pct, cost_in_R=cost_in_R,
                    exit_time=dt.datetime.fromtimestamp(res["exit_ms"] / 1000, IST),
                    equity_after=round(equity), reason=sig["detail"],
                    **{k2: v for k2, v in res.items() if k2 != "exit_ms"}))
                done = True
                break

            if not done:
                last = sa.detect(sess, H, L, sa.deadline_index(sess, dl_ms), name, None)
                if last and last.get("state") == "pending":
                    skip("Still waiting on SA4 confirmation when the session ended", sig=last)
                else:
                    skip("No sub-agent condition met this session")
    return trades, signals


# ── self-audit ──────────────────────────────────────────────────────────────

def audit_causality(candles, trades, sample=300, seed=0, **overrides):
    """End-to-end test that no decision depends on the future.

    For each sampled trade, every candle AFTER its entry candle (to the end of
    that session) is rescaled by a random factor, and the whole engine is run
    again. The entry decision — sub-agent, direction, trigger candle and stop —
    must be identical, because at the moment of entry none of those candles
    existed. Only the trade's outcome is allowed to change.

    Pass the same keyword overrides you used for the run being audited.
    Returns (checked, mismatches)."""
    import random
    rnd = random.Random(seed)
    by_t = {c["t"]: i for i, c in enumerate(candles)}
    pick = trades if len(trades) <= sample else rnd.sample(trades, sample)

    scrambled = [dict(c) for c in candles]
    for t in pick:
        e_ms = next(_ms(e) for n, _, _, e in session_windows(t["date"]) if n == t["session"])
        entry_i = by_t[_ms(t["entry_time"])]
        j = entry_i + 1                      # keep the entry candle: it is the fill
        while j < len(scrambled) and scrambled[j]["t"] < e_ms:
            f = rnd.uniform(0.9, 1.1)
            for key in ("open", "high", "low", "close"):
                scrambled[j][key] = candles[j][key] * f
            j += 1

    # breakers off: scrambling changes P&L, which could otherwise suppress later trades
    runner = overrides.pop("runner", run)
    new_trades, _ = runner(scrambled, **{**overrides, "DAILY_LOSS_LIMIT_INR": 0, "MAX_CONSEC_LOSSES": 0})
    seen = {(x["date"], x["session"]): x for x in new_trades}
    bad = []
    for t in pick:
        x = seen.get((t["date"], t["session"]))
        if (x is None or x["sub_agent"] != t["sub_agent"] or x["side"] != t["side"]
                or x["trigger_time"] != t["trigger_time"]
                or abs(x["sl"] - t["sl"]) > 1e-6 or abs(x["entry"] - t["entry"]) > 1e-6):
            bad.append((t["date"], t["session"], t["sub_agent"]))
    return len(pick), bad


# ── experimental: SA5 — breakout, ride, flip on confirmed reversal ──────────
# A new sub-agent, built at the user's request, tested here as the ONLY agent
# in the router so its standalone edge reads cleanly, separate from SA1-4.
# Same entry trigger as SA4 (clean breakout + 2-candle momentum check), except:
#   - where SA4 would veto on a confirmed reversal before entry, SA5 enters
#     the FLIPPED direction immediately instead of standing down.
#   - once in a trade, it keeps watching the WHOLE session (not just the
#     initial 2 candles) for a confirmed reversal -- a doji/long-wick candle
#     against the position, followed by a candle closing through it -- and
#     flips to the opposite direction every time one appears. No cap on flips.
# Each leg's stop = the signal candle's own extreme (the range candle for the
# first leg, the reversal candle itself for every flip after that).
# Session-bound: flattens at session end like SA1/SA4 -- no cross-session carry.

def _sa5_manage_leg(sess, entry_i, d, entry, sl, qty, end_ms):
    """One SA5 leg: fixed stop vs. a confirmed reversal, which ends this leg and
    hands back what's needed to open the flipped leg on the next candle's open.
    Returns (result_dict, flip_info_or_None)."""
    slip = C.SLIPPAGE
    notional = qty * entry * C.USDINR
    entry_ms = sess[entry_i]["t"]
    n = len(sess)

    def result(exit_ms, px, reason, j):
        hours = (exit_ms - entry_ms) / 3_600_000
        g, v = leg_pnl(d, entry, px, notional, hours, 1.0)
        return dict(exit_ms=exit_ms, exit=px, exit_reason=reason, legs=1,
                    notional_inr=round(notional), risk_inr=notional * abs(entry - sl) / entry,
                    mfe_R=0.0, candles_held=max(j - entry_i, 0), gross_inr=g * notional,
                    partial_hit=False, qty_btc=qty, R=d * (px - entry) / entry, pnl_inr=v)

    j = entry_i
    while j < n:
        c = sess[j]
        adv = c["low"] if d == 1 else c["high"]
        if (adv <= sl) if d == 1 else (adv >= sl):
            gap = (c["open"] < sl) if d == 1 else (c["open"] > sl)
            px = (c["open"] if gap else sl) * (1 - slip * d)
            return result(c["t"] + STEP_MS, px, "stop_loss", j), None

        if j > entry_i and sa.reversal_candle(c, d) and j + 1 < n:
            nxt = sess[j + 1]
            flipped = ((d == -1 and nxt["close"] > c["high"]) or
                       (d == 1 and nxt["close"] < c["low"]))
            if flipped:
                px = nxt["open"] * (1 - slip * d)
                res = result(nxt["t"], px, "reversal_flip", j)
                new_d = -d
                new_entry_i = j + 2
                if new_entry_i >= n:
                    return res, None
                new_entry = sess[new_entry_i]["open"] * (1 + slip * new_d)
                new_level = c["low"] if new_d == 1 else c["high"]
                new_sl = new_level * (1 - new_d * C.SL_LEVEL_BUFFER) if C.SA5_USE_BUFFER_SL else new_level
                if (new_d == 1 and new_entry <= new_sl) or (new_d == -1 and new_entry >= new_sl):
                    return res, None
                new_qty = sizing.qty_for(new_entry, new_sl)
                if new_qty <= 0:
                    return res, None
                return res, {"entry_i": new_entry_i, "entry": new_entry, "sl": new_sl, "qty": new_qty}

        if c["t"] + STEP_MS >= end_ms:
            px = c["close"] * (1 - slip * d)
            return result(c["t"] + STEP_MS, px, "session_end", j), None
        j += 1

    c = sess[-1]
    return result(c["t"] + STEP_MS, c["close"] * (1 - slip * d), "ran_out_of_data", n - 1), None


def run_sa5_breakout_flip(candles, **overrides):
    with C.override(**overrides):
        return _run_sa5_breakout_flip(candles)


def _run_sa5_breakout_flip(candles):
    if not candles:
        return [], []
    by_t = {c["t"]: i for i, c in enumerate(candles)}
    t0 = candles[0]["t"]
    t_end_data = candles[-1]["t"] + STEP_MS
    days = sorted({dt.datetime.fromtimestamp(c["t"] / 1000, IST).date() for c in candles})

    trades, signals = [], []
    equity = C.CAPITAL_INR
    day_pnl, consec, halt_through = {}, 0, None

    for day in days:
        if halt_through is not None and day > halt_through:
            halt_through, consec = None, 0

        for name, o, dl, e in session_windows(day):
            o_ms, dl_ms, e_ms = _ms(o), _ms(dl), _ms(e)
            if o_ms < t0 or e_ms > t_end_data or o_ms not in by_t:
                continue
            i0 = by_t[o_ms]
            rc = candles[i0]
            sess, j = [], i0 + 1
            while j < len(candles) and candles[j]["t"] < e_ms:
                sess.append(candles[j])
                j += 1
            if len(sess) < 3:
                continue
            H, L = rc["high"], rc["low"]
            di = sa.deadline_index(sess, dl_ms)
            log = {"date": day, "session": name, "range_high": H, "range_low": L,
                   "range_pct": (H - L) / L, "sub_agent": "-", "setup": "no setup",
                   "direction": "-", "breakout": False, "rejection": False,
                   "momentum_shift": False, "tp": "flip"}

            def skip(reason):
                log.update(action="AVOID", reason=reason)
                signals.append(dict(log))

            if halt_through is not None and day <= halt_through:
                skip("Halted after a losing streak"); continue
            if C.DAILY_LOSS_LIMIT_INR and day_pnl.get(day, 0) <= -C.DAILY_LOSS_LIMIT_INR:
                skip("Daily loss limit reached"); continue

            entry_i0, d0 = None, None
            for i in range(min(len(sess), di + 1)):
                c = sess[i]
                dd = sa.closes_beyond(c, H, L)
                if dd == 0:
                    continue
                if not sa.clear_of_level(c, dd, H, L):
                    if C.SA4_SCAN_PAST_IMPURE_BREAKOUT:
                        continue
                    entry_i0 = None
                    break
                entry_i0, d0 = i, dd
                break
            if entry_i0 is None:
                skip("No clean breakout this session")
                continue

            direction, trigger_i, sl0_from, sl0_candle = d0, None, "range", None
            abort = False
            for jj in range(entry_i0 + 1, entry_i0 + 1 + C.MOMENTUM_WINDOW):
                if jj >= len(sess):
                    abort = True
                    break
                cj = sess[jj]
                if not sa.reversal_candle(cj, direction):
                    continue
                if jj + 1 >= len(sess):
                    abort = True
                    break
                nxt = sess[jj + 1]
                flipped = ((direction == -1 and nxt["close"] > cj["high"]) or
                           (direction == 1 and nxt["close"] < cj["low"]))
                if flipped:
                    direction = -direction
                    sl0_from, sl0_candle = "reversal", cj
                trigger_i = jj + 1
                break
            if abort:
                skip("Momentum window never resolved before data ran out")
                continue
            if trigger_i is None:
                trigger_i = min(entry_i0 + C.MOMENTUM_WINDOW, len(sess) - 1)
            if trigger_i > di:
                skip("Signal completed after the entry deadline")
                continue
            entry_i = trigger_i + 1
            if entry_i >= len(sess):
                skip("No candle left to enter on")
                continue

            d = direction
            entry = sess[entry_i]["open"] * (1 + C.SLIPPAGE * d)
            level0 = (L if d == 1 else H) if sl0_from == "range" else \
                     (sl0_candle["low"] if d == 1 else sl0_candle["high"])
            sl = level0 * (1 - d * C.SL_LEVEL_BUFFER) if C.SA5_USE_BUFFER_SL else level0
            if (d == 1 and entry <= sl) or (d == -1 and entry >= sl):
                skip("Entry is already beyond its own stop")
                continue
            qty = sizing.qty_for(entry, sl)
            if qty <= 0:
                skip("Size below the exchange minimum (qty / notional)")
                continue

            leg_entry_i, leg_d, leg_entry, leg_sl, leg_qty = entry_i, d, entry, sl, qty
            leg_num = 1
            while True:
                res, flip = _sa5_manage_leg(sess, leg_entry_i, leg_d, leg_entry, leg_sl, leg_qty, e_ms)
                equity += res["pnl_inr"]
                day_pnl[day] = day_pnl.get(day, 0) + res["pnl_inr"]
                consec = consec + 1 if res["pnl_inr"] <= 0 else 0
                if C.MAX_CONSEC_LOSSES and consec >= C.MAX_CONSEC_LOSSES:
                    halt_through = day + dt.timedelta(days=C.HALT_DAYS)
                risk_pct = abs(leg_entry - leg_sl) / leg_entry
                cost_in_R = (2 * C.TAKER_FEE + 2 * C.SLIPPAGE) / risk_pct
                if leg_num == 1:
                    log.update(sub_agent="SA5", setup="Breakout + flip-on-reversal",
                               direction="LONG" if leg_d == 1 else "SHORT",
                               breakout=True, rejection=False, momentum_shift=False,
                               action="ENTER", reason="clean breakout, momentum confirmed")
                    signals.append(dict(log))
                trades.append(dict(
                    date=day, weekday=day.strftime("%a"), session=name,
                    sub_agent="SA5", setup=f"Breakout + flip-on-reversal, leg {leg_num}",
                    side="LONG" if leg_d == 1 else "SHORT",
                    range_high=H, range_low=L, range_pct=(H - L) / L,
                    level=(L if leg_d == 1 else H),
                    breakout=(leg_num == 1), rejection=(leg_num > 1), momentum_shift=(leg_num > 1),
                    trigger_time=dt.datetime.fromtimestamp(sess[max(leg_entry_i - 1, 0)]["t"] / 1000, IST),
                    entry_time=dt.datetime.fromtimestamp(sess[leg_entry_i]["t"] / 1000, IST),
                    entry=leg_entry, sl=leg_sl, risk_pct=risk_pct, cost_in_R=cost_in_R,
                    exit_time=dt.datetime.fromtimestamp(res["exit_ms"] / 1000, IST),
                    equity_after=round(equity), reason=f"leg {leg_num}: {res['exit_reason']}",
                    **{k2: v for k2, v in res.items() if k2 != "exit_ms"}))
                if flip is None or flip["entry_i"] >= len(sess):
                    break
                leg_num += 1
                leg_d = -leg_d
                leg_entry_i, leg_entry, leg_sl, leg_qty = (
                    flip["entry_i"], flip["entry"], flip["sl"], flip["qty"])
    return trades, signals


if __name__ == "__main__":
    # python -m sorb15.backtest --start 2026-01-01
    import argparse

    from . import data, report
    ap = argparse.ArgumentParser(description="Backtest the four sub-agents on CoinDCX history")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    cs, src = data.fetch_history(args.start, args.end)
    data.quality(cs)
    tr, sg = run(cs)
    t, s = report.frames(tr, sg)
    print()
    print(report.paste_summary(t, s, label=f"{args.start} -> {args.end or 'now'}  ({src})"))
