"""Offline tests. No network, no API keys, no orders.

    python test_local.py
"""
import datetime as dt
import os
import random
import sys

os.environ["DRY_RUN"] = "true"

from sorb15 import backtest as B
from sorb15 import config as C
from sorb15 import coindcx as ex
from sorb15 import main as M
from sorb15 import notify
from sorb15 import subagents as sa

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}\n        got  {got}\n        want {want}")


def K(o, h, l, c, t=0):
    return {"t": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


H_, L_ = 85200.0, 84800.0          # a typical opening range
DI = 5

print("SA1 — fires on the second candle alone; no entry candle needed")
s = sa.SA1_second_candle_breakout([K(85150, 85600, 85140, 85560)], H_, L_, DI)
check("detected with ONE candle", s is not None, True)
check("direction LONG", s and s["direction"], 1)
check("state ready", s and s["state"], "ready")
check("a candle that stays inside gives nothing",
      sa.SA1_second_candle_breakout([K(85000, 85100, 84900, 85050)], H_, L_, DI), None)

print("SA2 — tap, then rejection; direction comes from the rejection")
tap1 = K(85100, 85198, 85090, 85190)     # touches the high, closes near it: a tap, no rejection
tap2 = K(85150, 85199, 85140, 85195)
rej = K(85130, 85205, 85000, 85020)      # long upper wick, closes back down
check("two taps alone give no signal",
      sa.SA2_tap_rejection([tap1, tap2], H_, L_, DI), None)
s = sa.SA2_tap_rejection([tap1, tap2, rej], H_, L_, DI)
check("detected on the rejection candle", s is not None, True)
check("rejection at the HIGH -> SHORT", s and s["direction"], -1)
check("fires on candle index 2, after the taps", s and s["trigger_i"], 2)
check("taps are counted", s and s["taps"], 3)
s = sa.SA2_tap_rejection([K(84900, 85000, 84790, 84950), K(84950, 85000, 84795, 85010)],
                         H_, L_, DI)
check("rejection at the LOW -> LONG", s and s["direction"], 1)
check("a tap alone is never an entry",
      sa.SA2_tap_rejection([K(85100, 85195, 85050, 85180), K(85180, 85190, 85150, 85175)],
                           H_, L_, DI), None)
check("SA2 stands down once a candle closes beyond a line",
      sa.SA2_tap_rejection([K(85100, 85300, 85050, 85280), K(85280, 85290, 85100, 85150)],
                           H_, L_, DI), None)

print("SA4 — causal: it waits, and never reads candles that have not closed")
brk = K(84805, 84815, 84400, 84450)                       # clean break below
doji = K(84450, 84500, 84400, 84460)
up = K(84460, 84900, 84450, 84880)                        # closes above the doji's high
go = K(84450, 84470, 84150, 84200)                        # keeps falling
check("breakout only -> PENDING, not ready",
      sa.SA4_breakout_momentum_check([K(85100, 85150, 84790, 84810), brk], H_, L_, DI)["state"],
      "pending")
check("doji formed, next candle not here yet -> PENDING",
      sa.SA4_breakout_momentum_check([K(85100, 85150, 84790, 84810), brk, doji],
                                     H_, L_, DI)["state"], "pending")
v = sa.SA4_breakout_momentum_check([K(85100, 85150, 84790, 84810), brk, doji, up], H_, L_, DI)
check("doji then a close above it -> VETO", v["state"], "veto")
check("momentum_shift YES", v["momentum_shift"], True)
r = sa.SA4_breakout_momentum_check([K(85100, 85150, 84790, 84810), brk, go, go], H_, L_, DI)
check("no reversal across the window -> READY", r["state"], "ready")
check("trigger is the last window candle", r["trigger_i"], 1 + C.MOMENTUM_WINDOW)
check("direction SHORT", r["direction"], -1)
check("a breakout that overlaps the range is not SA4's",
      sa.SA4_breakout_momentum_check([K(85000, 85100, 84300, 84700)], H_, L_, DI), None)

print("SA3 — continuation of an open SA2 trade only")
openl = {"sub_agent": "SA2", "direction": -1, "level": 85200}
s = sa.SA3_ny_continuation([K(84980, 85045, 84940, 84990), K(84990, 85055, 84900, 84920)],
                           85050, 84900, DI, openl)
check("NY agrees -> continue", s and s["continue_trade"], True)
check("same direction", s and s["direction"], -1)
check("no open SA2 trade -> nothing",
      sa.SA3_ny_continuation([K(1, 2, 0, 1)], 2, 0, DI, None), None)

print("Router")
s = sa.detect([K(85150, 85600, 85140, 85560)], H_, L_, DI, "London", None)
check("SA1 wins on a clear second-candle break", s and s["sub_agent"], "SA1")

print("Sizing, costs and entry guard")
from sorb15 import sizing as _sz
q = _sz.qty_for_fixed_notional(85000, 2500)          # one slot: capital/2 at the default 5,000 capital
check("qty positive", q > 0, True)
check("qty is a whole number of exchange steps (0.0047 would be rejected)",
      abs(q / C.QTY_STEP - round(q / C.QTY_STEP)) < 1e-9, True)
notional_inr = q * 85000 * C.USDINR
check("exposure matches margin x leverage, floored to the step",
      abs(notional_inr - 2500 * C.LEVERAGE) < 85000 * C.USDINR * C.QTY_STEP, True)
check("too small a margin for the minimum size -> 0, trade skipped",
      _sz.qty_for_fixed_notional(85000, 0.01), 0.0)
tr = {"direction": 1, "entry": 85000.0}
check("round-trip fee is charged",
      round(M.leg_pnl(tr, 85000.0, 0.01), 2) < 0, True)

print("Session clock (IST) — Tokyo, London, New York")
A = M.active_session
check("06:00 -> Tokyo", A(dt.datetime(2026, 9, 28, 6, 0, tzinfo=M.IST))[0], "Tokyo")
check("14:00 -> London", A(dt.datetime(2026, 9, 28, 14, 0, tzinfo=M.IST))[0], "London")
check("20:00 -> New York", A(dt.datetime(2026, 9, 28, 20, 0, tzinfo=M.IST))[0], "New York")
check("02:00 -> none (after NY, before Tokyo)", A(dt.datetime(2026, 9, 28, 2, 0, tzinfo=M.IST))[0], None)
check("00:30 -> New York (past midnight)", A(dt.datetime(2026, 9, 29, 0, 30, tzinfo=M.IST))[0],
      "New York")
nap = M.seconds_to_next_open(dt.datetime(2026, 9, 28, 2, 0, tzinfo=M.IST))
check("sleeps until 05:30 Tokyo", 12500 < nap < 12700, True)

print("Halt logic — a streak halts, and the halt LIFTS")
st = {"day": "2026-09-28", "halted": True, "halt_through": "2026-09-29", "consec_losses": 4,
      "day_pnl": 0, "done_sessions": [], "trade": None}
check("still halted on the last halted day",
      notify.roll_day(dict(st), "2026-09-29")["halted"], True)
r = notify.roll_day(dict(st), "2026-09-30")
check("lifts the day after", r["halted"], False)
check("streak counter restarts", r["consec_losses"], 0)

print("Paper trading — the management loop, driven by crafted candles")
STEP = 15 * 60 * 1000
T0 = int(dt.datetime(2026, 9, 28, 14, 0, tzinfo=M.IST).timestamp() * 1000)
DAY_END_MS = M.ms(M.day_end_for(dt.datetime(2026, 9, 28, 14, 0, tzinfo=M.IST)))


def mk_trade(d=1, entry=100000.0, sl=98500.0, qty=0.01):
    risk_px = abs(entry - sl)
    tp_levels = [entry + d * r * risk_px for r in B.SA134_TP_R]
    from sorb15 import sizing as _sz
    leg_qtys, remaining = [], qty
    for frac in B.SA134_TP_FRACS[:-1]:
        q = _sz.floor_step(qty * frac, C.QTY_STEP)
        leg_qtys.append(q)
        remaining = round(remaining - q, 8)
    leg_qtys.append(round(remaining, 8))
    return {"symbol": "BTC", "pair": "B-BTC_USDT", "sub_agent": "SA1", "setup": "x",
            "session": "London", "direction": d,
            "level": entry, "entry": entry, "sl": sl, "qty": qty, "open_qty": qty,
            "risk_px": risk_px, "realised": 0.0, "seen": {}, "missing_polls": 0,
            "position_id": None, "entry_bar": T0, "opened_at": 0, "client_id": "t",
            "range_high": entry, "range_low": sl, "tp_levels": tp_levels,
            "leg_qtys": leg_qtys, "tp_hit": [False, False, False], "day_end_ms": DAY_END_MS}


def run_manage(trade, candle_lists, now_dt, sent):
    notify.send = lambda text, silent=False: sent.append(text)
    st = {"day_pnl": 0.0, "consec_losses": 0, "halted": False}
    out = trade
    for cs in candle_lists:
        ex.candles = lambda *a, _cs=cs, **k: _cs
        M.now_ist = lambda: now_dt
        out = M.manage(out, st) if out else None
    return out, st


real_send, real_candles, real_now = notify.send, ex.candles, M.now_ist
NOW = dt.datetime(2026, 9, 28, 14, 20, tzinfo=M.IST)

sent = []
cs = [K(100000, 100100, 99900, 100050, T0)]
t, st = run_manage(mk_trade(), [cs], NOW, sent)
check("quiet candle: trade stays open", t is not None, True)

sent = []
cs1 = [K(100000, 103100, 99950, 103050, T0)]                     # through +2R: target 1 fires
t, st = run_manage(mk_trade(), [cs1], NOW, sent)
check("target 1 (2R) books the first leg", t["tp_hit"][0], True)
check("the stop itself never moves off entry's 1.5%-buffered level", t["sl"], 98500.0)
check("the booked leg's size remains open", round(t["open_qty"], 4),
      round(0.01 - mk_trade()["leg_qtys"][0], 4))

sent = []
cs_all = [K(100000, 106100, 99950, 106050, T0)]                  # through +4R: every leg fires at once
t, st = run_manage(mk_trade(), [cs_all], NOW, sent)
check("all three targets hit -> trade closes", t, None)
check("closed as all_targets_hit", any("all_targets_hit" in m for m in sent), True)

sent = []
low_first = [K(100000, 100300, 98300, 100250, T0)]               # a fresh low through the fixed stop
t, st = run_manage(mk_trade(), [low_first], NOW, sent)
check("a low through the fixed stop closes it (no trail to hide behind)", t, None)
check("a CLOSED message was sent", any("CLOSED" in m for m in sent), True)

sent = []
at_day_end = dt.datetime(2026, 9, 29, 5, 29, 30, tzinfo=M.IST)   # inside the last minute before day_end
t, st = run_manage(mk_trade(), [[K(100000, 100100, 99900, 100050, T0)]], at_day_end, sent)
check("day end (next Tokyo open) flattens a paper trade that hit neither", t, None)
check("closed as day_end", any("day_end" in m for m in sent), True)

print("Pivot profit-booking (live) — mirrors the backtest's _sa134_manage_pivot_booking")
_pb_pivots_live = {"P": 99000.0, "R1": 100697.0, "R2": 101500.0, "R3": 102500.0,
                   "S1": 97000.0, "S2": 96000.0, "S3": 95000.0}
check("pivot_booking_due: direct scan finds the confirmed pattern",
      M.pivot_booking_due([
          K(100050, 100200, 99950, 100100, T0),
          K(100100, 100800, 100050, 100700, T0 + STEP),      # at_pivot, in profit
          K(100700, 101200, 100500, 100600, T0 + 2 * STEP),  # reversal-shaped
          K(100600, 100650, 100350, 100400, T0 + 3 * STEP),  # follow-through
      ], 1, 100000.0, 1500.0, _pb_pivots_live), True)
check("pivot_booking_due: pivots=None never fires", M.pivot_booking_due([
    K(100050, 100200, 99950, 100100, T0), K(100100, 100800, 100050, 100700, T0 + STEP),
    K(100700, 101200, 100500, 100600, T0 + 2 * STEP), K(100600, 100650, 100350, 100400, T0 + 3 * STEP),
], 1, 100000.0, 1500.0, None), False)
check("pivot_booking_due: entry already at the pivot (zero real profit) does not fire -- 1 Nov Tokyo bug",
      M.pivot_booking_due([
          K(100690, 100700, 100650, 100680, T0),             # entry candle, barely below R1
          K(100680, 101000, 100600, 100650, T0 + STEP),      # reversal-shaped, but no real profit yet
          K(100650, 100700, 100500, 100550, T0 + 2 * STEP),  # "follow-through", still no profit
      ], 1, 100690.0, 1500.0, _pb_pivots_live), False)

with C.override(SA134_PIVOT_PROFIT_BOOKING=True):
    t = mk_trade()
    t["pivots"] = _pb_pivots_live
    cs_book = [
        K(100050, 100200, 99950, 100100, T0),
        K(100100, 100800, 100050, 100700, T0 + STEP),
        K(100700, 101200, 100500, 100600, T0 + 2 * STEP),
        K(100600, 100650, 100350, 100400, T0 + 3 * STEP),
        K(100400, 100450, 100300, 100350, T0 + 4 * STEP),    # currently forming -- not scanned for the pattern
    ]
    sent = []
    out, st = run_manage(t, [cs_book], NOW, sent)
    check("live manage(): pivot profit-booking closes the trade", out, None)
    check("live manage(): closed as pivot_profit_booking", any("pivot_profit_booking" in m for m in sent), True)

sent = []
t3 = mk_trade()
t3["pivots"] = _pb_pivots_live
out, st = run_manage(t3, [cs_book], NOW, sent)
check("feature OFF by default: identical candles do NOT book early", out is not None, True)

notify.send, ex.candles, M.now_ist = real_send, real_candles, real_now


print("Exchange client — request shape, checked against a mocked HTTP layer")
import hashlib, hmac, json
import requests as _rq


class FakeResp:
    def __init__(self, js, code=200):
        self._js, self.status_code, self.text = js, code, json.dumps(js)

    def json(self):
        return self._js


calls, reply = [], {"v": []}
real_request = ex._session.request


def fake_request(method, url, data=None, headers=None, timeout=None):
    calls.append((method, url, data, headers))
    if isinstance(reply["v"], Exception):
        raise reply["v"]
    return FakeResp(reply["v"])


ex._session.request = fake_request
with C.override(DRY_RUN=False, COINDCX_API_KEY="KEY", COINDCX_API_SECRET="SECRET"):
    calls.clear(); reply["v"] = [{"currency_short_name": "INR", "balance": "4000", "locked_balance": "1000"},
                                 {"currency_short_name": "USDT", "balance": "0", "locked_balance": "0"}]
    w = ex.wallets()
    m, url, data, hdr = calls[0]
    check("wallet is a GET (it was a POST — that was the 404)", m, "GET")
    check("wallet path", url.endswith("/exchange/v1/derivatives/futures/wallets"), True)
    check("GET still carries a signed JSON body", "timestamp" in json.loads(data), True)
    check("signature is HMAC-SHA256 of that exact body",
          hdr["X-AUTH-SIGNATURE"], hmac.new(b"SECRET", data.encode(), hashlib.sha256).hexdigest())
    check("api key header sent", hdr["X-AUTH-APIKEY"], "KEY")
    check("wallet total = balance + locked", w["INR"]["total"], 5000.0)
    check("both wallets returned", sorted(w), ["INR", "USDT"])

    calls.clear(); reply["v"] = [{"id": "o1"}]
    ex.market_order("buy", 0.0123456)
    m, url, data, hdr = calls[0]
    body = json.loads(data)
    check("order create is a POST to /orders/create",
          (m, url.endswith("/futures/orders/create")), ("POST", True))
    o = body["order"]
    check("order is wrapped in {'order': ...}", "order" in body, True)
    check("market_order type", o["order_type"], "market_order")
    check("quantity floored to the exchange step", o["total_quantity"], 0.012)
    check("margin currency, margin type and leverage sent",
          (o["margin_currency_short_name"], o["position_margin_type"], o["leverage"]),
          (C.MARGIN_CURRENCY, "isolated", C.LEVERAGE))
    check("time_in_force sent", o["time_in_force"], "good_till_cancel")
    check("undocumented fields NOT sent", "reduce_only" in o or "client_order_id" in o, False)

    calls.clear(); reply["v"] = _rq.ConnectionError("reply lost")
    try:
        ex.market_order("buy", 0.01)
    except ex.CoinDCXError:
        pass
    check("an order is NEVER retried (a retry could double the position)", len(calls), 1)

    calls.clear(); reply["v"] = {"status": "ok"}
    ex.set_stop("P1", 99500.123)
    m, url, data, _ = calls[0]
    b = json.loads(data)
    check("stop goes on the POSITION via create_tpsl", url.endswith("/positions/create_tpsl"), True)
    check("stop body", (b["id"], b["stop_loss"]["order_type"], b["stop_loss"]["stop_price"]),
          ("P1", "stop_market", "99500.1"))

    calls.clear(); reply["v"] = {"status": "ok"}
    ex.exit_position("P1")
    check("full close uses the exit endpoint", calls[0][1].endswith("/positions/exit"), True)

    reply["v"] = [{"id": "p9", "pair": "B-BTC_USDT", "active_pos": -0.005, "avg_price": "81000",
                   "stop_loss_trigger": "81500"},
                  {"id": "p8", "pair": "B-ETH_USDT", "active_pos": 1.0, "avg_price": "3000"},
                  {"id": "p7", "pair": "B-BTC_USDT", "active_pos": 0, "avg_price": "0"}]
    p = ex.open_position()
    check("position: right pair, ignores flat ones", (p["id"], p["side"], p["qty"]), ("p9", "SHORT", 0.005))
    check("position exposes the stop the exchange holds", p["stop"], 81500.0)

with C.override(DRY_RUN=True):
    calls.clear()
    ex.market_order("buy", 0.01); ex.set_stop("P1", 1.0); ex.exit_position("P1"); ex.cancel("x")
    check("DRY_RUN sends nothing to the exchange", len(calls), 0)
ex._session.request = real_request

print("Sizing against the real contract spec (step 0.001, min 0.001, min notional 60)")
from sorb15 import sizing
check("floors to the step, never rounds up", sizing.floor_step(0.0047, 0.001), 0.004)
check("an exact multiple is unchanged", sizing.floor_step(0.007, 0.001), 0.007)
check("price lands on the tick", sizing.price(99500.123), 99500.1)
check("partial of 0.004 -> 0.002 (50%)", sizing.partial_qty(0.004), 0.002)
check("partial of 0.003 -> 0.001 (a third, the nearest valid split)", sizing.partial_qty(0.003), 0.001)
check("partial of 0.002 -> 0.001", sizing.partial_qty(0.002), 0.001)
check("0.001 cannot be split: no partial", sizing.partial_qty(0.001), 0.0)
check("a stop too wide for the minimum size -> 0, trade skipped", sizing.qty_for(100000, 98000), 0.0)
with C.override(MIN_NOTIONAL_USDT=1000.0):
    check("below the minimum notional -> 0", sizing.qty_for(100000, 99800), 0.0)
check("capped by max notional, then floored", sizing.qty_for(100000, 99990),
      sizing.floor_step((C.MAX_NOTIONAL_INR / C.USDINR) / 100000, C.QTY_STEP))

print("Spec sync — each symbol reads its OWN exchange spec, not a hand-typed value")
real_inst, real_send2 = ex.instrument, notify.send
msgs = []
notify.send = lambda text, silent=False: msgs.append(text)
ex.instrument = lambda *a, **k: {"quantity_increment": 0.01, "price_increment": 0.5,
                                 "min_trade_size": 0.01, "min_quantity": 0.01, "min_notional": 100.0}
spec = M.spec_for("B-XAU_USDT")
check("step, tick, min size and min notional taken from the exchange",
      (spec["QTY_STEP"], spec["PRICE_STEP"], spec["MIN_QTY"], spec["MIN_NOTIONAL_USDT"]),
      (0.01, 0.5, 0.01, 100.0))
check("global config is untouched -- each symbol's spec is its own, not shared",
      C.QTY_STEP, 0.001)
specs = M.sync_specs()
check("one spec per symbol", sorted(specs), sorted(M.SYMBOLS))
check("and you are told what was read", any("Contract specs" in m for m in msgs), True)
def no_spec(*a, **k):
    raise RuntimeError("network down")
ex.instrument = no_spec
check("if a symbol's spec cannot be read, BTC's configured values are kept for it",
      M.spec_for("B-XAU_USDT"),
      {"QTY_STEP": C.QTY_STEP, "PRICE_STEP": C.PRICE_STEP,
       "MIN_QTY": C.MIN_QTY, "MIN_NOTIONAL_USDT": C.MIN_NOTIONAL_USDT})
ex.instrument, notify.send = real_inst, real_send2

print("Entry under failure — live path with a fake exchange")
real = {k: getattr(ex, k) for k in ("market_order", "open_position", "set_stop", "exit_position")}
real_send, real_sleep = notify.send, M.time.sleep
M.time.sleep = lambda s: None
log_msgs, closed = [], []


def make_sig():
    return {"sub_agent": "SA1", "setup": "x", "direction": 1, "level": 100000.0, "trigger_i": 0,
            "breakout": True, "rejection": False, "momentum_shift": False, "detail": "t"}


SPEC = {"QTY_STEP": C.QTY_STEP, "PRICE_STEP": C.PRICE_STEP,
        "MIN_QTY": C.MIN_QTY, "MIN_NOTIONAL_USDT": C.MIN_NOTIONAL_USDT}


def live_enter(market=None, position=None, stop=None):
    log_msgs.clear(); closed.clear()
    notify.send = lambda text, silent=False: log_msgs.append(text)
    ex.market_order = market or (lambda *a, **k: {"ok": 1})
    ex.open_position = position or (lambda *a, **k: None)
    ex.set_stop = stop or (lambda pid, px: {"ok": 1})
    ex.exit_position = lambda pid: closed.append(pid) or {"ok": 1}
    now = M.now_ms()
    sess = [K(100000, 100100, 99950, 100020, now - STEP - 5000)]     # trigger closed 5s ago
    rc = {"high": 100000.0, "low": 99000.0}
    with C.override(DRY_RUN=False):
        return M.enter("BTC", "B-BTC_USDT", SPEC, make_sig(), "London", rc, sess)


pos_ok = lambda *a, **k: {"id": "P1", "entry": 100030.0, "qty": 0.006,
                         "raw": {"stop_loss_trigger": 99850.0}}
t = live_enter(position=pos_ok)
check("healthy entry: trade opened", t is not None, True)
check("uses the REAL fill price, not the candle close", t and t["entry"], 100030.0)
check("keeps the exchange position id", t and t["position_id"], "P1")

t = live_enter(position=lambda *a, **k: {"id": "P1", "entry": 100030.0, "qty": 0.006,
                                        "raw": {"stop_loss_trigger": 0}})
check("stop the exchange does not show -> position closed, no trade", (t, closed), (None, ["P1"]))
check("and you are told", any("STOP NOT CONFIRMED" in m for m in log_msgs), True)

def boom(pid, px):
    raise RuntimeError("tpsl rejected")
t = live_enter(position=pos_ok, stop=boom)
check("stop call raises -> position closed", (t, closed), (None, ["P1"]))

def lost_reply(*a, **k):
    raise RuntimeError("timeout")
t = live_enter(market=lost_reply, position=pos_ok)
check("order errored but the position exists -> it is protected, not abandoned",
      (t is not None, any("Protecting it" in m for m in log_msgs)), (True, True))
t = live_enter(market=lost_reply, position=lambda *a, **k: None)
check("order errored and nothing exists -> clean AVOID", (t, closed), (None, []))

stale = M.now_ms()
sess = [K(100000, 100100, 99950, 100020, stale - STEP - 300000)]       # signal is 5 minutes old
log_msgs.clear(); notify.send = lambda text, silent=False: log_msgs.append(text)
with C.override(DRY_RUN=False):
    t = M.enter("BTC", "B-BTC_USDT", SPEC, make_sig(), "London",
               {"high": 100000.0, "low": 99000.0}, sess)
check("a stale signal is skipped, never chased",
      (t, any("Missed the entry window" in m for m in log_msgs)), (None, True))
for k, v in real.items():
    setattr(ex, k, v)
notify.send, M.time.sleep = real_send, real_sleep

print("Order-book overlay — live-only (cannot be backtested); off by default, fails open")
real_ob = ex.orderbook
check("nearest_wall: finds the one level that stands out",
      ex.nearest_wall([(100, 1), (99, 1.1), (98, 0.9), (97, 1), (96, 1),
                        (95, 1), (94, 1), (93, 1), (92, 1), (91, 12.0)], mult=5.0),
      (91, 12.0))
check("nearest_wall: nothing stands out -> None",
      ex.nearest_wall([(100, 1), (99, 1.1), (98, 0.9), (97, 1), (96, 1),
                        (95, 1), (94, 1), (93, 1), (92, 1), (91, 1.2)], mult=5.0), None)
check("nearest_wall: too few levels to judge -> None",
      ex.nearest_wall([(100, 50), (99, 1)], mult=5.0), None)

flat = [(100000 - i * 10, 1.0) for i in range(1, 15)]     # no symbol's own price, just a flat book


def ob_with_wall(side, px, qty):
    book = {"bids": list(flat), "asks": list(flat)}
    book[side] = [(px, qty)] + [lv for lv in book[side] if lv[0] != px]
    return book


real.update({k: getattr(ex, k) for k in ("market_order", "open_position", "set_stop", "exit_position")})
notify.send = lambda text, silent=False: log_msgs.append(text)
ex.market_order = lambda *a, **k: {"ok": 1}
ex.open_position = pos_ok
ex.set_stop = lambda pid, px: {"ok": 1}
ex.exit_position = lambda pid: closed.append(pid) or {"ok": 1}
M.time.sleep = lambda s: None

now = M.now_ms()
sess_ob = [K(100000, 100100, 99950, 100020, now - STEP - 5000)]
rc_ob = {"high": 100000.0, "low": 99000.0}

log_msgs.clear()
ex.orderbook = lambda pair=None: ob_with_wall("asks", 100020 * 1.0005, 50.0)   # right at entry
with C.override(ORDERBOOK_FILTER=True, ORDERBOOK_WALL_MULT=5.0, ORDERBOOK_GATE_PCT=0.003):
    wall = M._orderbook_wall_against("B-BTC_USDT", 1, 100020)
check("_orderbook_wall_against finds a wall right against the entry",
      wall is not None and round(wall[1], 1), 50.0)
with C.override(ORDERBOOK_FILTER=False):
    check("same wall ignored with the feature off (default)",
          M._orderbook_wall_against("B-BTC_USDT", 1, 100020), None)
with C.override(ORDERBOOK_FILTER=True, ORDERBOOK_WALL_MULT=5.0, ORDERBOOK_GATE_PCT=0.0001):
    check("same wall ignored once it's outside the proximity threshold",
          M._orderbook_wall_against("B-BTC_USDT", 1, 100020), None)


def _boom(pair=None):
    raise AssertionError(f"orderbook() must not be called for unsupported pair {pair}")


ex.orderbook = _boom
with C.override(ORDERBOOK_FILTER=True, ORDERBOOK_WALL_MULT=5.0, ORDERBOOK_GATE_PCT=0.003):
    check("_orderbook_wall_against skips the HTTP call entirely for a non-BTC pair (always 422 on CoinDCX)",
          M._orderbook_wall_against("B-NATGAS_USDT", 1, 100020), None)
    check("_orderbook_stop skips the HTTP call entirely for a non-BTC pair too",
          M._orderbook_stop("B-NATGAS_USDT", 1, 100020, 99000)[0], 99000)
ex.orderbook = lambda pair=None: ob_with_wall("asks", 100020 * 1.0005, 50.0)   # restore

print("Liquidity-hunt reversal — a wall gets WATCHED (sweep + reverse), not avoided")
state_h = {"halted": False, "day_pnl": 0.0, "consec_losses": 0, "trades": {}}
real.update({k: getattr(ex, k) for k in ("market_order", "open_position", "set_stop", "exit_position")})
notify.send = lambda text, silent=False: log_msgs.append(text)
ex.market_order = lambda *a, **k: {"ok": 1}
# entry=None -> enter_hunt_reversal() falls back to its own computed entry_px,
# so this mock works at any price scale instead of assuming BTC's ~100,000
ex.open_position = lambda *a, **k: {"id": "P1", "entry": None, "qty": 1.0, "raw": {}}
ex.set_stop = lambda pid, px: {"ok": 1}
ex.exit_position = lambda pid: closed.append(pid) or {"ok": 1}
M.time.sleep = lambda s: None

log_msgs.clear()
state_h = {"halted": False, "day_pnl": 0.0, "consec_losses": 0, "trades": {}}
with C.override(DRY_RUN=False):
    M.register_hunt_watch(state_h, "BTC", "B-BTC_USDT", 1, 100100.0, 50.0, "London",
                          100000.0, 99000.0, M.now_ms() + 3_600_000)
check("register_hunt_watch stores it and notifies instead of entering",
      ("BTC" in state_h["hunt_watches"], any("watching for a sweep" in m for m in log_msgs)),
      (True, True))

# an expired watch is dropped with no trade, no crash
state_h2 = {"halted": False, "day_pnl": 0.0, "consec_losses": 0, "trades": {},
           "hunt_watches": {"BTC": {"pair": "B-BTC_USDT", "d0": 1, "wall_px": 100100.0,
                                    "wall_qty": 50.0, "session": "London", "H": 100000.0,
                                    "L": 99000.0, "registered_at": M.now_ms() - 10000,
                                    "expires_at": M.now_ms() - 1000}}}
log_msgs.clear()
with C.override(DRY_RUN=False):
    got = M.check_hunt_watches(state_h2, {"BTC": SPEC}, 2)
check("an expired watch is dropped, no trade",
      (got, "BTC" in state_h2["hunt_watches"], any("expired" in m for m in log_msgs)),
      ({}, False, True))


def mk_vol_candle(o, h, l, c, v, t):
    return {"t": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


# 20 quiet candles (needed lookback for good_volume), then a high-volume sweep
# ABOVE a 100.0 wall (original breakout was LONG, d0=1), then a doji right after
# -- confirms the reversal, should enter SHORT (the reverse of d0).
base_t = M.now_ms() - 25 * 900_000
quiet = [mk_vol_candle(96 + i * 0.05, 96.3 + i * 0.05, 95.8 + i * 0.05, 96.1 + i * 0.05, 10.0,
                       base_t + i * 900_000) for i in range(20)]
sweep = mk_vol_candle(99.5, 103.0, 99.4, 102.0, 500.0, base_t + 20 * 900_000)     # closes above 100.0 wall, huge volume
confirm = mk_vol_candle(102.0, 104.0, 101.9, 102.05, 10.0, base_t + 21 * 900_000)  # doji
hunt_candles = quiet + [sweep, confirm]

state_h3 = {"halted": False, "day_pnl": 0.0, "consec_losses": 0, "trades": {},
           "hunt_watches": {"BTC": {"pair": "B-BTC_USDT", "d0": 1, "wall_px": 100.0,
                                    "wall_qty": 50.0, "session": "London", "H": 105.0,
                                    "L": 95.0, "registered_at": base_t,
                                    "expires_at": M.now_ms() + 3_600_000}}}
ex.candles = lambda *a, _cs=hunt_candles, **k: _cs
log_msgs.clear()
with C.override(DRY_RUN=False):
    got = M.check_hunt_watches(state_h3, {"BTC": SPEC}, 2)
check("sweep above the wall + doji confirms -> enters the REVERSE (SHORT) direction",
      ("BTC" in got, got.get("BTC", {}).get("direction")), (True, -1))
check("the watch is cleared once it resolves", "BTC" in state_h3["hunt_watches"], False)

# same setup but no reversal candle after the sweep -- stays pending, no trade
no_confirm = mk_vol_candle(102.0, 108.0, 101.0, 106.0, 10.0, base_t + 21 * 900_000)  # strong green, not a reversal
state_h4 = {"halted": False, "day_pnl": 0.0, "consec_losses": 0, "trades": {},
           "hunt_watches": {"BTC": {"pair": "B-BTC_USDT", "d0": 1, "wall_px": 100.0,
                                    "wall_qty": 50.0, "session": "London", "H": 105.0,
                                    "L": 95.0, "registered_at": base_t,
                                    "expires_at": M.now_ms() + 3_600_000}}}
ex.candles = lambda *a, _cs=quiet + [sweep, no_confirm], **k: _cs
with C.override(DRY_RUN=False):
    got = M.check_hunt_watches(state_h4, {"BTC": SPEC}, 2)
check("no reversal candle yet -> still pending, no trade",
      (got, "BTC" in state_h4["hunt_watches"]), ({}, True))

# a budget of 0 free slots blocks entry even if the pattern already confirmed
state_h5 = {"halted": False, "day_pnl": 0.0, "consec_losses": 0, "trades": {},
           "hunt_watches": {"BTC": {"pair": "B-BTC_USDT", "d0": 1, "wall_px": 100.0,
                                    "wall_qty": 50.0, "session": "London", "H": 105.0,
                                    "L": 95.0, "registered_at": base_t,
                                    "expires_at": M.now_ms() + 3_600_000}}}
ex.candles = lambda *a, _cs=hunt_candles, **k: _cs
with C.override(DRY_RUN=False):
    got = M.check_hunt_watches(state_h5, {"BTC": SPEC}, 0)
check("zero free slots -> no entry even though the pattern confirmed", got, {})

base_sl = rc_ob["low"]   # the stop is the range candle's own opposite extreme now, no buffer
wall_px = base_sl * (1 - 0.003)      # a bit further out than the baseline stop, same side
# a handful of ordinary resting levels inside the search band too, so the wall
# has something to stand out against (a real book is thin this far from price)
near_sl_bids = [(base_sl * (1 - 0.001 * i), 1.0) for i in range(1, 8)] + [(wall_px, 50.0)]
ex.orderbook = lambda pair=None: {"bids": near_sl_bids, "asks": list(flat)}
with C.override(DRY_RUN=False, ORDERBOOK_FILTER=True, ORDERBOOK_WALL_MULT=5.0):
    t = M.enter("BTC", "B-BTC_USDT", SPEC, make_sig(), "London", rc_ob, sess_ob)
check("a protective-side wall WIDENS the stop, trade still opens",
      (t is not None, t and t["sl"] < base_sl), (True, True))

ex.orderbook = lambda pair=None: {"bids": list(flat), "asks": list(flat)}   # no wall anywhere
with C.override(DRY_RUN=False, ORDERBOOK_FILTER=True, ORDERBOOK_WALL_MULT=5.0):
    t = M.enter("BTC", "B-BTC_USDT", SPEC, make_sig(), "London", rc_ob, sess_ob)
check("no wall found -> baseline stop unchanged", t and round(t["sl"], 6), round(base_sl, 6))


def boom_ob(pair=None):
    raise RuntimeError("timeout")


ex.orderbook = boom_ob
with C.override(DRY_RUN=False, ORDERBOOK_FILTER=True, ORDERBOOK_WALL_MULT=5.0):
    t = M.enter("BTC", "B-BTC_USDT", SPEC, make_sig(), "London", rc_ob, sess_ob)
check("order-book fetch failing never blocks the trade (fails open)",
      (t is not None, t and round(t["sl"], 6)), (True, round(base_sl, 6)))

ex.orderbook = real_ob
ex.candles = real_candles
for k, v in real.items():
    setattr(ex, k, v)
notify.send, M.time.sleep = real_send, real_sleep

print("all_sessions() — must read C.SESSIONS live, not freeze it at import")
default_london_open = M.all_sessions()["London"]["open"]
shifted = {"London": {"open": dt.time(15, 0), "deadline": dt.time(16, 45), "end": dt.time(20, 0)},
           "New York": {"open": dt.time(20, 0), "deadline": dt.time(21, 45), "end": dt.time(2, 15)}}
with C.override(SESSIONS=shifted):
    check("an override on C.SESSIONS is picked up inside the with-block",
          M.all_sessions()["London"]["open"], dt.time(15, 0))
check("and the override doesn't leak out afterwards",
      M.all_sessions()["London"]["open"], default_london_open)
check("Tokyo always comes from backtest.TOKYO_SESSION, unaffected by C.SESSIONS",
      M.all_sessions()["Tokyo"], B.TOKYO_SESSION)

print("ACTIVE_SYMBOLS — splitting the 5-symbol roster across two services")
with C.override(ACTIVE_SYMBOLS=""):
    check("empty (default): every symbol", set(M.active_symbols()), set(B.SA134_SYMBOLS))
with C.override(ACTIVE_SYMBOLS="BTC,Oil,NatGas"):
    check("a real-money subset", M.active_symbols(),
          {"BTC": "B-BTC_USDT", "Oil": "B-CL_USDT", "NatGas": "B-NATGAS_USDT"})
with C.override(ACTIVE_SYMBOLS="Gold, Silver"):
    check("the paper-only remainder (spaces tolerated)", M.active_symbols(),
          {"Gold": "B-XAU_USDT", "Silver": "B-XAG_USDT"})
with C.override(ACTIVE_SYMBOLS="BTC,Dogecoin"):
    try:
        M.active_symbols()
        check("an unknown symbol name raises rather than silently trading nothing", False, True)
    except ValueError as e:
        check("an unknown symbol name raises rather than silently trading nothing",
              "Dogecoin" in str(e), True)

print("Companion-pairs slot policy — Gold/Silver solo-only, BTC/Oil/NatGas pairwise")
check("first position ever (nothing open) is always allowed",
      B.compatible_with_open(set(), "Gold"), True)
check("BTC may join Oil", B.compatible_with_open({"Oil"}, "BTC"), True)
check("BTC may join NatGas", B.compatible_with_open({"NatGas"}, "BTC"), True)
check("Oil may join NatGas", B.compatible_with_open({"NatGas"}, "Oil"), True)
check("Gold may NOT join BTC", B.compatible_with_open({"BTC"}, "Gold"), False)
check("Silver may NOT join Oil", B.compatible_with_open({"Oil"}, "Silver"), False)
check("BTC may NOT join Gold", B.compatible_with_open({"Gold"}, "BTC"), False)
check("Gold may NOT join Silver", B.compatible_with_open({"Silver"}, "Gold"), False)

print("Daily pivot confluence for SA1/SA4 — fully backtestable, OFF by default")
_tokyo_open_ms = lambda day: B._ms(B._at(day, B.TOKYO_SESSION["open"]))
_day0 = dt.date(2026, 1, 1)
_day1 = dt.date(2026, 1, 2)
_prior_day_candles = [
    K(100.0, 110.0, 95.0, 103.0, _tokyo_open_ms(_day0)),
    K(103.0, 105.0, 101.0, 104.0, _tokyo_open_ms(_day0) + STEP),
]
_pivot_candles = _prior_day_candles + [K(104.0, 106.0, 103.0, 105.0, _tokyo_open_ms(_day1))]
_by_t = {c["t"]: i for i, c in enumerate(_pivot_candles)}
_piv = B.daily_pivot_fib(_pivot_candles, _by_t, _day1)
# hand-computed: H=110, L=95, C=104 (last prior-day candle) -> P=(110+95+104)/3=103.0
check("daily_pivot_fib: P from prior day's H/L/C", round(_piv["P"], 4), round((110 + 95 + 104) / 3, 4))
check("daily_pivot_fib: R1 = P + 0.382*(H-L)",
      round(_piv["R1"], 4), round((110 + 95 + 104) / 3 + 0.382 * 15, 4))
check("daily_pivot_fib: S1 = P - 0.382*(H-L)",
      round(_piv["S1"], 4), round((110 + 95 + 104) / 3 - 0.382 * 15, 4))
check("daily_pivot_fib: no prior day in the data -> None (first day, can't judge)",
      B.daily_pivot_fib(_pivot_candles, _by_t, _day0), None)

check("_pivot_confluent: None pivots always passes (nothing to judge against)",
      B._pivot_confluent(85200, 1, None, 0.003), True)
_piv2 = {"P": 85200.0, "R1": 90000.0, "R2": 92000.0, "R3": 94000.0,
         "S1": 78000.0, "S2": 76000.0, "S3": 74000.0}
check("_pivot_confluent: LONG breakout level sitting exactly on P passes",
      B._pivot_confluent(85200, 1, _piv2, 0.003), True)
check("_pivot_confluent: LONG breakout far from every resistance pivot fails",
      B._pivot_confluent(85200, 1, {**_piv2, "P": 80000.0}, 0.003), False)
check("_pivot_confluent: SHORT breakout level sitting exactly on S1 passes",
      B._pivot_confluent(78000, -1, _piv2, 0.003), True)

with C.override(SA134_PIVOT_CONFLUENCE=False):
    s = B.sa134_entry_signal([K(85150, 85600, 85140, 85560)], H_, L_, DI)
    check("feature OFF (default): SA1 second-candle fires exactly as before",
          s and s["state"], "ready")
with C.override(SA134_PIVOT_CONFLUENCE=True):
    s = B.sa134_entry_signal([K(85150, 85600, 85140, 85560)], H_, L_, DI, pivots=None)
    check("feature ON, no prior-day pivots yet: doesn't block (nothing to judge)",
          s and s["state"], "ready")
    s = B.sa134_entry_signal([K(85150, 85600, 85140, 85560)], H_, L_, DI,
                              pivots={**_piv2, "P": 80000.0})
    check("feature ON, breakout level far from every resistance pivot: SA1 does NOT fire",
          s, None)
    s = B.sa134_entry_signal([K(85150, 85600, 85140, 85560)], H_, L_, DI,
                              pivots={**_piv2, "P": H_})
    check("feature ON, breakout level confluent with P: SA1 fires",
          s and s["state"], "ready")

print("Pivot profit-booking (SA134_PIVOT_PROFIT_BOOKING) — exit-only, OFF by default")
_PB_STEP = 900000
_pb_pivots = {"P": 99000.0, "R1": 100697.0, "R2": 101500.0, "R3": 102500.0,
              "S1": 97000.0, "S2": 96000.0, "S3": 95000.0}

with C.override(SA134_PIVOT_PROFIT_BOOKING=True):
    # pivots=None (no prior day yet) -- must reduce to a plain _sa134_manage trade
    _cs_plain = [K(100100, 100200, 99900, 100050, 0), K(100050, 103100, 100000, 103050, _PB_STEP)]
    chain = B._sa134_manage_pivot_booking(_cs_plain, 0, 1, 100000.0, 98500.0, 1.0,
                                           100 * _PB_STEP, None)
    plain = B._sa134_manage(_cs_plain, 0, 1, 100000.0, 98500.0, 1.0, 100 * _PB_STEP)
    check("pivots=None: exactly one leg, identical to plain _sa134_manage",
          (len(chain) == 1, chain[0]["exit_reason"], round(chain[0]["pnl_inr"], 2)),
          (True, plain["exit_reason"], round(plain["pnl_inr"], 2)))

    # genuine reversal: reaches R1, a reversal-shaped candle appears, and the
    # candle AFTER that genuinely follows through (closes further down) --
    # two-candle confirmation, not one
    _cs_reversal = [
        K(100100, 100200, 99900, 100050, 0 * _PB_STEP),    # quiet
        K(100050, 100800, 100000, 100700, 1 * _PB_STEP),   # pushes to R1 (100697) -> at_pivot
        K(100700, 101200, 100500, 100600, 2 * _PB_STEP),   # reversal-shaped (long upper wick)
        K(100600, 100650, 100350, 100400, 3 * _PB_STEP),   # follow-through: closes lower still
        K(100400, 100450, 100300, 100350, 4 * _PB_STEP),   # fill candle (its OPEN is the exit)
    ]
    chain = B._sa134_manage_pivot_booking(_cs_reversal, 0, 1, 100000.0, 98500.0, 1.0,
                                           100 * _PB_STEP, _pb_pivots)
    check("genuine reversal: pivot+2-candle confirmation books the trade",
          chain[0]["exit_reason"], "pivot_profit_booking")
    check("genuine reversal: pnl_inr > 0 (booked while still in profit)",
          chain[0]["pnl_inr"] > 0, True)

    # a reversal-shaped candle that DOESN'T follow through (price resumes the
    # original direction right after -- the 29 Oct Tokyo failure mode) must
    # NOT book early
    _cs_no_follow_through = [
        K(100100, 100200, 99900, 100050, 0 * _PB_STEP),
        K(100050, 100800, 100000, 100700, 1 * _PB_STEP),   # at_pivot
        K(100700, 101200, 100500, 100600, 2 * _PB_STEP),   # reversal-shaped
        K(100600, 101400, 100550, 101300, 3 * _PB_STEP),   # NO follow-through -- resumes UP
        K(101300, 101500, 101200, 101450, 4 * _PB_STEP),
    ]
    chain = B._sa134_manage_pivot_booking(_cs_no_follow_through, 0, 1, 100000.0, 98500.0, 1.0,
                                           100 * _PB_STEP, _pb_pivots)
    check("reversal-shaped candle with no follow-through: does not book early",
          chain[0]["exit_reason"] != "pivot_profit_booking", True)

    # the real bug found 9 Nov (1 Nov Tokyo / 4 Nov New York): entry sits
    # almost ON the pivot already, so "at a pivot" is satisfied immediately
    # with ~zero profit -- an ordinary post-entry wick must NOT book a LOSS
    # labelled "pivot_profit_booking". risk_px=1500, so +0.3R = 100690+450=
    # 101140; every high here stays below that, so the profit floor is never
    # cleared and the trade falls through to its ordinary day_end handling.
    _cs_entry_at_pivot = [
        K(100690, 100700, 100650, 100680, 0 * _PB_STEP),   # entry candle, barely below R1 (100697)
        K(100680, 101000, 100600, 100650, 1 * _PB_STEP),   # reversal-shaped wick, but no real profit yet
        K(100650, 100700, 100500, 100550, 2 * _PB_STEP),   # "follow-through" lower -- still no profit
        K(100550, 100600, 100500, 100550, 3 * _PB_STEP),
    ]
    chain = B._sa134_manage_pivot_booking(_cs_entry_at_pivot, 0, 1, 100690.0, 99190.0, 1.0,
                                           4 * _PB_STEP, _pb_pivots)
    check("entry already at the pivot, zero real profit: does not book a loss as 'profit booking'",
          chain[0]["exit_reason"] != "pivot_profit_booking", True)

print("Optional rules — the two trades flagged on the charts (all OFF by default)")
def cd(o, h, l, c, i=0):
    return {"t": i * 900000, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}

# 16 Oct London: range 82,065.0 - 82,196.5. The second candle straddles the high and closes just above it
# (54% of it beyond the line); the next candle is red and gives it all back.
H1, L1 = 82196.5, 82065.0
c2 = cd(82170, 82260, 82140, 82258, 0)               # touching: 47% of the candle is back inside the range
red = cd(82258, 82262, 82195, 82200, 1)
grn = cd(82258, 82330, 82250, 82320, 1)
det = lambda sess, H=H1, L=L1: sa.detect(sess, H, L, 5, "London", None)
s = det([c2])
check("baseline: the touching candle IS accepted as a breakout (this is what happened)",
      (s["sub_agent"], s["direction"], s["state"]), ("SA1", 1, "ready"))
with C.override(SA1_REQUIRE_CLEAN=True):
    check("SA1 clean-only: a candle touching the line is not a clear breakout", det([c2]), None)
    clean = cd(82230, 82300, 82205, 82290, 0)        # sits fully above the line
    check("SA1 clean-only: a candle that really is clear still qualifies", det([clean])["state"], "ready")
with C.override(VETO_MODE="next_candle"):
    check("next-candle veto: waits, does not enter on the signal candle", det([c2])["state"], "pending")
    check("next-candle veto: the red candle cancels the LONG", det([c2, red])["state"], "veto")
    s = det([c2, grn])
    check("next-candle veto: a candle that holds lets the trade through, one candle later",
          (s["state"], s["trigger_i"]), ("ready", 1))
with C.override(VETO_MODE="sa4_style"):
    check("SA4-style veto: an ordinary red candle is not a reversal candle -> keeps waiting", det([c2, red])["state"], "pending")
    check("SA4-style veto: window passes with no reversal -> ready", det([c2, red, grn])["state"], "ready")
    doji = cd(82258, 82300, 82220, 82256, 1)
    dn = cd(82256, 82257, 82150, 82160, 2)           # closes below the doji's low
    check("SA4-style veto: doji then a close past it cancels", det([c2, doji, dn])["state"], "veto")
with C.override(VETO_MODE="next_candle", VETO_AGENTS="SA2"):
    check("veto can be limited to chosen sub-agents (SA1 untouched)", det([c2])["state"], "ready")

# 15 Oct New York: range 80,027.0 - 80,719.4. The second candle tags the high, closes red, and also spikes
# far below the low. The very next candle is a huge green candle that closes far above the high.
H2, L2 = 80719.4, 80027.0
sweep = cd(80590, 80722, 79700, 80090, 0)
blast = cd(80090, 80990, 80080, 80980, 1)
det2 = lambda sess: sa.detect(sess, H2, L2, 5, "New York", None)
s = det2([sweep])
check("baseline: any red candle reaching the high is a 'rejection' -> SHORT (this is what happened)",
      (s["sub_agent"], s["direction"], s["detail"].split("rejected by ")[1]), ("SA2", -1, "bearish_close_at_high"))
with C.override(SA2_STRICT_REJECTION=True):
    check("SA2 strict: that candle is not a doji or a long-wick rejection -> no trade", det2([sweep]), None)
    pin = cd(80600, 80760, 80560, 80590, 0)          # a real upper-wick rejection of the high
    pin = cd(80560, 80900, 80540, 80580, 0)
    check("SA2 strict: a genuine upper-wick rejection still counts", det2([pin])["state"], "ready")
with C.override(VETO_MODE="next_candle"):
    check("next-candle veto: waits one candle after the SHORT signal", det2([sweep])["state"], "pending")
    check("next-candle veto: the green blast candle cancels the SHORT", det2([sweep, blast])["state"], "veto")

print("Optional rules — every variant stays causal and obeys the invariants")
import sys as _s
_s.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import synth
cs_ = synth.candles(seed=5, days=120)
base_tr, _ = B.run(cs_)
VARS = {"clean-only SA1": dict(SA1_REQUIRE_CLEAN=True), "strict SA2": dict(SA2_STRICT_REJECTION=True),
        "next-candle veto": dict(VETO_MODE="next_candle"), "SA4-style veto": dict(VETO_MODE="sa4_style"),
        "all three": dict(SA1_REQUIRE_CLEAN=True, SA2_STRICT_REJECTION=True, VETO_MODE="next_candle")}
for name, ov in VARS.items():
    tr_, sg_ = B.run(cs_, **ov)
    per = {}
    for x in tr_:
        per[(x["date"], x["session"])] = per.get((x["date"], x["session"]), 0) + 1
    n_, bad_ = B.audit_causality(cs_, tr_, sample=40, **ov)
    check(f"{name}: runs, at most one trade per session, future-scramble audit clean",
          (len(tr_) > 0, max(per.values()) == 1, len(bad_)), (True, True, 0))
check("the switches restore: back to the baseline afterwards", len(B.run(cs_)[0]), len(base_tr))
check("BASELINE FINGERPRINT unchanged (seed 11, 270 days) — no optional rule is leaking into the default",
      synth.signature(B.run(synth.candles())[0]), ("2f6ec4de24d1060c", 230))

print("End-to-end: the two flagged sessions replayed through the whole engine")
def day_candles(start, shape, n_total):
    """shape = the first few (o,h,l,c) candles; the rest drift flat so the session can run to its end."""
    out = []
    for i in range(n_total):
        if i < len(shape):
            o, h, l, c = shape[i]
        else:
            last = out[-1]["close"]
            o, h, l, c = last, last * 1.0002, last * 0.9998, last
        out.append({"t": start + i * 900000, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})
    return out
IST_ = B.IST
ms_ = lambda y, mo, d, h, mi: int(dt.datetime(y, mo, d, h, mi, tzinfo=IST_).timestamp() * 1000)

# Same pattern the real 16 Oct London ledger reproduced under the old 13:00 session open
# (now 14:00): range candle sets 82,065.0-82,196.5; the next candle touches the line and
# closes above it (SA1 LONG); the candle after that reverses; price then falls through the stop.
lon = day_candles(ms_(2026, 10, 16, 14, 0), [
    (82100, 82196.5, 82065.0, 82150),        # 14:00 the range candle
    (82170, 82260, 82140, 82258),            # 14:15 touching breakout
    (82258, 82262, 82195, 82200),            # 14:30 the reversal
    (82200, 82205, 82060, 82070),            # 14:45 down through the stop
], 28)
def outcome(candles, date, session, **ov):
    tr, sg = B.run(candles, **ov)
    rows = [x for x in sg if str(x["date"]) == date and x["session"] == session]
    if not rows: return "no record"
    x = rows[0]
    if x["action"] == "ENTER":
        t_ = next(t_ for t_ in tr if str(t_["date"]) == date and t_["session"] == session)
        return f"ENTER {t_['sub_agent']} {t_['side']} -> {t_['exit_reason']}"
    return "AVOID"
check("16 Oct London, baseline: SA1 LONG, stopped out (reproduces the ledger)",
      outcome(lon, "2026-10-16", "London"), "ENTER SA1 LONG -> stop_loss")
check("16 Oct London, clean-only SA1: no trade", outcome(lon, "2026-10-16", "London", SA1_REQUIRE_CLEAN=True), "AVOID")
check("16 Oct London, next-candle veto: no trade", outcome(lon, "2026-10-16", "London", VETO_MODE="next_candle"), "AVOID")

# 15 Oct New York. Range candle 19:00 sets 80,027.0-80,719.4; the 19:15 candle tags the high, closes red and
# spikes far below the low (SA2 SHORT); the 19:30 candle is a huge green candle through the high and the stop.
ny = day_candles(ms_(2026, 10, 15, 18, 45), [
    (80000, 80050, 79990, 80040),            # 18:45 (before the session)
    (80100, 80719.4, 80027.0, 80600),        # 19:00 the range candle
    (80590, 80722, 79700, 80090),            # 19:15 tags the high, closes red, spikes below the low
    (80090, 81100, 80080, 81080),            # 19:30 the blast candle through the high and the stop
], 36)
check("15 Oct New York, baseline: SA2 SHORT, stopped out (reproduces the ledger)",
      outcome(ny, "2026-10-15", "New York"), "ENTER SA2 SHORT -> stop_loss")
check("15 Oct New York, strict SA2: no trade", outcome(ny, "2026-10-15", "New York", SA2_STRICT_REJECTION=True), "AVOID")
check("15 Oct New York, next-candle veto: no trade", outcome(ny, "2026-10-15", "New York", VETO_MODE="next_candle"), "AVOID")

print("Backtest — causal, and it agrees with itself")
rnd = random.Random(7)
price, candles = 90000.0, []
t0 = int(dt.datetime(2026, 1, 1, 0, 0, tzinfo=M.IST).timestamp() * 1000)
for i in range(96 * 120):
    o = price
    c = o * (1 + rnd.gauss(0, 0.0025))
    h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0012)))
    l = min(o, c) * (1 - abs(rnd.gauss(0, 0.0012)))
    candles.append(K(o, h, l, c, t0 + i * STEP))
    price = c
trades, signals = B.run(candles)
check("produces trades", len(trades) > 50, True)
check("every session is accounted for", len(signals) >= len(trades), True)
check("never more than one trade per session",
      len({(t["date"], t["session"]) for t in trades}) == len(trades), True)
check("entries only inside the entry window",
      all(t["entry_time"].time() <= dt.time(20, 45) or t["session"] == "New York" or
          t["entry_time"].time() <= dt.time(14, 45) for t in trades), True)
check("every backtest trade is sized in whole exchange steps",
      all(abs(x["qty_btc"] / C.QTY_STEP - round(x["qty_btc"] / C.QTY_STEP)) < 1e-9 and
          x["qty_btc"] >= C.MIN_QTY - 1e-12 for x in trades), True)
check("and never risks more than the target",
      all(x["risk_inr"] <= C.RISK_PER_TRADE_INR * 1.0001 for x in trades), True)
n, bad = B.audit_causality(candles, trades)
check(f"future-scramble audit: {n} trades, 0 changed decisions", bad, [])
a, _ = B.run(candles, MOMENTUM_WINDOW=1)
b, _ = B.run(candles, MOMENTUM_WINDOW=3)
check("overrides take effect and restore", C.MOMENTUM_WINDOW, 2)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
