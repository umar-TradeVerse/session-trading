"""SORB-15 — SA1 + SA4 + Tokyo continuation, live on CoinDCX futures.

Multi-symbol (28 Oct 2026): BTC, Gold, Silver, Oil, Natural Gas (BT.SA134_SYMBOLS),
sharing ONE pool of BT.SA134_MAX_SLOTS (2) concurrent positions system-wide —
fixed margin per slot = capital / max_slots. Every symbol runs the identical
engine independently; when more than one symbol qualifies for entry in the same
poll and slots run out, the slot goes to whichever signal's trigger candle
closed EARLIEST (a poller can only act on what has already completed — it
cannot wait and compare a slower symbol's breakout against a faster one). An
exact tie (same trigger candle, different symbols) is broken by close_strength,
the same tiebreak run_sa134_portfolio uses in the backtest.

Sessions (IST): Tokyo 05:30-07:15-14:00, then London and New York from config.
The first candle of a session sets the range (high/low), per symbol. A symbol
with an open position is skipped in every later session until it resolves.

Each poll:
  1. Manage every currently open trade: fixed stop (never moves), staged
     partial exits at 2R/3R/4R, or day-end (next Tokyo open) if neither the
     stop nor every leg has been reached.
  2. For every symbol without an open trade, inside a session, check
     sa134_entry_signal; collect all that qualify this poll and allocate
     whatever slots are free (earliest trigger first, strength as tiebreak).
  3. Outside a session for every symbol, sleep until the next one opens.

DRY_RUN=true runs the whole thing as a PAPER account: the same signals, the
same entry, stop and staged exits, with P&L computed from real candles —
but no order is ever sent to the exchange.

Detection uses CLOSED candles only (exactly what the backtest sees). Trade
management also reads the still-forming candle, so a target can be hit
intrabar the way the backtest assumes.

Entry logic, session windows, the SL/TP constants, and the symbol/slot
registry all live in sorb15/backtest.py (sa134_entry_signal, TOKYO_SESSION,
SA134_SL_BUFFER_PCT, SA134_TP_R, SA134_TP_FRACS, SA134_SYMBOLS,
SA134_MAX_SLOTS) — the single source shared with the backtest. Never fork
that logic here.
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

def active_symbols():
    """BT.SA134_SYMBOLS, filtered to C.ACTIVE_SYMBOLS if that's set (comma-separated
    names) -- lets one real-money service and one paper-only service, each its own
    process/env file, split the 5-symbol roster between them with one DRY_RUN truth
    apiece. Empty/unset ACTIVE_SYMBOLS -> every symbol (today's single-service default)."""
    if not C.ACTIVE_SYMBOLS:
        return dict(BT.SA134_SYMBOLS)
    wanted = [s.strip() for s in C.ACTIVE_SYMBOLS.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in BT.SA134_SYMBOLS]
    if unknown:
        raise ValueError(f"ACTIVE_SYMBOLS has unknown symbol(s): {unknown} -- "
                         f"valid names are {list(BT.SA134_SYMBOLS)}")
    return {s: BT.SA134_SYMBOLS[s] for s in wanted}


SYMBOLS = active_symbols()          # {"BTC": "B-BTC_USDT", "Gold": "B-XAU_USDT", ...}
MAX_SLOTS = BT.SA134_MAX_SLOTS      # 2

def all_sessions():
    """Tokyo (from the shared backtest module) plus London / New York (from config)
    -- one dict, so window()/active_session() see all three the same way. Read
    fresh on every call, NOT bound once at import: main() overrides C.SESSIONS
    for the live/paper services (the DST-adapted London/New York times), and
    that override only exists for the life of that `with` block -- a
    module-level binding taken at import time would freeze in the stale
    pre-override values and silently ignore it."""
    return {"Tokyo": BT.TOKYO_SESSION, **C.SESSIONS}


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
    s = all_sessions()[name]
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
    for name in all_sessions():
        o, d, e = window(name, now)
        if o <= now < e:
            return name, o, d, e
    return None, None, None, None


def seconds_to_next_open(now=None):
    now = now or now_ist()
    opens = [o for o, _, _ in (window(n, now) for n in all_sessions()) if o > now]
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

def fetch_closed(pair, open_dt):
    mins = int((now_ist() - open_dt).total_seconds() // 60) + 120
    return ex.candles(pair=pair, minutes_back=max(mins, 180))


def split_session(cs, open_dt, end_dt):
    o_ms, e_ms = ms(open_dt), ms(end_dt)
    rc = next((c for c in cs if c["t"] == o_ms), None)
    return rc, [c for c in cs if o_ms < c["t"] < e_ms]


# ── sizing and P&L ──────────────────────────────────────────────────────────

def spec_for(pair):
    """Each symbol's own contract spec (step sizes, minimums) — read once at
    startup. The exchange is the source of truth; typing these in by hand per
    symbol is how a quantity ends up rejected against the wrong step. Falls
    back to config's defaults (BTC's) if the read fails for some symbol."""
    fallback = {"QTY_STEP": C.QTY_STEP, "PRICE_STEP": C.PRICE_STEP,
                "MIN_QTY": C.MIN_QTY, "MIN_NOTIONAL_USDT": C.MIN_NOTIONAL_USDT}
    try:
        spec = ex.instrument(pair=pair)
    except Exception as err:
        log.warning("could not read the contract spec for %s (%s) — using BTC's values", pair, err)
        return fallback
    wanted = [("quantity_increment", "QTY_STEP"), ("price_increment", "PRICE_STEP"),
              ("min_trade_size", "MIN_QTY"), ("min_quantity", "MIN_QTY"),
              ("min_notional", "MIN_NOTIONAL_USDT")]
    out = dict(fallback)
    for key, attr in wanted:
        v = spec.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            if attr == "MIN_QTY" and attr in out and out[attr] != fallback[attr]:
                v = max(v, out[attr])
            out[attr] = v
    return out


def sync_specs():
    specs = {}
    lines = []
    for sym, pair in SYMBOLS.items():
        specs[sym] = spec_for(pair)
        s = specs[sym]
        log.info("%s (%s) contract spec: step=%s min_qty=%s price_step=%s min_notional=%s",
                 sym, pair, s["QTY_STEP"], s["MIN_QTY"], s["PRICE_STEP"], s["MIN_NOTIONAL_USDT"])
        lines.append(f"{sym}: step={s['QTY_STEP']:g} min_qty={s['MIN_QTY']:g} "
                     f"price_step={s['PRICE_STEP']:g} min_notional={s['MIN_NOTIONAL_USDT']:g}")
    notify.send("ℹ️ Contract specs read from CoinDCX:\n<pre>" + "\n".join(lines) + "</pre>",
                silent=True)
    return specs


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

def wait_for_position(pair, tries=10):
    """After an entry order, read the position back from the exchange."""
    for _ in range(tries):
        live = ex.open_position(pair=pair)
        if live:
            return live
        time.sleep(1)
    return None


def flatten(trade_or_pid, why, sym, session, sub_agent=""):
    """Emergency exit. Used whenever a position cannot be protected."""
    pid = trade_or_pid.get("position_id") if isinstance(trade_or_pid, dict) else trade_or_pid
    try:
        ex.exit_position(pid)
        outcome = "position closed at market"
    except Exception as err:
        outcome = f"CLOSE FAILED ({err}) — CLOSE IT MANUALLY NOW"
    notify.send(f"🚨 <b>{why}</b> {sub_agent} {sym} {session}\n<pre>{outcome}</pre>")


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


# ── live-only order-book overlay ─────────────────────────────────────────────
# Cannot be backtested (see config.ORDERBOOK_FILTER) -- read that before touching
# this. Both functions fail OPEN: an API hiccup here must never block a trade or
# leave a position without the baseline stop it would otherwise have gotten.

def _safe_orderbook(pair):
    try:
        return ex.orderbook(pair=pair)
    except Exception as err:
        log.warning("order-book fetch failed for %s (%s) — skipping the overlay this time", pair, err)
        return None


def _orderbook_wall_against(pair, d, entry_px):
    """None if no wall, the feature is off, or the fetch failed. Otherwise
    (wall_px, wall_qty) -- a resting level on the side AGAINST the breakout
    direction, within ORDERBOOK_GATE_PCT of entry. The caller doesn't avoid the
    trade on this any more (see the hunt-watch functions below) -- it diverts
    to watching for a sweep-through + reversal instead."""
    if not C.ORDERBOOK_FILTER or pair not in C.ORDERBOOK_SUPPORTED_PAIRS:
        return None
    ob = _safe_orderbook(pair)
    if ob is None:
        return None
    against = ob["asks"] if d == 1 else ob["bids"]
    wall = ex.nearest_wall(against, mult=C.ORDERBOOK_WALL_MULT)
    if wall is None:
        return None
    wpx, wq = wall
    if abs(wpx - entry_px) / entry_px > C.ORDERBOOK_GATE_PCT:
        return None
    return wpx, wq


def _orderbook_stop(pair, d, entry_px, sl):
    """Returns (sl, note) -- `sl` widened past a protective-side wall if one is
    found within ORDERBOOK_STOP_SEARCH_PCT beyond the baseline stop; otherwise
    the baseline `sl` unchanged and note=None. Never tightens the stop."""
    if not C.ORDERBOOK_FILTER or pair not in C.ORDERBOOK_SUPPORTED_PAIRS:
        return sl, None
    ob = _safe_orderbook(pair)
    if ob is None:
        return sl, None
    protective = ob["bids"] if d == 1 else ob["asks"]
    search_limit = sl * (1 - d * C.ORDERBOOK_STOP_SEARCH_PCT)
    beyond = [(p, q) for p, q in protective
              if ((sl > p >= search_limit) if d == 1 else (sl < p <= search_limit))]
    # A 1% search band this far from the entry price is naturally thin in a real
    # book (depth clusters near the current price) -- judge it against its own
    # few levels rather than demanding the same 10-level minimum the entry gate
    # uses right at the money.
    wall = ex.nearest_wall(beyond, mult=C.ORDERBOOK_WALL_MULT, min_levels=3)
    if wall is None:
        return sl, None
    wpx, wq = wall
    new_sl = wpx * (1 - d * C.ORDERBOOK_STOP_PAD_PCT)
    return new_sl, f"stop widened to {new_sl:,.2f}, past a {wq:g} wall at {wpx:,.2f}"


# ── entry ───────────────────────────────────────────────────────────────────

def enter(sym, pair, spec, sig, session, rc, sess):
    d, level0 = sig["direction"], sig["level"]
    H, L = rc["high"], rc["low"]

    # The backtest enters at the open of the candle after the trigger. Live
    # can only match that if it acts right after the close; if the bot was late
    # (restart, outage) the price has moved and the trade is not the one we tested.
    trigger = sess[sig["trigger_i"]]
    lag_s = (now_ms() - (trigger["t"] + STEP_MS)) / 1000
    entry_px = sess[-1]["close"]
    sl = level0 * (1 - d * BT.SA134_SL_BUFFER_PCT)
    label = f"{sym} {session}"

    def avoid(why):
        notify.send(notify.signal_block(sig, label, H, L, round(entry_px, 2), round(sl, 2),
                                        "AVOID", why))
        return None

    if lag_s > C.MAX_ENTRY_LAG_SECONDS:
        return avoid(f"Missed the entry window — signal completed {lag_s:.0f}s ago, "
                     f"limit {C.MAX_ENTRY_LAG_SECONDS}s")
    if (d == 1 and entry_px <= sl) or (d == -1 and entry_px >= sl):
        return avoid("Entry is already beyond its own stop")

    sl, stop_note = _orderbook_stop(pair, d, entry_px, sl)
    if stop_note:
        log.info("%s: %s", label, stop_note)

    fixed_margin = C.CAPITAL_INR / MAX_SLOTS
    with C.override(QTY_STEP=spec["QTY_STEP"], MIN_QTY=spec["MIN_QTY"],
                    MIN_NOTIONAL_USDT=spec["MIN_NOTIONAL_USDT"], PRICE_STEP=spec["PRICE_STEP"]):
        qty = sizing.qty_for_fixed_notional(entry_px, fixed_margin)
        if qty <= 0:
            return avoid(f"Size is below the exchange minimum (min qty {spec['MIN_QTY']}, "
                         f"min notional {spec['MIN_NOTIONAL_USDT']:g} USDT, step {spec['QTY_STEP']})")

        cid = f"sorb15-{sym}-{sig['sub_agent']}-{int(time.time())}"
        live = None
        try:
            ex.market_order("buy" if d == 1 else "sell", qty, pair=pair)
        except Exception as err:
            # The request may have reached the exchange even though the reply was lost.
            # Never assume it failed: look at the real position before deciding.
            time.sleep(2)
            live = None if C.DRY_RUN else ex.open_position(pair=pair)
            if not live:
                return avoid(f"Entry order failed: {err}")
            notify.send(f"⚠️ Entry call errored ({err}) but the position exists. Protecting it.")

        pid, fill = None, entry_px
        if not C.DRY_RUN:
            live = live or wait_for_position(pair)
            if not live:
                return avoid("Entry order sent but no position appeared on the exchange")
            pid, fill = live["id"], (live["entry"] or entry_px)
            if (d == 1 and fill <= sl) or (d == -1 and fill >= sl):
                flatten(pid, "FILL BEYOND STOP — closed", sym, session, sig["sub_agent"])
                return None
        try:
            place_stop(pid, sl)
        except Exception as err:
            flatten(pid, f"STOP NOT CONFIRMED ({err}) — closed", sym, session, sig["sub_agent"])
            return None
        entry_px = fill

        risk_px = abs(entry_px - sl)
        tp_levels = [entry_px + d * r * risk_px for r in BT.SA134_TP_R]
        leg_qtys, remaining = [], qty
        for frac in BT.SA134_TP_FRACS[:-1]:
            q = sizing.floor_step(qty * frac, spec["QTY_STEP"])
            if q < spec["MIN_QTY"] - 1e-12:
                q = 0.0
            leg_qtys.append(q)
            remaining = round(remaining - q, 8)
        leg_qtys.append(round(remaining, 8))

    notional_inr = qty * entry_px * C.USDINR
    risk_inr = notional_inr * risk_px / entry_px

    entry_bar = (now_ms() // STEP_MS) * STEP_MS
    trade = {"symbol": sym, "pair": pair, "sub_agent": sig["sub_agent"], "setup": sig["setup"],
             "session": session, "direction": d, "level": level0, "entry": entry_px, "sl": sl,
             "qty": qty, "open_qty": qty, "risk_px": risk_px, "realised": 0.0, "seen": {},
             "missing_polls": 0, "position_id": pid,
             "entry_bar": entry_bar, "opened_at": int(time.time()),
             "client_id": cid, "range_high": H, "range_low": L,
             "tp_levels": tp_levels, "leg_qtys": leg_qtys, "tp_hit": [False, False, False],
             "day_end_ms": ms(day_end_for(now_ist()))}
    notify.send(notify.signal_block(sig, label, H, L, round(entry_px, 2), round(sl, 2), "ENTER",
                                    sig.get("detail", ""),
                                    result=f"qty {qty}  margin Rs {fixed_margin:.0f}  "
                                           f"risk Rs {risk_inr:.0f}"))
    log.info("ENTER %s %s %s qty=%s entry=%.2f sl=%.2f", sym, sig["sub_agent"], d, qty, entry_px, sl)
    return trade


# ── liquidity-hunt reversal (replaces the old defensive order-book gate) ────
# Umar's explicit redesign, 5 Nov: a wall sitting against the breakout isn't a
# reason to stand down -- it's where a hunt is likely. Instead of avoiding,
# watch for price to SWEEP THROUGH the wall (continuing the original breakout
# direction, consuming that resting liquidity) on good volume, then a reversal
# candle right after -- and if that happens, trade the REVERSAL, not the
# original breakout. Same mental model as the Gold 4-Nov example Umar flagged
# (a stop-hunt then a hard reverse), anchored to a real resting order instead
# of an inferred session range line. Still BTC-only in practice (the only
# symbol with a real CoinDCX order book) and still cannot be backtested -- no
# historical order book exists to replay this against, so this can only ever
# be judged by watching it trade live.

def register_hunt_watch(state, sym, pair, d0, wall_px, wall_qty, session, H, L, expires_at):
    state.setdefault("hunt_watches", {})[sym] = {
        "pair": pair, "d0": d0, "wall_px": wall_px, "wall_qty": wall_qty,
        "session": session, "H": H, "L": L,
        "registered_at": now_ms(), "expires_at": expires_at,
    }
    notify.send(f"<b>{sym} {session}</b> — order-book wall against the breakout "
                f"({wall_qty:g} resting at {wall_px:,.2f}): watching for a sweep through it "
                f"+ a reversal candle, instead of entering the original breakout "
                f"(live-only check, never backtested).", silent=False)


def enter_hunt_reversal(sym, pair, spec, sig, session, H, L, entry_px, sl):
    """Places the REVERSAL trade once a hunt watch confirms. Mirrors enter()'s
    order-placement core but isn't anchored to a session's range candle the
    way enter() is, so it's kept as its own function rather than sharing that
    lag/deadline machinery, which doesn't apply here."""
    d = sig["direction"]
    label = f"{sym} {session}"

    def avoid(why):
        notify.send(notify.signal_block(sig, label, H, L, round(entry_px, 2), round(sl, 2),
                                        "AVOID", why))
        return None

    if (d == 1 and entry_px <= sl) or (d == -1 and entry_px >= sl):
        return avoid("Entry is already beyond its own stop")

    fixed_margin = C.CAPITAL_INR / MAX_SLOTS
    with C.override(QTY_STEP=spec["QTY_STEP"], MIN_QTY=spec["MIN_QTY"],
                    MIN_NOTIONAL_USDT=spec["MIN_NOTIONAL_USDT"], PRICE_STEP=spec["PRICE_STEP"]):
        qty = sizing.qty_for_fixed_notional(entry_px, fixed_margin)
        if qty <= 0:
            return avoid(f"Size is below the exchange minimum (min qty {spec['MIN_QTY']}, "
                         f"min notional {spec['MIN_NOTIONAL_USDT']:g} USDT, step {spec['QTY_STEP']})")

        cid = f"sorb15-{sym}-HUNT-{int(time.time())}"
        live = None
        try:
            ex.market_order("buy" if d == 1 else "sell", qty, pair=pair)
        except Exception as err:
            time.sleep(2)
            live = None if C.DRY_RUN else ex.open_position(pair=pair)
            if not live:
                return avoid(f"Entry order failed: {err}")
            notify.send(f"⚠️ Entry call errored ({err}) but the position exists. Protecting it.")

        pid, fill = None, entry_px
        if not C.DRY_RUN:
            live = live or wait_for_position(pair)
            if not live:
                return avoid("Entry order sent but no position appeared on the exchange")
            pid, fill = live["id"], (live["entry"] or entry_px)
            if (d == 1 and fill <= sl) or (d == -1 and fill >= sl):
                flatten(pid, "FILL BEYOND STOP — closed", sym, session, sig["sub_agent"])
                return None
        try:
            place_stop(pid, sl)
        except Exception as err:
            flatten(pid, f"STOP NOT CONFIRMED ({err}) — closed", sym, session, sig["sub_agent"])
            return None
        entry_px = fill

        risk_px = abs(entry_px - sl)
        tp_levels = [entry_px + d * r * risk_px for r in BT.SA134_TP_R]
        leg_qtys, remaining = [], qty
        for frac in BT.SA134_TP_FRACS[:-1]:
            q = sizing.floor_step(qty * frac, spec["QTY_STEP"])
            if q < spec["MIN_QTY"] - 1e-12:
                q = 0.0
            leg_qtys.append(q)
            remaining = round(remaining - q, 8)
        leg_qtys.append(round(remaining, 8))

    notional_inr = qty * entry_px * C.USDINR
    risk_inr = notional_inr * risk_px / entry_px

    entry_bar = (now_ms() // STEP_MS) * STEP_MS
    trade = {"symbol": sym, "pair": pair, "sub_agent": sig["sub_agent"], "setup": sig["setup"],
             "session": session, "direction": d, "level": sig["level"], "entry": entry_px, "sl": sl,
             "qty": qty, "open_qty": qty, "risk_px": risk_px, "realised": 0.0, "seen": {},
             "missing_polls": 0, "position_id": pid,
             "entry_bar": entry_bar, "opened_at": int(time.time()),
             "client_id": cid, "range_high": H, "range_low": L,
             "tp_levels": tp_levels, "leg_qtys": leg_qtys, "tp_hit": [False, False, False],
             "day_end_ms": ms(day_end_for(now_ist()))}
    notify.send(notify.signal_block(sig, label, H, L, round(entry_px, 2), round(sl, 2), "ENTER",
                                    sig.get("detail", ""),
                                    result=f"qty {qty}  margin Rs {fixed_margin:.0f}  "
                                           f"risk Rs {risk_inr:.0f}"))
    log.info("ENTER %s HUNT %s qty=%s entry=%.2f sl=%.2f", sym, d, qty, entry_px, sl)
    return trade


def check_hunt_watches(state, specs, free_slots):
    """Advances every pending hunt watch by one poll. Returns {sym: trade} for
    anything actually entered this poll (the caller merges these into
    state['trades']). `free_slots` bounds how many of this poll's confirmations
    can actually enter -- in practice at most one watch is ever pending at a
    time (only BTC has a real order book), but this keeps two watches that
    happened to confirm in the same poll from ever exceeding the slot cap."""
    watches = state.get("hunt_watches") or {}
    entered = {}
    for sym in list(watches.keys()):
        if free_slots - len(entered) <= 0:
            break
        w = watches[sym]
        if now_ms() >= w["expires_at"]:
            notify.send(f"<b>{sym} {w['session']}</b> — liquidity-hunt watch expired "
                        f"(the wall at {w['wall_px']:,.2f} was never swept + confirmed this "
                        f"session). No trade.", silent=False)
            del watches[sym]
            continue

        pair, d0, wall_px = w["pair"], w["d0"], w["wall_px"]
        try:
            cs = ex.candles(pair=pair, minutes_back=600)
        except Exception as err:
            log.warning("hunt-watch candle fetch failed for %s (%s)", sym, err)
            continue
        cs = [c for c in cs if c["t"] >= w["registered_at"]]
        if len(cs) < 2:
            continue

        found = None
        for i in range(len(cs) - 1):
            c = cs[i]
            beyond = (c["close"] > wall_px) if d0 == 1 else (c["close"] < wall_px)
            if not beyond:
                continue
            if not sa.good_volume(cs, i, C.SA134_VOL_WINDOW, C.SA134_VOL_MULT):
                continue
            nxt = cs[i + 1]
            if sa.reversal_candle(nxt, d0):
                found = (c, nxt)
                break
        if found is None:
            continue

        sweep_c, confirm_c = found
        new_d = -d0
        entry_px = confirm_c["close"] * (1 + C.SLIPPAGE * new_d)
        new_level = sweep_c["low"] if new_d == 1 else sweep_c["high"]
        sl = new_level * (1 - new_d * BT.SA134_SL_BUFFER_PCT)
        sig = {"sub_agent": "HUNT", "setup": "Liquidity-hunt reversal off a real order-book wall",
               "direction": new_d, "level": wall_px, "breakout": True, "rejection": True,
               "momentum_shift": True,
               "detail": f"swept past a {w['wall_qty']:g} wall at {wall_px:,.2f} on volume, "
                         f"confirmed by a reversal candle -- trading the reversal"}
        del watches[sym]
        trade = enter_hunt_reversal(sym, pair, specs[sym], sig, w["session"], w["H"], w["L"],
                                    entry_px, sl)
        if trade:
            entered[sym] = trade
    return entered


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


def finish(trade, exit_px, pnl, reason, state, day_delta=None):
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
    notify.send(f"<b>CLOSED</b> {trade['sub_agent']} {trade['symbol']} {trade['session']}{tag}\n"
                f"<pre>reason   : {reason}\n"
                f"side     : {'LONG' if trade['direction'] == 1 else 'SHORT'}\n"
                f"entry    : {trade['entry']:,.2f}\n"
                f"exit     : {exit_px:,.2f}\n"
                f"P&amp;L      : Rs {pnl:,.2f}  (after fees)\n"
                f"day P&amp;L  : Rs {state['day_pnl']:,.2f}</pre>")
    log.info("CLOSED %s %s %s pnl=%.2f", trade["symbol"], trade["sub_agent"], reason, pnl)


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
                notify.send(f"🚨 <b>CLOSE FAILED</b> {trade['sub_agent']} {trade['symbol']} "
                            f"{trade['session']}\n<pre>{err}\nCLOSE IT MANUALLY NOW</pre>")
            try:
                ex.clear_position_orders(pid)
            except Exception:
                pass
        hours = (now_ms() - trade["entry_bar"]) / 3_600_000
        final_leg = leg_pnl(trade, exit_px, qty, hours)
        trade["open_qty"] = 0
    pnl = trade["realised"] + final_leg
    finish(trade, exit_px, pnl, reason, state, day_delta=final_leg)
    return None


def manage(trade, state):
    """Fixed stop (never moves), staged 2R/3R/4R partials, day-end. Returns the
    updated trade, or None once it is closed."""
    d, slip = trade["direction"], C.SLIPPAGE
    pair = trade["pair"]
    mins = int((now_ms() - trade["entry_bar"]) / 60000) + 30
    cs = ex.candles(pair=pair, minutes_back=max(mins, 60), include_forming=True)
    since = [c for c in cs if c["t"] >= trade["entry_bar"]]
    if not since:
        return trade

    # 1. Was the stop hit? The stop never moves, so the entry candle's extreme
    # from the first poll is as valid as any later one.
    if C.DRY_RUN:
        if stop_was_hit(trade, since):
            return end_trade(trade, trade["sl"] * (1 - slip * d), "stop_loss", state)
    else:
        if ex.open_position(pair=pair) is None:
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
                ex.market_order("sell" if d == 1 else "buy", q, pair=pair)
            hours = (since[-1]["t"] - trade["entry_bar"]) / 3_600_000
            pnl = leg_pnl(trade, px, q, hours)
            trade["open_qty"] = round(trade["open_qty"] - q, 8)
            trade["realised"] += pnl
            state["day_pnl"] += pnl
            notify.send(f"<b>TARGET {k + 1} HIT</b> {trade['sub_agent']} {trade['symbol']} "
                        f"{trade['session']}\n"
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
    this engine with (tightened clean-breakout) so the live bot can never
    silently drift from them because of a Railway/droplet env var that is
    missing, stale, or still set for the old engine.

    CAPITAL_INR is tied to DRY_RUN itself, not to a separate env var, for the
    same drift-proofing reason: the real-money service (DRY_RUN=false) uses
    Rs 10,042.53 -- the actual usable futures-wallet balance Umar confirmed --
    the paper service (DRY_RUN=true, Gold/Silver) keeps the round Rs 10,000
    it's always backtested against.

    ORDERBOOK_FILTER and SA134_ENFORCE_COMPANION_PAIRS are Umar's explicit
    instruction to go live with -- hardcoded here for the same reason: a
    missing env var on the droplet must never silently turn either back off.

    SESSIONS is shifted +1h on London and New York to track their REAL current
    (standard-time) opens -- NYSE 9:30am ET and LSE 8:00am UK time, both now on
    winter/standard time (US fell back 1 Nov, UK 25 Oct). Backtested worse than
    the old fixed blocks (full period net -15,648 to -26,485 vs -2,294 baseline,
    depending on variant) -- Umar's explicit call to deploy anyway and watch it
    live rather than trust the backtest here. Tokyo is untouched: Japan has no
    DST, so its real open never moves. config.SESSIONS itself stays the old
    values (so the baseline fingerprint/regression tests keep meaning what they
    always have) -- this override is what actually reaches the live/paper bots."""
    capital = 10042.53 if not C.DRY_RUN else 10000
    shifted_sessions = {
        "London":   {"open": dt.time(15, 0), "deadline": dt.time(16, 45), "end": dt.time(20, 0)},
        "New York": {"open": dt.time(20, 0), "deadline": dt.time(21, 45), "end": dt.time(2, 15)},
    }
    with C.override(CLEAR_TOUCH_TOLERANCE=0.10, CAPITAL_INR=capital,
                    ORDERBOOK_FILTER=True, SA134_ENFORCE_COMPANION_PAIRS=True,
                    SESSIONS=shifted_sessions):
        _main_loop()


def _session_times_str():
    """Reads the ACTUAL session windows in effect (all_sessions(), which honors
    main()'s SESSIONS override) instead of a hardcoded string, so the startup
    banner can never claim a session time this process isn't really running."""
    parts = []
    for name, s in all_sessions().items():
        parts.append(f"{name} {s['open'].strftime('%H:%M')}-{s['deadline'].strftime('%H:%M')}"
                     f"-{s['end'].strftime('%H:%M')}")
    return ", ".join(parts) + " (IST)"


def _main_loop():
    missing = C.validate()
    specs = sync_specs()
    mode = "DRY RUN — paper account, no orders" if C.DRY_RUN else "LIVE TRADING"
    notify.send(f"🟢 <b>SORB-15 started</b>\n<pre>mode      : {mode}\n"
                f"symbols   : {', '.join(SYMBOLS)} ({MAX_SLOTS} concurrent slots)\n"
                f"leverage  : {C.LEVERAGE}x\n"
                f"capital   : Rs {C.CAPITAL_INR:,.0f}  (margin/slot Rs {C.CAPITAL_INR / MAX_SLOTS:,.0f})\n"
                f"sessions  : {_session_times_str()}\n"
                f"agents    : SA1, SA4 (clean breakout {1 - C.CLEAR_TOUCH_TOLERANCE:.0%} outside)\n"
                f"stop      : broken level +/- {BT.SA134_SL_BUFFER_PCT:.1%}, never moves\n"
                f"targets   : 50% @ 2R, 25% @ 3R, 25% @ 4R, else close at the next Tokyo open\n"
                f"a free slot goes to the fastest qualifying breakout, strength breaks a tie\n"
                f"pairing   : only BTC+Oil, BTC+NatGas, or Oil+NatGas may be open together "
                f"(Gold/Silver trade solo only)\n"
                f"order book: live BTC order-book stop-widening + liquidity-hunt reversal ON "
                f"(wall against a breakout -> watch for a sweep + reversal, trade that instead; "
                f"other 4 symbols have no order book on CoinDCX — no-op there, never backtested)</pre>")
    if missing:
        log.warning("missing env vars (ok in dry run): %s", missing)
    state = notify.load()

    while True:
        try:
            try:
                with open(C.HEARTBEAT_FILE, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass      # a watchdog missing one beat is harmless; never block on this
            now = now_ist()
            state = notify.roll_day(state, str(now.date()))
            name, o, dl, e = active_session(now)
            trades = state.get("trades") or {}

            # ---- manage every open trade first, regardless of session ----
            for sym in list(trades.keys()):
                t = manage(trades[sym], state)
                if t is None:
                    del trades[sym]
                else:
                    trades[sym] = t
            state["trades"] = trades

            # ---- advance every pending liquidity-hunt watch, regardless of session ----
            hunted = check_hunt_watches(state, specs, MAX_SLOTS - len(trades))
            for sym, t in hunted.items():
                trades[sym] = t
            if hunted:
                state["trades"] = trades
            notify.save(state)

            if name is None:
                notify.save(state)
                nap = min(seconds_to_next_open(now), 900)
                log.info("no session active — sleeping %ss", nap)
                time.sleep(nap)
                continue

            free_slots = MAX_SLOTS - len(trades)
            if free_slots <= 0 or state["halted"] or (
                    C.DAILY_LOSS_LIMIT_INR and state["day_pnl"] <= -C.DAILY_LOSS_LIMIT_INR):
                notify.save(state)
                time.sleep(C.POLL_SECONDS)
                continue

            # ---- scan every free symbol for a qualifying signal this poll ----
            candidates = []
            for sym, pair in SYMBOLS.items():
                if sym in trades or sym in (state.get("hunt_watches") or {}):
                    continue
                key = f"{o.date()}|{name}|{sym}"
                if key in state["done_sessions"]:
                    continue

                rc, sess = split_session(fetch_closed(pair, o), o, e)
                if rc is None or len(sess) < 1:
                    continue
                H, L = rc["high"], rc["low"]
                di = sa.deadline_index(sess, ms(dl))
                sig = BT.sa134_entry_signal(sess, H, L, di)
                label = f"{sym} {name}"

                def close_session(msg, silent=True, _key=key):
                    state["done_sessions"].append(_key)
                    notify.send(msg, silent=silent)

                if sig is None:
                    if now >= dl:
                        close_session(f"<b>{label}</b> — deadline passed, no qualifying setup. "
                                      f"Session closed for trading.")
                    continue

                st = sig.get("state")
                if st == "pending":                       # SA4 awaiting momentum confirmation
                    if now >= dl + dt.timedelta(minutes=C.TIMEFRAME_MIN):
                        close_session(f"<b>{label}</b> — {sig['sub_agent']} was still awaiting "
                                      f"confirmation at the deadline. No trade.")
                    continue
                if st == "veto":
                    close_session(notify.signal_block(
                        sig, label, H, L, action="AVOID",
                        reason="Momentum reversed against the breakout — " + sig["detail"]),
                        silent=False)
                    continue
                if sig["trigger_i"] > di and not sig.get("exempt_deadline"):
                    close_session(notify.signal_block(
                        sig, label, H, L, action="AVOID",
                        reason="Signal completed after the entry deadline"))
                    continue

                trigger_c = sess[sig["trigger_i"]]
                strength = sa.close_strength(trigger_c, sig["direction"], H, L)
                candidates.append(dict(sym=sym, pair=pair, key=key, sig=sig, rc=rc, sess=sess,
                                       H=H, L=L, trigger_ms=trigger_c["t"], strength=strength))

            notify.save(state)

            # ---- allocate free slots: earliest trigger first, strength breaks a tie ----
            candidates.sort(key=lambda c: (c["trigger_ms"], -c["strength"]))
            open_syms = set(trades.keys())
            for c in candidates:
                sym, sig, H, L = c["sym"], c["sig"], c["H"], c["L"]
                label = f"{sym} {name}"
                if free_slots <= 0:
                    state["done_sessions"].append(c["key"])
                    notify.send(notify.signal_block(
                        sig, label, H, L, action="AVOID",
                        reason="No free slot this poll — claimed by a faster or stronger "
                               "breakout on another symbol"), silent=False)
                    continue
                if C.SA134_ENFORCE_COMPANION_PAIRS and not BT.compatible_with_open(open_syms, sym):
                    state["done_sessions"].append(c["key"])
                    notify.send(notify.signal_block(
                        sig, label, H, L, action="AVOID",
                        reason=f"Slot-pairing policy — {sym} may not open alongside "
                               f"{', '.join(sorted(open_syms))}"), silent=False)
                    continue

                # Mark the session done BEFORE any order leaves the building, so a
                # crash or exception mid-entry can never trigger a second entry.
                state["done_sessions"].append(c["key"])
                notify.save(state)

                entry_px_est = c["sess"][-1]["close"]
                wall = _orderbook_wall_against(c["pair"], sig["direction"], entry_px_est)
                if wall is not None:
                    wpx, wq = wall
                    register_hunt_watch(state, sym, c["pair"], sig["direction"], wpx, wq,
                                        name, H, L, ms(e))
                    notify.save(state)
                    continue

                trade = enter(sym, c["pair"], specs[sym], sig, name, c["rc"], c["sess"])
                if trade:
                    trades[sym] = trade
                    free_slots -= 1
                    open_syms.add(sym)
                state["trades"] = trades
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
