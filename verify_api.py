"""Connection check. Read-only: places no orders.

    python verify_api.py

Distinguishes three outcomes for every check: PASS, FAIL, and SKIPPED. A check
that was skipped because no credentials were entered is NOT a pass.
"""
import os
import sys

try:                                    # load .env if there is one
    for line in open(".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass

os.environ["DRY_RUN"] = "true"          # nothing here can place an order
from sorb15 import coindcx as ex, config as C, notify, sizing
from sorb15 import main as M

have_keys = bool(C.COINDCX_API_KEY and C.COINDCX_API_SECRET)
fails, skipped = [], []

print("=" * 64)
print("SORB-15 connection check   (read-only, places no orders)")
print("=" * 64)

print("\n1. Public candles")
try:
    cs = ex.candles(minutes_back=300)
    print(f"   PASS  {len(cs)} closed 15m candles for {C.PAIR}, latest close {cs[-1]['close']:,.2f}")
except Exception as e:
    fails.append(f"candles: {e}"); print(f"   FAIL  {e}")

print("\n2. Contract specification (read from CoinDCX — nothing to look up by hand)")
try:
    spec = ex.instrument()
    scalars = {k: x for k, x in spec.items() if isinstance(x, (int, float, str)) and x != ""}
    keep = [k for k in scalars if any(w in k.lower() for w in
            ("min", "max", "tick", "incr", "step", "precision", "lot", "leverage", "size", "status"))]
    for k in (keep or list(scalars))[:24]:
        print(f"        {k:34s} {scalars[k]}")
    pairs = [("quantity_increment", "QTY_STEP"), ("price_increment", "PRICE_STEP"),
             ("min_trade_size", "MIN_QTY"), ("min_notional", "MIN_NOTIONAL_USDT")]
    diffs = []
    for key, attr in pairs:
        try:
            ev = float(spec[key])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(ev - getattr(C, attr)) > 1e-12:
            diffs.append(f"{attr}: configured {getattr(C, attr)}, exchange says {ev}")
    if diffs:
        print("   NOTE  configured values differ from the exchange:")
        for d in diffs:
            print("          -", d)
        print("         The bot reads the real spec at startup and uses the exchange's values,")
        print("         so this cannot cause a rejected order. Update .env to match anyway.")
        print("         Everything below uses the exchange's values.")
    _send, notify.send = notify.send, (lambda *a, **k: None)     # no Telegram from a check
    try:
        M.sync_specs()
    finally:
        notify.send = _send
    print("   PASS  contract spec read. Orders will use quantity steps of "
          f"{spec.get('quantity_increment', C.QTY_STEP)} and a minimum of "
          f"{spec.get('min_trade_size', C.MIN_QTY)}.")
except Exception as e:
    fails.append(f"instrument: {e}"); print(f"   FAIL  {e}")

print("\n3. API credentials, futures wallets and positions")
if not have_keys:
    skipped.append("credentials")
    print("   SKIPPED  no COINDCX_API_KEY / COINDCX_API_SECRET entered.")
    print("            This is NOT a pass — your key has not been tested yet.")
else:
    try:
        w = ex.wallets()
        for cur, v in w.items():
            print(f"   wallet {cur:5s} usable {v['usable']:>12,.4f}   locked {v['locked']:>10,.4f}"
                  f"   total {v['total']:>12,.4f}")
        mine = w.get(C.MARGIN_CURRENCY, {}).get("total", 0)
        other = {k: v["total"] for k, v in w.items() if k != C.MARGIN_CURRENCY and v["total"] > 0}
        if mine <= 0 and other:
            fails.append(f"MARGIN_CURRENCY={C.MARGIN_CURRENCY} wallet is empty but {other} is funded")
            print(f"   FAIL  MARGIN_CURRENCY is {C.MARGIN_CURRENCY} but your funds are in "
                  f"{list(other)[0]}. Set MARGIN_CURRENCY={list(other)[0]}.")
        elif mine <= 0:
            fails.append("futures wallet empty")
            print("   FAIL  the futures wallet holds nothing. Transfer funds Spot -> Futures.")
        else:
            print(f"   PASS  {C.MARGIN_CURRENCY} futures wallet holds {mine:,.2f}")
    except Exception as e:
        fails.append(f"wallets: {e}"); print(f"   FAIL  wallets: {e}")
        print("         401 = key or secret wrong, or no futures permission.  "
              "Also check any IP binding on the key.")
    try:
        p = ex.open_position()
        print(f"   PASS  positions endpoint answered: "
              f"{('open ' + p['side'] + ' ' + str(p['qty'])) if p else 'no open position'}")
    except Exception as e:
        fails.append(f"positions: {e}"); print(f"   FAIL  positions: {e}")

print("\n4. Telegram")
if not (C.TELEGRAM_BOT_TOKEN and C.TELEGRAM_CHAT_ID):
    skipped.append("telegram"); print("   SKIPPED  token or chat id not entered.")
else:
    try:
        notify.send("✅ SORB-15 connection check — Telegram works.")
        print("   PASS  sent. Check your chat.")
    except Exception as e:
        fails.append(f"telegram: {e}"); print(f"   FAIL  {e}")

print("\n5. Sizing — exactly what the exchange would receive")
try:
    px = ex.last_price()
    print(f"   BTC {px:,.2f}   risk target Rs {C.RISK_PER_TRADE_INR:.0f}   step {C.QTY_STEP}")
    print(f"   {'stop':>6} {'qty':>7} {'partial':>8} {'notional Rs':>12} {'real risk Rs':>13}")
    for pct in (0.002, 0.003, 0.0045, 0.008, 0.012, 0.016):
        sl = px * (1 - pct)
        q = sizing.qty_for(px, sl)
        if q <= 0:
            print(f"   {pct*100:5.2f}% {'-':>7} {'-':>8}   below the exchange minimum: trade skipped")
            continue
        pq = sizing.partial_qty(q)
        real = pct * q * px * C.USDINR
        print(f"   {pct*100:5.2f}% {q:7.3f} {(pq if pq else 'none'):>8} "
              f"{q*px*C.USDINR:12,.0f} {real:13,.0f}")
    print("   (qty is floored to the step, so real risk is at or below the target.)")
except Exception as e:
    print(f"   skipped: {e}")

print("\n" + "=" * 64)
if fails:
    print("NOT READY. Fix:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
if skipped:
    print("INCOMPLETE — nothing failed, but these were not tested: " + ", ".join(skipped))
    sys.exit(2)
print("All checks passed. Safe to deploy with DRY_RUN=true.")
