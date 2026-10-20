"""Order lifecycle test — places ONE real resting order and cancels it.

This is the only script here that touches your account, so it is locked.
The order is a limit BUY of the minimum size placed 50% BELOW the market. It
cannot fill. The script then confirms the exchange lists it, cancels it, and
confirms it is gone.

What it proves: your key can create and cancel futures orders, and the order
payload the bot sends is accepted. What it does NOT prove: stop-loss placement
(that needs an open position — see the README's first-trade checklist).

    CONFIRM_LIVE_TEST=YES python verify_orders.py
"""
import os
import sys
import time

if os.environ.get("CONFIRM_LIVE_TEST") != "YES":
    print(__doc__)
    print("Locked. To run it, set CONFIRM_LIVE_TEST=YES.")
    sys.exit(0)

try:
    for line in open(".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass

from sorb15 import coindcx as ex, config as C, sizing

if not (C.COINDCX_API_KEY and C.COINDCX_API_SECRET):
    sys.exit("No credentials entered.")

C.DRY_RUN = False                       # this script is the one deliberate exception
oid = None
try:
    px = ex.last_price()
    limit = sizing.price(px * 0.5)
    print(f"market {px:,.2f}  ->  resting BUY {C.MIN_QTY} at {limit:,.2f}  (50% below; cannot fill)")
    resp = ex.limit_order("buy", C.MIN_QTY, limit)
    oid = ex.order_id_of(resp)
    print("create response:", resp)
    if not oid:
        sys.exit("FAIL  no order id came back. Do NOT proceed; check the CoinDCX app.")
    time.sleep(2)
    listed = [o for o in ex.open_orders() if str(o.get("id")) == str(oid)]
    print(f"{'PASS' if listed else 'FAIL'}  exchange {'lists' if listed else 'does not list'} order {oid}")
    ex.cancel(oid)
    time.sleep(2)
    still = [o for o in ex.open_orders() if str(o.get("id")) == str(oid)]
    print(f"{'FAIL  still open' if still else 'PASS'}  cancel {'did not work' if still else 'worked'}")
    print("\nNow open the CoinDCX app and confirm there is NO open order left.")
    sys.exit(1 if still or not listed else 0)
except SystemExit:
    raise
except Exception as e:
    print(f"FAIL  {type(e).__name__}: {e}")
    if oid:
        try:
            ex.cancel(oid); print(f"cancel requested for {oid}")
        except Exception as e2:
            print(f"CANCEL FAILED ({e2}) — remove order {oid} in the CoinDCX app NOW")
    sys.exit(1)
