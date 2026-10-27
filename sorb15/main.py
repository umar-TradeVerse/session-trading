"""SORB-15 — SA1 + SA4 + Tokyo continuation, live on CoinDCX futures.

Sessions (IST): Tokyo 05:30-07:15-14:00, then London and New York from
config. The first candle of a session sets its range (high/low). One
position at a time: once a trade opens, every later session that day is
skipped until it resolves (SA3's old "continuation" idea, generalised —
there is no separate continuation detector any more, it falls out of simply
not looking for a new entry while one is open).

Each poll:
  1. If a trade is open, manage it: fixed stop (never moves), staged
     partial exits at 2R/3R/4R, or day-end (next Tokyo open) if neither
     stop nor every leg has been reached.
  2. Otherwise, inside a session, ask sa134_entry_signal for a signal and
     enter on it.
  3. Outside a session, sleep until the next one opens.

DRY_RUN=true runs the whole thing as a PAPER account: the same signals, the
same entry, stop and staged exits, with P&L computed from real candles —
but no order is ever sent to the exchange.

Detection uses CLOSED candles only (exactly what the backtest sees). Trade
management also reads the still-forming candle, so a target can be hit
intrabar the way the backtest assumes.

Entry logic, session windows and the SL/TP constants all live in
sorb15/backtest.py (sa134_entry_signal, TOKYO_SESSION, SA134_SL_BUFFER_PCT,
SA134_TP_R, SA134_TP_FRACS) — the single source shared with the backtest.
Never fork that logic here.
"""
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from . import backtest as BT
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

# Tokyo (from the shared backtest module) plus London / New York (from config) —
# one dict, so window()/active_session() see all three the same way.
ALL_SESSIONS = {"Tokyo": BT.TOKYO_SESSION, **C.SESSIONS}


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
    s = ALL_SESSIONS[name]
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
    for name in ALL_SESSIONS:
        o, d, e = window(name, now)
        if o <= now < e:
            return name, o, d, e
    return None, None, None, None


def seconds_to_next_open(now=None):
    now = now or now_ist()
    opens = [o for o, _, _ in (window(n, now) for n in ALL_SESSIONS) if o > now]
    return max(5, int((min(opens) - now).total_seconds())) if opens else 60


def day_end_for(ref_dt):
    """The next Tokyo open after `ref_dt`'s trading day — the same boundary
    the backtest flattens an unresolved trade at. The "trading day" containing
    `ref_dt` runs from one Tokyo open to the next, so a New York trade opened
    after midnight still belongs to the Tokyo session's day that started it."""
    tokyo_open_today = at(ref_dt.date(), BT.TOKYO_SESSION["open"])
    trading_day = ref_dt.date() if ref_dt >= tokyo_open_today else ref_dt.date() - dt.timedelta(days=1)
    return at(trading_day + dt.timedelta(days=1), BT.TOKYO_SESSION["open"])


# ── candles ─────────────────────────────────────────────────────────────────

def fetch_closed(open_dt):
    mins = int((now_ist() - open_dt).total_seconds() // 60) + 120
    return ex.candles(minutes_back=max(mins, 180))


def split_session(cs, open_dt, end_dt):
    o_ms, e_ms = ms(open_dt), ms(end_dt)
    rc = next((c for c in cs if c["t"] == o_ms), None)
    return rc, [c for c in cs if o_ms < c["t"] < e_ms]


# ── sizing and P&L ──────────────────────────────────────────────────────────

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

def enter(sig, session, rc, sess):
    d, level0 = sig["direction"], sig["level"]
    H, L = rc["high"], rc["low"]

    # The backtest enters at the open of the candle after the trigger. Live
    # can only match that if it acts right after the close; if the bot was late
    # (restart, outage) the price has moved and the trade is not the one we tested.
    trigger = sess[sig["trigger_i"]]
    lag_s = (now_ms() - (trigger["t"] + STEP_MS)) / 1000
    entry_px = sess[-1]["close"]
    sl = level0 * (1 - d * BT.SA134_SL_BUFFER_PCT)

    def avoid(why):
        notify.send(notify.signal_block(sig, session, H, L, round(entry_px, 2), round(sl, 2),
                                        "AVOID", why))
        return None

    if lag_s > C.MAX_ENTRY_LAG_SECONDS:
        return avoid(f"Missed the entry window — signal completed {lag_s:.0f}s ago, "
                     f"limit {C.MAX_ENTRY_LAG_SECONDS}s")
    if (d == 1 and entry_px <= sl) or (d == -1 and entry_px >= sl):
        return avoid("Entry is already beyond its own stop")

    fixed_margin = C.CAPITAL_INR / 2            # one of up to 2 portfolio slots
    qty = sizing.qty_for_fixed_notional(entry_px, fixed_margin)
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

    risk_px = abs(entry_px - sl)
    tp_levels = [entry_px + d * r * risk_px for r in BT.SA134_TP_R]
    leg_qtys, remaining = [], qty
    for frac in BT.SA134_TP_FRACS[:-1]:
        q = sizing.floor_step(qty * frac, C.QTY_STEP)
        if q < C.MIN_QTY - 1e-12:
            q = 0.0
        leg_qtys.append(q)
        remaining = round(remaining - q, 8)
    leg_qtys.append(round(remaining, 8))

    notional_inr = qty * entry_px * C.USDINR
    risk_inr = notional_inr * risk_px / entry_px

    entry_bar = (now_ms() // STEP_MS) * STEP_MS
    trade = {"sub_agent": sig["sub_agent"], "setup": sig["setup"], "session": session,
             "direction": d, "level": level0, "entry": entry_px, "sl": sl, "qty": qty,
             "open_qty": qty, "risk_px": risk_px, "realised": 0.0, "seen": {},
             "missing_polls": 0, "position_id": pid,
             "entry_bar": entry_bar, "opened_at": int(time.time()),
             "client_id": cid, "range_high": H, "range_low": L,
             "tp_levels": tp_levels, "leg_qtys": leg_qtys, "tp_hit": [False, False, False],
             "day_end_ms": ms(day_end_for(now_ist()))}
    notify.send(notify.signal_block(sig, session, H, L, round(entry_px, 2), round(sl, 2), "ENTER",
                                    sig.get("detail", ""),
                                    result=f"qty {qty}  margin Rs {fixed_margin:.0f}  "
                                           f"risk Rs {risk_inr:.0f}"))
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


def finish(trade, exit_px, pnl, reason, session, state, day_delta=None):
    # `pnl` is the trade's FULL realised total (it may include an earlier leg
    # already added to day_pnl when it was booked) — day_pnl must only move by
    # what's NEW at this step, or a leg gets counted twice.
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


def end_trade(trade, exit_px, reason, state, place_order=False):
    """Close whatever quantity is still open (0 once every leg has already been
    booked) and report the trade's full realised P&L."""
    qty = trade["open_qty"]
    final_leg = 0.0
    if qty > 1e-9:
        if place_order:
            pid = trade.get("position_id")
            try:
                ex.exit_position(pid)
            except Exception as err:
                notify.send(f"🚨 <b>CLOSE FAILED</b> {trade['sub_agent']} {trade['session']}\n"
                            f"<pre>{err}\nCLOSE IT MANUALLY NOW</pre>")
            try:
                ex.clear_position_orders(pid)
            except Exception:
                pass
        hours = (now_ms() - trade["entry_bar"]) / 3_600_000
        final_leg = leg_pnl(trade, exit_px, qty, hours)
        trade["open_qty"] = 0
    pnl = trade["realised"] + final_leg
    finish(trade, exit_px, pnl, reason, trade["session"], state, day_delta=final_leg)
    return None


def manage(trade, state):
    """Fixed stop (never moves), staged 2R/3R/4R partials, day-end. Returns the
    updated trade, or None once it is closed."""
    d, slip = trade["direction"], C.SLIPPAGE
    mins = int((now_ms() - trade["entry_bar"]) / 60000) + 30
    cs = ex.candles(minutes_back=max(mins, 60), include_forming=True)
    since = [c for c in cs if c["t"] >= trade["entry_bar"]]
    if not since:
        return trade

    # 1. Was the stop hit? The stop never moves, so the entry candle's extreme
    # from the first poll is as valid as any later one.
    if C.DRY_RUN:
        if stop_was_hit(trade, since):
            return end_trade(trade, trade["sl"] * (1 - slip * d), "stop_loss", state)
    else:
        if ex.open_position() is None:
            trade["missing_polls"] += 1      # two misses in a row, so one API blip can't fool us
            if trade["missing_polls"] >= 2:
                return end_trade(trade, trade["sl"] * (1 - slip * d),
                                 "stopped out on the exchange (approx fill)", state)
            return trade
        trade["missing_polls"] = 0

    # 2. Staged take-profit legs — 50% at 2R, 25% at 3R, 25% at 4R
    hi, lo = max(c["high"] for c in since), min(c["low"] for c in since)
    fav = hi if d == 1 else lo
    last_px = since[-1]["close"]
    for k in range(3):
        if trade["tp_hit"][k] or trade["leg_qtys"][k] <= 0:
            continue
        reached = (fav >= trade["tp_levels"][k]) if d == 1 else (fav <= trade["tp_levels"][k])
        if reached:
            trade["tp_hit"][k] = True
            q = trade["leg_qtys"][k]
            px = trade["tp_levels"][k] * (1 - slip * d)
            if not C.DRY_RUN:
                ex.market_order("sell" if d == 1 else "buy", q)
            hours = (since[-1]["t"] - trade["entry_bar"]) / 3_600_000
            pnl = leg_pnl(trade, px, q, hours)
            trade["open_qty"] = round(trade["open_qty"] - q, 8)
            trade["realised"] += pnl
            state["day_pnl"] += pnl
            notify.send(f"<b>TARGET {k + 1} HIT</b> {trade['sub_agent']} {trade['session']}\n"
                        f"<pre>closed {q} at {px:,.2f} (+{BT.SA134_TP_R[k]}R)\n"
                        f"P&amp;L Rs {pnl:,.2f}\nremaining qty {trade['open_qty']}</pre>")
    if trade["open_qty"] <= 1e-9:
        return end_trade(trade, last_px, "all_targets_hit", state)

    # 3. Day end — the next Tokyo open. The stop never trails; nothing else closes it.
    if now_ms() >= trade["day_end_ms"] - 60_000:
        return end_trade(trade, last_px * (1 - slip * d), "day_end", state,
                         place_order=not C.DRY_RUN)
    return trade


# ── main loop ───────────────────────────────────────────────────────────────

def main():
    """Entry point. Wraps the loop in the exact parameters the backtest validated
    this engine with (tightened clean-breakout, Rs 10,000 capital) so the live bot
    can never silently drift from them because of a Railway variable that is
    missing, stale, or still set for the old engine."""
    with C.override(CLEAR_TOUCH_TOLERANCE=0.10, CAPITAL_INR=10000):
        _main_loop()


def _main_loop():
    missing = C.validate()
    sync_spec()
    mode = "DRY RUN — paper account, no orders" if C.DRY_RUN else "LIVE TRADING"
    notify.send(f"🟢 <b>SORB-15 started</b>\n<pre>mode      : {mode}\n"
                f"pair      : {C.PAIR}\nleverage  : {C.LEVERAGE}x\n"
                f"capital   : Rs {C.CAPITAL_INR:,.0f}  (margin/slot Rs {C.CAPITAL_INR / 2:,.0f})\n"
                f"sessions  : Tokyo 05:30-07:15-14:00, London, New York (IST)\n"
                f"agents    : SA1, SA4 (clean breakout {1 - C.CLEAR_TOUCH_TOLERANCE:.0%} outside)\n"
                f"stop      : broken level +/- {BT.SA134_SL_BUFFER_PCT:.1%}, never moves\n"
                f"targets   : 50% @ 2R, 25% @ 3R, 25% @ 4R, else close at the next Tokyo open\n"
                f"one position at a time — a later session waits for it to resolve</pre>")
    if missing:
        log.warning("missing env vars (ok in dry run): %s", missing)
    state = notify.load()

    while True:
        try:
            now = now_ist()
            state = notify.roll_day(state, str(now.date()))
            name, o, dl, e = active_session(now)
            trade = state.get("trade")

            # ---- manage an open trade (any session, or none at all) ----
            if trade:
                state["trade"] = manage(trade, state)
                notify.save(state)
                time.sleep(C.POLL_SECONDS)
                continue

            # ---- outside a session ----
            if name is None:
                notify.save(state)
                nap = min(seconds_to_next_open(now), 900)
                log.info("no session active — sleeping %ss", nap)
                time.sleep(nap)
                continue

            key = f"{o.date()}|{name}"
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
            sig = BT.sa134_entry_signal(sess, H, L, di)

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
            if st == "pending":                       # SA4 awaiting momentum confirmation
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
            if sig["trigger_i"] > di and not sig.get("exempt_deadline"):
                close_session(notify.signal_block(
                    sig, name, H, L, action="AVOID",
                    reason="Signal completed after the entry deadline"))
                continue

            # Mark the session done BEFORE any order leaves the building, so a
            # crash or exception mid-entry can never trigger a second entry.
            state["done_sessions"].append(key)
            notify.save(state)
            trade = enter(sig, name, rc, sess)
            state["trade"] = trade
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
