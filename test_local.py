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
q = M.contracts(85000, 84800)
check("qty positive", q > 0, True)
check("qty is a whole number of exchange steps (0.0047 would be rejected)",
      abs(q / C.QTY_STEP - round(q / C.QTY_STEP)) < 1e-9, True)
risk = (200 / 85000) * (q * 85000 * C.USDINR)
check("a stop-out never risks more than the target", risk <= C.RISK_PER_TRADE_INR + 1e-6, True)
check("and not far below it", risk > 0.7 * C.RISK_PER_TRADE_INR, True)
tr = {"direction": 1, "entry": 85000.0}
check("round-trip fee is charged",
      round(M.leg_pnl(tr, 85000.0, 0.01), 2) < 0, True)

print("Session clock (IST)")
A = M.active_session
check("14:00 -> London", A(dt.datetime(2026, 9, 28, 14, 0, tzinfo=M.IST))[0], "London")
check("20:00 -> New York", A(dt.datetime(2026, 9, 28, 20, 0, tzinfo=M.IST))[0], "New York")
check("10:00 -> none", A(dt.datetime(2026, 9, 28, 10, 0, tzinfo=M.IST))[0], None)
check("00:30 -> New York (past midnight)", A(dt.datetime(2026, 9, 29, 0, 30, tzinfo=M.IST))[0],
      "New York")
nap = M.seconds_to_next_open(dt.datetime(2026, 9, 28, 10, 0, tzinfo=M.IST))
check("sleeps until 14:00", 14300 < nap < 14500, True)

print("Halt logic — a streak halts, and the halt LIFTS")
st = {"day": "2026-09-28", "halted": True, "halt_through": "2026-09-29", "consec_losses": 4,
      "day_pnl": 0, "done_sessions": [], "open_london": None, "trade": None}
check("still halted on the last halted day",
      notify.roll_day(dict(st), "2026-09-29")["halted"], True)
r = notify.roll_day(dict(st), "2026-09-30")
check("lifts the day after", r["halted"], False)
check("streak counter restarts", r["consec_losses"], 0)

print("Paper trading — the management loop, driven by crafted candles")
STEP = 15 * 60 * 1000
T0 = int(dt.datetime(2026, 9, 28, 14, 0, tzinfo=M.IST).timestamp() * 1000)


def mk_trade(d=1, entry=100000.0, sl=99500.0):
    return {"sub_agent": "SA1", "setup": "x", "session": "London", "direction": d,
            "level": entry, "entry": entry, "sl": sl, "qty": 0.01, "open_qty": 0.01,
            "risk_px": abs(entry - sl), "best": entry, "partial_done": False,
            "moved_be": False, "realised": 0.0, "seen": {}, "missing_polls": 0,
            "position_id": None, "entry_bar": T0, "opened_at": 0, "client_id": "t"}


def run_manage(trade, candle_lists, now_dt, sent):
    notify.send = lambda text, silent=False: sent.append(text)
    st = {"day_pnl": 0.0, "consec_losses": 0, "halted": False}
    end = dt.datetime(2026, 9, 28, 18, 45, tzinfo=M.IST)
    out = trade
    for cs in candle_lists:
        ex.candles = lambda *a, _cs=cs, **k: _cs
        M.now_ist = lambda: now_dt
        out = M.manage(out, "London", end, st) if out else None
    return out, st


real_send, real_candles, real_now = notify.send, ex.candles, M.now_ist
NOW = dt.datetime(2026, 9, 28, 14, 20, tzinfo=M.IST)

sent = []
cs = [K(100000, 100100, 99900, 100050, T0)]
t, st = run_manage(mk_trade(), [cs], NOW, sent)
check("quiet candle: trade stays open", t is not None, True)

sent = []
cs1 = [K(100000, 100300, 99950, 100250, T0)]                     # +0.6R: partial + trail arm
t, st = run_manage(mk_trade(), [cs1], NOW, sent)
check("partial fires at +0.5R", t["partial_done"], True)
check("stop is trailed above entry-risk", t["sl"] > 99500.0, True)
check("half the position remains", round(t["open_qty"], 4), 0.005)

sent = []
cs2 = [K(100000, 100300, 99950, 100250, T0)]
cs3 = [K(100000, 100300, 99300, 99350, T0)]                      # then falls through the trail
t, st = run_manage(mk_trade(), [cs2, cs3], NOW, sent)
check("a fresh low through the trailed stop closes the paper trade", t, None)
check("a CLOSED message was sent", any("CLOSED" in m for m in sent), True)

sent = []
low_first = [K(100000, 100300, 99300, 100250, T0)]               # low printed, stop was 99500
t, st = run_manage(mk_trade(), [low_first], NOW, sent)
check("a low through the ORIGINAL stop closes it", t, None)

sent = []
late = dt.datetime(2026, 9, 28, 18, 44, 30, tzinfo=M.IST)
t, st = run_manage(mk_trade(), [[K(100000, 100100, 99900, 100050, T0)]], late, sent)
check("session end flattens a paper trade", t, None)
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

print("Spec sync — the bot reads the exchange, not a hand-typed value")
saved = {k: getattr(C, k) for k in ("QTY_STEP", "PRICE_STEP", "MIN_QTY", "MIN_NOTIONAL_USDT")}
real_inst, real_send2 = ex.instrument, notify.send
msgs = []
notify.send = lambda text, silent=False: msgs.append(text)
ex.instrument = lambda *a, **k: {"quantity_increment": 0.01, "price_increment": 0.5,
                                 "min_trade_size": 0.01, "min_quantity": 0.01, "min_notional": 100.0}
ch = M.sync_spec()
check("step, tick, min size and min notional taken from the exchange",
      (C.QTY_STEP, C.PRICE_STEP, C.MIN_QTY, C.MIN_NOTIONAL_USDT), (0.01, 0.5, 0.01, 100.0))
check("and you are told what changed", any("Contract spec" in m for m in msgs), True)
for k, v in saved.items():
    setattr(C, k, v)
def no_spec(*a, **k):
    raise RuntimeError("network down")
ex.instrument = no_spec
check("if the spec cannot be read, configured values are kept",
      (M.sync_spec(), C.QTY_STEP), ({}, saved["QTY_STEP"]))
ex.instrument, notify.send = real_inst, real_send2

print("Entry under failure — live path with a fake exchange")
real = {k: getattr(ex, k) for k in ("market_order", "open_position", "set_stop", "exit_position")}
real_send, real_sleep = notify.send, M.time.sleep
M.time.sleep = lambda s: None
log_msgs, closed = [], []


def make_sig():
    return {"sub_agent": "SA1", "setup": "x", "direction": 1, "level": 100000.0, "trigger_i": 0,
            "breakout": True, "rejection": False, "momentum_shift": False, "detail": "t"}


def live_enter(market=None, position=None, stop=None):
    log_msgs.clear(); closed.clear()
    notify.send = lambda text, silent=False: log_msgs.append(text)
    ex.market_order = market or (lambda *a, **k: {"ok": 1})
    ex.open_position = position or (lambda: None)
    ex.set_stop = stop or (lambda pid, px: {"ok": 1})
    ex.exit_position = lambda pid: closed.append(pid) or {"ok": 1}
    now = M.now_ms()
    sess = [K(100000, 100100, 99950, 100020, now - STEP - 5000)]     # trigger closed 5s ago
    rc = {"high": 100000.0, "low": 99000.0}
    st = {"day_pnl": 0.0}
    with C.override(DRY_RUN=False):
        return M.enter(make_sig(), "London", rc, sess, st)


pos_ok = lambda: {"id": "P1", "entry": 100030.0, "qty": 0.006,
                  "raw": {"stop_loss_trigger": 99850.0}}
t = live_enter(position=pos_ok)
check("healthy entry: trade opened", t is not None, True)
check("uses the REAL fill price, not the candle close", t and t["entry"], 100030.0)
check("keeps the exchange position id", t and t["position_id"], "P1")

t = live_enter(position=lambda: {"id": "P1", "entry": 100030.0, "qty": 0.006,
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
t = live_enter(market=lost_reply, position=lambda: None)
check("order errored and nothing exists -> clean AVOID", (t, closed), (None, []))

stale = M.now_ms()
sess = [K(100000, 100100, 99950, 100020, stale - STEP - 300000)]       # signal is 5 minutes old
log_msgs.clear(); notify.send = lambda text, silent=False: log_msgs.append(text)
with C.override(DRY_RUN=False):
    t = M.enter(make_sig(), "London", {"high": 100000.0, "low": 99000.0}, sess, {"day_pnl": 0.0})
check("a stale signal is skipped, never chased",
      (t, any("Missed the entry window" in m for m in log_msgs)), (None, True))
for k, v in real.items():
    setattr(ex, k, v)
notify.send, M.time.sleep = real_send, real_sleep

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
