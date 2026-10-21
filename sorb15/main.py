"""SORB-15 — four sub-agents, live on CoinDCX futures.

Runs 24x7 on Railway and sleeps outside the London / New York windows.

Each poll:
  1. If a trade is open, manage it: stop, partial, breakeven, trail, session end.
  2. Otherwise, inside a session, ask the detectors for a signal and enter.
  3. Outside a session, sleep until the next one opens.

DRY_RUN=true runs the whole thing as a PAPER account: the same signals, the
same entries, stops, partials and trailing, with P&L computed from real
candles — but no order is ever sent to the exchange.

Detection uses CLOSED candles only (exactly what the backtest sees). Trade
management also reads the still-forming candle, so the trail follows price
inside a candle the way the backtest assumes.
"""
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from . import coindcx as ex
from . import config as C
from . import notify
from . import sizing
from . import subagents as sa

logging.basicConfig(level=getattr(logging, C.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sorb15")
IST = ZoneInfo(C.TZ)
STEP_MS = C.TIMEFRAME_MIN * 60 * 1000


# ── time ────────────────────────────────────────────────────────────────────

def now_ist():
    return dt.datetime.now(IST)


def now_ms():
    return int(now_ist().timestamp() * 1000)


def ms(d):
    return int(d.timestamp() * 1000)


def at(day, t, ref=None):
    ts = dt.datetime.combine(day, t, tzinfo=IST)
    if ref is not None and ts <= ref:
        ts += dt.timedelta(days=1)
    return ts


def window(name, now=None):
    """(open, deadline, end) of the session instance containing `now`, or the next."""
    now = now or now_ist()
    s = C.SESSIONS[name]
    for day in (now.date() - dt.timedelta(days=1), now.date(), now.date() + dt.timedelta(days=1)):
        o = at(day, s["open"])
        e = at(day, s["end"], o)
        if o <= now < e:
            return o, at(day, s["deadline"], o), e
    day = now.date()
    o = at(day, s["open"])
    if o < now:
        o = at(day + dt.timedelta(days=1), s["open"])
    return o, at(o.date(), s["deadline"], o), at(o.date(), s["end"], o)


def active_session(now=None):
    now = now or now_ist()
    for name in C.SESSIONS:
        o, d, e = window(name, now)
        if o <= now < e:
            return name, o, d, e
    return None, None, None, None


def seconds_to_next_open(now=None):
    now = now or now_ist()
    opens = [o for o, _, _ in (window(n, now) for n in C.SESSIONS) if o > now]
    return max(5, int((min(opens) - now).total_seconds())) if opens else 60


# ── candles ─────────────────────────────────────────────────────────────────

def fetch_closed(open_dt):
    mins = int((now_ist() - open_dt).total_seconds() // 60) + 120
    return ex.candles(minutes_back=max(mins, 180))


def split_session(cs, open_dt, end_dt):
    o_ms, e_ms = ms(open_dt), ms(end_dt)
    rc = next((c for c in cs if c["t"] == o_ms), None)
    return rc, [c for c in cs if o_ms < c["t"] < e_ms]


# ── sizing and P&L ──────────────────────────────────────────────────────────

def contracts(entry, sl):
    """BTC quantity so a stop-out loses at most RISK_PER_TRADE_INR, floored to the
    exchange's step. 0.0 means the exchange would refuse the order."""
    return sizing.qty_for(entry, sl)


def sync_spec():
    """Read the real contract spec from the exchange and use it. The exchange is
    the source of truth for step sizes and minimums; typing them in by hand is how
    a quantity like 0.0047 ends up rejected against a 0.001 step."""
    try:
        spec = ex.instrument()
    except Exception as err:
        log.warning("could not read the contract spec (%s) — using configured values", err)
        return {}
    wanted = [("quantity_increment", "QTY_STEP"), ("price_increment", "PRICE_STEP"),
              ("min_trade_size", "MIN_QTY"), ("min_quantity", "MIN_QTY"),
              ("min_notional", "MIN_NOTIONAL_USDT")]
    changed = {}
    for key, attr in wanted:
        v = spec.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0 and abs(getattr(C, attr) - v) > 1e-12:
            if attr == "MIN_QTY" and attr in changed:
                v = max(v, changed[attr])
            changed[attr] = v
            setattr(C, attr, v)
    if changed:
        notify.send("ℹ️ Contract spec read from CoinDCX and applied: "
                    + ", ".join(f"{k}={v}" for k, v in changed.items()), silent=True)
    log.info("contract spec: step=%s min_qty=%s price_step=%s min_notional=%s",
             C.QTY_STEP, C.MIN_QTY, C.PRICE_STEP, C.MIN_NOTIONAL_USDT)
    return changed


def leg_pnl(trade, px, qty, hours=0.0):
    """Net rupee P&L of closing `qty` at `px`, after taker fees on both legs and
    funding for the hours held — same formula as backtest.leg_pnl (funding is a
    rate against the entry-based notional, not the exit price), kept in sync so
    live paper P&L matches what the backtest validated."""
    d = trade["direction"]
    gross = d * (px - trade["entry"]) * qty * C.USDINR
    fees = (trade["entry"] * qty + px * qty) * C.USDINR * C.TAKER_FEE
    funding = C.FUNDING_RATE_8H * (hours / 8.0) * trade["entry"] * qty * C.USDINR
    return gross - fees - funding


# ── exchange helpers (live only; all no-ops under DRY_RUN) ───────────────────

def wait_for_position(tries=10):
    """After an entry order, read the position back from the exchange."""
    for _ in range(tries):
        live = ex.open_position()
        if live:
            return live
        time.sleep(1)
    return None


def flatten(trade_or_pid, why, session, sub_agent=""):
    """Emergency exit. Used whenever a position cannot be protected."""
    pid = trade_or_pid.get("position_id") if isinstance(trade_or_pid, dict) else trade_or_pid
    try:
        ex.exit_position(pid)
        outcome = "position closed at market"
    except Exception as err:
        outcome = f"CLOSE FAILED ({err}) — CLOSE IT MANUALLY NOW"
    notify.send(f"🚨 <b>{why}</b> {sub_agent} {session}\n<pre>{outcome}</pre>")


def place_stop(position_id, price):
    """Set the stop and confirm the exchange took it. Raises if it cannot be confirmed."""
    ex.set_stop(position_id, price)
    if C.DRY_RUN:
        return
    chk = ex.open_position()
    raw = (chk or {}).get("raw", {})
    if "stop_loss_trigger" in raw and float(raw.get("stop_loss_trigger") or 0) <= 0:
        time.sleep(1.5)                      # give the exchange a moment, then look again
        raw = (ex.open_position() or {}).get("raw", {})
        if float(raw.get("stop_loss_trigger") or 0) <= 0:
            raise RuntimeError("the exchange does not show a stop on the position")


# ── entry ───────────────────────────────────────────────────────────────────

def enter(sig, session, rc, sess, state):
    d, level = sig["direction"], sig["level"]
    H, L = rc["high"], rc["low"]

    # The backtest enters at the open of the candle after the trigger. Live can
    # only match that if it acts right after the close; if the bot was late
    # (restart, outage) the price has moved and the trade is not the one we tested.
    trigger = sess[sig["trigger_i"]]
    lag_s = (now_ms() - (trigger["t"] + STEP_MS)) / 1000
    entry_px = sess[-1]["close"]
    if sig["sub_agent"] == "SA2":
        sl = trigger["low"] if d == 1 else trigger["high"]            # the rejection candle's own extreme
    elif sig["sub_agent"] in ("SA1", "SA4"):
        sl = L if d == 1 else H                                       # the range candle's own extreme
    else:
        sl = level * (1 - d * C.SL_LEVEL_BUFFER)

    def avoid(why):
        notify.send(notify.signal_block(sig, session, H, L, round(entry_px, 2), round(sl, 2),
                                        "AVOID", why))
        return None

    if lag_s > C.MAX_ENTRY_LAG_SECONDS:
        return avoid(f"Missed the entry window — signal completed {lag_s:.0f}s ago, "
                     f"limit {C.MAX_ENTRY_LAG_SECONDS}s")
    if (d == 1 and entry_px <= sl) or (d == -1 and entry_px >= sl):
        return avoid("Entry is already beyond its own stop")
    qty = contracts(entry_px, sl)
    if qty <= 0:
        return avoid(f"Size is below the exchange minimum (min qty {C.MIN_QTY}, "
                     f"min notional {C.MIN_NOTIONAL_USDT:g} USDT, step {C.QTY_STEP})")

    cid = f"sorb15-{sig['sub_agent']}-{int(time.time())}"
    live = None
    try:
        ex.market_order("buy" if d == 1 else "sell", qty)
    except Exception as err:
        # The request may have reached the exchange even though the reply was lost.
        # Never assume it failed: look at the real position before deciding.
        time.sleep(2)
        live = None if C.DRY_RUN else ex.open_position()
        if not live:
            return avoid(f"Entry order failed: {err}")
        notify.send(f"⚠️ Entry call errored ({err}) but the position exists. Protecting it.")

    pid, fill = None, entry_px
    if not C.DRY_RUN:
        live = live or wait_for_position()
        if not live:
            return avoid("Entry order sent but no position appeared on the exchange")
        pid, fill = live["id"], (live["entry"] or entry_px)
        if (d == 1 and fill <= sl) or (d == -1 and fill >= sl):
            flatten(pid, "FILL BEYOND STOP — closed", session, sig["sub_agent"])
            return None
    try:
        place_stop(pid, sl)
    except Exception as err:
        flatten(pid, f"STOP NOT CONFIRMED ({err}) — closed", session, sig["sub_agent"])
        return None
    entry_px = fill

    entry_bar = (now_ms() // STEP_MS) * STEP_MS
    trade = {"sub_agent": sig["sub_agent"], "setup": sig["setup"], "session": session,
             "direction": d, "level": level, "entry": entry_px, "sl": sl, "qty": qty,
             "open_qty": qty, "risk_px": abs(entry_px - sl), "best": entry_px,
             "partial_done": False, "moved_be": False, "realised": 0.0, "seen": {},
             "missing_polls": 0, "position_id": pid,
             "entry_bar": entry_bar, "opened_at": int(time.time()),
             "client_id": cid, "range_high": H, "range_low": L,
             # SA2 candle-pattern exit state (unused by SA1/SA3/SA4)
             "phase": "await_r1", "seen_favorable": False, "rejection1": None,
             "consec_breaks": 0, "pattern_last_t": entry_bar}
    notify.send(notify.signal_block(sig, session, H, L, round(entry_px, 2), round(sl, 2), "ENTER",
                                    sig.get("detail", ""),
                                    result=f"qty {qty}  risk Rs {C.RISK_PER_TRADE_INR:.0f}"))
    log.info("ENTER %s %s qty=%s entry=%.2f sl=%.2f", sig["sub_agent"], d, qty, entry_px, sl)
    return trade


# ── management ──────────────────────────────────────────────────────────────

def stop_was_hit(trade, since):
    """PAPER mode: did price trade through the stop since the last poll?

    Only extremes that are NEW since the previous poll count, so a low that
    printed before the stop was raised cannot stop us out of a trailed trade."""
    d, sl = trade["direction"], trade["sl"]
    seen = trade["seen"]
    hit = False
    for c in since[-3:]:
        key = str(c["t"])
        ext = c["low"] if d == 1 else c["high"]
        prev = seen.get(key)
        fresh = prev is None or (ext < prev if d == 1 else ext > prev)
        if fresh and ((ext <= sl) if d == 1 else (ext >= sl)):
            hit = True
        seen[key] = ext if prev is None else (min(prev, ext) if d == 1 else max(prev, ext))
    for k in list(seen)[:-6]:
        seen.pop(k)
    return hit


def move_stop(trade, new_sl, why, session):
    """Move the position's stop. If the new stop cannot be placed, put the old one
    back; if that also fails, close the position rather than leave it naked."""
    old, pid = trade["sl"], trade.get("position_id")
    try:
        place_stop(pid, new_sl)
    except Exception as err:
        log.warning("stop move failed: %s", err)
        try:
            place_stop(pid, old)
            notify.send(f"⚠️ Could not move the stop ({err}). Old stop restored at {old:,.2f}.",
                        silent=True)
        except Exception as err2:
            flatten(pid, f"STOP LOST ({err2}) — closed", session, trade["sub_agent"])
        return
    trade["sl"] = new_sl
    if abs(new_sl - old) > 1e-9:
        notify.send(f"<b>STOP MOVED</b> {trade['sub_agent']} {session}\n"
                    f"<pre>{why}: {old:,.2f} -> {new_sl:,.2f}</pre>", silent=True)
    log.info("stop moved (%s) %.2f -> %.2f", why, old, new_sl)


def finish(trade, exit_px, pnl, reason, session, state, day_delta=None):
    # `pnl` is the trade's FULL realised total (it may include an earlier partial
    # already added to day_pnl when it was booked) — day_pnl must only move by
    # what's NEW at this step, or a partial gets counted twice.
    state["day_pnl"] += pnl if day_delta is None else day_delta
    state["consec_losses"] = state["consec_losses"] + 1 if pnl <= 0 else 0
    if C.MAX_CONSEC_LOSSES and state["consec_losses"] >= C.MAX_CONSEC_LOSSES:
        state["halted"] = True
        state["halt_through"] = str(now_ist().date() + dt.timedelta(days=C.HALT_DAYS))
        notify.send(f"⚠️ <b>HALTED</b> — {state['consec_losses']} losses in a row. "
                    f"No new trades through {state['halt_through']} (IST).")
    tag = "  (paper)" if C.DRY_RUN else ""
    notify.send(f"<b>CLOSED</b> {trade['sub_agent']} {session}{tag}\n"
                f"<pre>reason   : {reason}\n"
                f"side     : {'LONG' if trade['direction'] == 1 else 'SHORT'}\n"
                f"entry    : {trade['entry']:,.2f}\n"
                f"exit     : {exit_px:,.2f}\n"
                f"P&amp;L      : Rs {pnl:,.2f}  (after fees)\n"
                f"day P&amp;L  : Rs {state['day_pnl']:,.2f}</pre>")
    log.info("CLOSED %s %s pnl=%.2f", trade["sub_agent"], reason, pnl)


def end_trade(trade, exit_px, reason, session, state, place_order=False):
    qty = trade["open_qty"]
    if place_order and qty > 0:
        pid = trade.get("position_id")
        try:
            ex.exit_position(pid)
        except Exception as err:
            notify.send(f"🚨 <b>CLOSE FAILED</b> {trade['sub_agent']} {session}\n"
                        f"<pre>{err}\nCLOSE IT MANUALLY NOW</pre>")
        try:
            ex.clear_position_orders(pid)
        except Exception:
            pass
    hours = (now_ms() - trade["entry_bar"]) / 3_600_000
    final_leg = leg_pnl(trade, exit_px, qty, hours)
    pnl = trade["realised"] + final_leg
    trade["open_qty"] = 0
    finish(trade, exit_px, pnl, reason, session, state, day_delta=final_leg)
    return None


def manage(trade, session, end_dt, state):
    """Stop, partial, breakeven, trail and session end for the open trade.
    Returns the updated trade, or None once it is closed."""
    d, slip = trade["direction"], C.SLIPPAGE
    mins = int((now_ms() - trade["entry_bar"]) / 60000) + 30
    cs = ex.candles(minutes_back=max(mins, 60), include_forming=True)
    since = [c for c in cs if c["t"] >= trade["entry_bar"]]
    if not since:
        return trade
    hi, lo = max(c["high"] for c in since), min(c["low"] for c in since)
    fav = hi if d == 1 else lo
    last_px = since[-1]["close"]

    # 1. Was the stop hit?
    if C.DRY_RUN:
        if stop_was_hit(trade, since):
            return end_trade(trade, trade["sl"] * (1 - slip * d), "stop hit", session, state)
    else:
        if ex.open_position() is None:
            trade["missing_polls"] += 1      # two misses in a row, so one API blip can't fool us
            if trade["missing_polls"] >= 2:
                return end_trade(trade, trade["sl"] * (1 - slip * d),
                                 "stopped out on the exchange (approx fill)", session, state)
            return trade
        trade["missing_polls"] = 0

    # 2. Partial take-profit
    risk = trade["risk_px"]
    if C.PARTIAL_TP_R and not trade["partial_done"]:
        ptp = trade["entry"] + d * C.PARTIAL_TP_R * risk
        if (hi >= ptp) if d == 1 else (lo <= ptp):
            part = sizing.partial_qty(trade["qty"])
            trade["partial_done"] = True
            if part > 0:
                px = ptp * (1 - slip * d)
                ex.market_order("sell" if d == 1 else "buy", part)
                hours = (since[-1]["t"] - trade["entry_bar"]) / 3_600_000
                pnl = leg_pnl(trade, px, part, hours)
                trade["open_qty"] = round(trade["open_qty"] - part, 8)
                trade["realised"] += pnl
                state["day_pnl"] += pnl
                notify.send(f"<b>PARTIAL</b> {trade['sub_agent']} {session}\n"
                            f"<pre>closed {part} at {px:,.2f} (+{C.PARTIAL_TP_R}R)\n"
                            f"P&amp;L Rs {pnl:,.2f}\nremaining qty {trade['open_qty']}</pre>")
            else:
                notify.send(f"<b>PARTIAL SKIPPED</b> {trade['sub_agent']} {session}\n"
                            f"<pre>{trade['qty']} cannot be split into two valid orders (step {C.QTY_STEP}, min {C.MIN_QTY})</pre>",
                            silent=True)

    # 3. Breakeven and trail, from the best price reached since entry
    trade["best"] = max(trade["best"], fav) if d == 1 else min(trade["best"], fav)
    r_now = d * (trade["best"] - trade["entry"]) / risk
    if C.BREAKEVEN_AT_R and not trade["moved_be"] and r_now >= C.BREAKEVEN_AT_R:
        be = trade["entry"] * (1 + d * C.BREAKEVEN_PLUS_PCT)
        if (d == 1 and be > trade["sl"]) or (d == -1 and be < trade["sl"]):
            move_stop(trade, be, "breakeven", session)
            trade["moved_be"] = True
    if C.TRAIL_AFTER_R and r_now >= C.TRAIL_AFTER_R:
        tr = trade["best"] - d * C.TRAIL_STEP_R * risk
        if (d == 1 and tr > trade["sl"]) or (d == -1 and tr < trade["sl"]):
            move_stop(trade, tr, "trail", session)

    # 4. Session end
    if now_ist() >= end_dt - dt.timedelta(minutes=1):
        return end_trade(trade, last_px * (1 - slip * d), "session end", session, state,
                         place_order=not C.DRY_RUN)
    return trade


# ── SA2 candle-pattern exit (live) ───────────────────────────────────────────
# No R-based partial/breakeven/trail. The stop is fixed at the rejection candle's
# own extreme set at entry (never trailed). Mirrors backtest.manage_sa2_pattern():
#   - after at least one favourable-coloured candle, the first counter-coloured
#     candle is "rejection #1" -> book 50% at the NEXT candle's open.
#   - watching the remaining 50%: two consecutive candles each breaking the
#     prior candle's low (LONG) / high (SHORT) -> close the rest there.
#   - instead, a candle closes back past rejection #1's high (LONG) / low
#     (SHORT) -> hold, wait for a second rejection candle, close the rest at
#     the candle AFTER that one opens.
# Never flattened at session end — can run across session and day boundaries.

def _close_remaining(trade, px, reason, session, state, t):
    qty = trade["open_qty"]
    if qty > 0 and not C.DRY_RUN:
        ex.market_order("sell" if trade["direction"] == 1 else "buy", qty)
    hours = (t - trade["entry_bar"]) / 3_600_000
    final_leg = leg_pnl(trade, px, qty, hours)
    pnl = trade["realised"] + final_leg
    trade["open_qty"] = 0
    finish(trade, px, pnl, reason, session, state, day_delta=final_leg)


def manage_sa2_live(trade, session, state):
    d, slip = trade["direction"], C.SLIPPAGE
    mins = int((now_ms() - trade["entry_bar"]) / 60000) + 30

    # 1. stop-loss — fixed, never trails
    since = [c for c in ex.candles(minutes_back=max(mins, 60), include_forming=True)
             if c["t"] >= trade["entry_bar"]]
    if not since:
        return trade
    if C.DRY_RUN:
        if stop_was_hit(trade, since):
            return end_trade(trade, trade["sl"] * (1 - slip * d), "stop hit", session, state)
    else:
        if ex.open_position() is None:
            trade["missing_polls"] += 1
            if trade["missing_polls"] >= 2:
                return end_trade(trade, trade["sl"] * (1 - slip * d),
                                 "stopped out on the exchange (approx fill)", session, state)
            return trade
        trade["missing_polls"] = 0

    # 2. the candle-pattern state machine, over any newly CLOSED candles
    closed = [c for c in ex.candles(minutes_back=max(mins, 60), include_forming=False)
              if c["t"] > trade["pattern_last_t"]]
    for c in closed:
        trade["pattern_last_t"] = c["t"]
        is_favorable = (c["close"] > c["open"]) if d == 1 else (c["close"] < c["open"])
        is_counter = (c["close"] < c["open"]) if d == 1 else (c["close"] > c["open"])

        if trade["phase"] == "await_r1":
            if is_favorable:
                trade["seen_favorable"] = True
            elif trade["seen_favorable"] and is_counter:
                trade["rejection1"] = {"high": c["high"], "low": c["low"]}
                trade["phase"] = "book_partial"

        elif trade["phase"] == "book_partial":
            part = sizing.partial_qty(trade["qty"])
            if part > 0:
                px = c["open"] * (1 - slip * d)
                if not C.DRY_RUN:
                    ex.market_order("sell" if d == 1 else "buy", part)
                hours = (c["t"] - trade["entry_bar"]) / 3_600_000
                pnl = leg_pnl(trade, px, part, hours)
                trade["open_qty"] = round(trade["open_qty"] - part, 8)
                trade["realised"] += pnl
                state["day_pnl"] += pnl
                trade["partial_done"] = True
                notify.send(f"<b>PARTIAL</b> {trade['sub_agent']} {session}\n"
                            f"<pre>closed {part} at {px:,.2f} (rejection candle)\n"
                            f"P&amp;L Rs {pnl:,.2f}\nremaining qty {trade['open_qty']}</pre>")
            trade["phase"], trade["consec_breaks"] = "watch", 0
            trade["prior_hl"] = {"high": c["high"], "low": c["low"]}

        elif trade["phase"] == "watch":
            prior = trade["prior_hl"]
            broke = (c["low"] < prior["low"]) if d == 1 else (c["high"] > prior["high"])
            trade["consec_breaks"] = trade["consec_breaks"] + 1 if broke else 0
            trade["prior_hl"] = {"high": c["high"], "low": c["low"]}
            if trade["consec_breaks"] >= 2:
                _close_remaining(trade, c["close"] * (1 - slip * d),
                                 "pattern reversal confirmed", session, state, c["t"])
                return None
            recovered = (c["close"] > trade["rejection1"]["high"]) if d == 1 \
                        else (c["close"] < trade["rejection1"]["low"])
            if recovered:
                trade["phase"], trade["seen_favorable"] = "await_r2", False

        elif trade["phase"] == "await_r2":
            if is_favorable:
                trade["seen_favorable"] = True
            elif trade["seen_favorable"] and is_counter:
                trade["phase"] = "exit_on_next_open"

        elif trade["phase"] == "exit_on_next_open":
            _close_remaining(trade, c["open"] * (1 - slip * d), "second rejection exit",
                             session, state, c["t"])
            return None

    return trade


# ── main loop ───────────────────────────────────────────────────────────────

def main():
    missing = C.validate()
    sync_spec()
    mode = "DRY RUN — paper account, no orders" if C.DRY_RUN else "LIVE TRADING"
    notify.send(f"🟢 <b>SORB-15 started</b>\n<pre>mode      : {mode}\n"
                f"pair      : {C.PAIR}\nleverage  : {C.LEVERAGE}x\n"
                f"capital   : Rs {C.CAPITAL_INR:,.0f}\nrisk/trade: Rs {C.RISK_PER_TRADE_INR:,.0f}\n"
                f"sessions  : London 13:00-18:45, NY 19:00-01:15 IST\n"
                f"agents    : SA1 SA2 SA3 SA4</pre>")
    if missing:
        log.warning("missing env vars (ok in dry run): %s", missing)
    state = notify.load()

    while True:
        try:
            now = now_ist()
            state = notify.roll_day(state, str(now.date()))
            name, o, dl, e = active_session(now)
            trade = state.get("trade")

            # ---- outside a session ----
            if name is None:
                if trade:
                    if trade["sub_agent"] == "SA2":
                        # SA2's exit is candle-pattern based, not session-bound — keep
                        # managing it at normal poll speed even with no session open.
                        state["trade"] = manage_sa2_live(trade, trade["session"], state)
                        if state["trade"] is None:
                            state["open_london"] = None
                        notify.save(state)
                        time.sleep(C.POLL_SECONDS)
                        continue
                    # The session ended while we were offline. Flatten, never leave it open.
                    px = ex.candles(minutes_back=60, include_forming=True)[-1]["close"]
                    state["trade"] = end_trade(trade, px, "session ended while the bot was offline",
                                               trade["session"], state, place_order=not C.DRY_RUN)
                notify.save(state)
                nap = min(seconds_to_next_open(now), 900)
                log.info("no session active — sleeping %ss", nap)
                time.sleep(nap)
                continue

            # ---- manage an open trade ----
            if trade:
                if trade["sub_agent"] == "SA2":
                    state["trade"] = manage_sa2_live(trade, name, state)
                else:
                    state["trade"] = manage(trade, name, e, state)
                if state["trade"] is None:
                    state["open_london"] = None
                notify.save(state)
                time.sleep(C.POLL_SECONDS)
                continue

            key = f"{now.date()}|{name}"
            if key in state["done_sessions"] or state["halted"] or (
                    C.DAILY_LOSS_LIMIT_INR and state["day_pnl"] <= -C.DAILY_LOSS_LIMIT_INR):
                time.sleep(C.POLL_SECONDS)
                continue

            rc, sess = split_session(fetch_closed(o), o, e)
            if rc is None or len(sess) < 1:
                time.sleep(C.POLL_SECONDS)
                continue
            H, L = rc["high"], rc["low"]
            di = sa.deadline_index(sess, ms(dl))
            sig = sa.detect(sess, H, L, di, name, state.get("open_london"))

            def close_session(msg, silent=True):
                state["done_sessions"].append(key)
                notify.send(msg, silent=silent)
                notify.save(state)

            if sig is None:
                if now >= dl:
                    close_session(f"<b>{name}</b> — deadline passed, no qualifying setup. "
                                  f"Session closed for trading.")
                time.sleep(C.POLL_SECONDS)
                continue

            st = sig.get("state")
            if st == "continue":                      # SA3
                close_session(notify.signal_block(
                    sig, name, H, L, action="CONTINUE" if sig["continue_trade"] else "HOLD",
                    reason=sig["detail"]), silent=False)
                state["open_london"] = None
                continue
            if st == "pending":                       # SA4 waiting on candles
                if now >= dl + dt.timedelta(minutes=C.TIMEFRAME_MIN):
                    close_session(f"<b>{name}</b> — {sig['sub_agent']} was still awaiting confirmation "
                                  f"at the deadline. No trade.")
                time.sleep(C.POLL_SECONDS)
                continue
            if st == "veto":
                close_session(notify.signal_block(
                    sig, name, H, L, action="AVOID",
                    reason="Momentum reversed against the breakout — " + sig["detail"]),
                    silent=False)
                continue
            if sig["trigger_i"] > di:
                close_session(notify.signal_block(
                    sig, name, H, L, action="AVOID",
                    reason="Signal completed after the entry deadline"))
                continue

            # Mark the session done BEFORE any order leaves the building, so a
            # crash or exception mid-entry can never trigger a second entry.
            state["done_sessions"].append(key)
            notify.save(state)
            trade = enter(sig, name, rc, sess, state)
            state["trade"] = trade
            if trade and name == "London" and sig["sub_agent"] == "SA2":
                state["open_london"] = {"sub_agent": "SA2", "direction": sig["direction"],
                                        "level": sig["level"]}
            notify.save(state)
            time.sleep(C.POLL_SECONDS)

        except KeyboardInterrupt:
            notify.send("🔴 <b>SORB-15 stopped</b>")
            raise
        except Exception as err:                       # never let the loop die
            log.exception("loop error")
            notify.send(f"⚠️ <b>Error</b>\n<pre>{type(err).__name__}: {str(err)[:300]}</pre>")
            time.sleep(60)


if __name__ == "__main__":
    main()
