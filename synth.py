"""Deterministic fake 15m candles for tests and regression checks. Not market data.

    from synth import candles, signature
    trades, _ = backtest.run(candles())          # seed 11, 270 days
    signature(trades)                            # -> ('2f6ec4de24d1060c', 230) with all optional rules OFF
                                                  # (London session moved to 14:00-19:00 on 2026-10-26)

That fingerprint is the proof that a code change did not alter baseline behaviour: if it moves
with every optional switch off, something in the engine changed.
"""
import datetime as dt
import hashlib
import json
import random
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def candles(seed=11, days=270):
    rnd = random.Random(seed)
    price, out = 90000.0, []
    t0 = int(dt.datetime(2026, 1, 1, 0, 0, tzinfo=IST).timestamp() * 1000)
    for k in range(96 * days):
        o = price
        c = o * (1 + rnd.gauss(0, 0.0025))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0012)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.0012)))
        out.append({"t": t0 + k * 900000, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})
        price = c
    return out


def signature(trades):
    rows = [(str(t["date"]), t["session"], t["sub_agent"], t["side"], round(t["entry"], 4),
             round(t["pnl_inr"], 4), t["exit_reason"]) for t in trades]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16], len(rows)
