"""CoinDCX futures client.

Endpoints and payloads were checked against CoinDCX's own docs and a futures
client written from the official API PDF. Differences from a generic exchange:

  * Signed requests send a JSON body (with a timestamp) even for GET.
    The signature is HMAC-SHA256 over that exact body.
  * The futures wallet is a signed GET, not a POST.
  * Stop-loss / take-profit are set on the POSITION (create_tpsl), by position id.
  * One open position per pair; closing a position is its own endpoint.

Everything that places or changes an order is a no-op when DRY_RUN is true.
"""
import hashlib
import hmac
import json
import logging
import time

import requests

from . import config as C
from . import sizing

log = logging.getLogger("coindcx")
_session = requests.Session()

FUT = "/exchange/v1/derivatives/futures"


class CoinDCXError(RuntimeError):
    pass


def _signed(path, body=None, method="POST", timeout=20, retries=3):
    """Signed request. The timestamp and signature are rebuilt on every attempt.

    `retries` must be 1 for anything that creates an order: if the request
    reached the exchange but the reply was lost, a retry would place it twice."""
    url = C.BASE_URL + path
    last = None
    for attempt in range(retries):
        payload_body = dict(body or {})
        payload_body["timestamp"] = int(time.time() * 1000)
        payload = json.dumps(payload_body, separators=(",", ":"))
        sig = hmac.new(C.COINDCX_API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json",
                   "X-AUTH-APIKEY": C.COINDCX_API_KEY, "X-AUTH-SIGNATURE": sig}
        try:
            r = _session.request(method, url, data=payload, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise CoinDCXError(last)             # will not fix itself
        except requests.RequestException as e:
            last = str(e)
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    raise CoinDCXError(f"{method} {path} failed after {retries} attempt(s) — {last}")


def _rows(js):
    return js if isinstance(js, list) else (js.get("data") or js.get("orders") or [])


# ── public market data ──────────────────────────────────────────────────────

def candles(pair=None, minutes_back=600, include_forming=False):
    """15m futures candles, oldest first, open time in UTC ms.

    By default only CLOSED candles are returned — that is what the detectors
    must see. Trade management passes include_forming=True so the stop can
    follow price inside the current candle, as the backtest assumes."""
    pair = pair or C.PAIR
    now = int(time.time())
    params = {"pair": pair, "from": now - minutes_back * 60, "to": now,
              "resolution": C.RESOLUTION, "pcode": "f"}
    r = _session.get(C.PUBLIC_URL + "/market_data/candlesticks", params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    out = []
    for c in (js.get("data", []) if isinstance(js, dict) else js):
        t = int(c["time"])
        if t < 1e11:
            t *= 1000
        out.append({"t": t, "open": float(c["open"]), "high": float(c["high"]),
                    "low": float(c["low"]), "close": float(c["close"]),
                    "volume": float(c.get("volume", 0))})
    out.sort(key=lambda x: x["t"])
    if include_forming:
        return out
    cutoff = int(time.time() * 1000) - C.TIMEFRAME_MIN * 60 * 1000
    return [c for c in out if c["t"] <= cutoff]


def last_price(pair=None):
    cs = candles(pair, minutes_back=60, include_forming=True)
    if not cs:
        raise CoinDCXError("no candles returned")
    return cs[-1]["close"]


def instrument(pair=None):
    """Contract specification (public). Source of truth for min size and precision."""
    r = _session.get(C.BASE_URL + FUT + "/data/instrument",
                     params={"pair": pair or C.PAIR}, timeout=20)
    r.raise_for_status()
    js = r.json()
    return js.get("instrument", js) if isinstance(js, dict) else js


# ── account ─────────────────────────────────────────────────────────────────

def wallets():
    """Both futures wallets: {currency: {usable, locked, total}}.
    Total wallet balance = balance + locked_balance (per CoinDCX)."""
    out = {}
    for w in _rows(_signed(FUT + "/wallets", {}, method="GET")):
        bal = float(w.get("balance") or 0)
        lock = float(w.get("locked_balance") or 0)
        out[w.get("currency_short_name")] = {"usable": bal, "locked": lock, "total": bal + lock}
    return out


def balance():
    return wallets().get(C.MARGIN_CURRENCY, {}).get("total", 0.0)


def positions(pair=None):
    """Open positions for the pair, as the exchange reports them."""
    pair = pair or C.PAIR
    rows = []
    for extra in ({}, {"margin_currency_short_name": [C.MARGIN_CURRENCY]}):
        try:
            js = _signed(FUT + "/positions", {"page": "1", "size": "50", **extra})
        except CoinDCXError:
            if extra:
                break                  # the filtered form is optional
            raise
        rows = [p for p in _rows(js)
                if p.get("pair") == pair and abs(float(p.get("active_pos") or 0)) > 0]
        if rows:
            return rows
    return rows


def open_position(pair=None):
    """The live position as the EXCHANGE sees it, or None.

    Always call this before acting. Never trust local state about what is open —
    stale position data has bitten this account before."""
    ps = positions(pair)
    if not ps:
        return None
    p = ps[0]
    qty = float(p.get("active_pos") or 0)
    return {"id": p.get("id"), "qty": abs(qty), "side": "LONG" if qty > 0 else "SHORT",
            "entry": float(p.get("avg_price") or 0),
            "stop": float(p.get("stop_loss_trigger") or 0),
            "liq": float(p.get("liquidation_price") or 0), "raw": p}


def open_orders(pair=None):
    js = _signed(FUT + "/orders", {"status": "open", "page": "1", "size": "50"})
    pair = pair or C.PAIR
    return [o for o in _rows(js) if o.get("pair") in (None, pair)]


# ── orders (all no-ops in DRY_RUN) ──────────────────────────────────────────

def _dry(what, body):
    log.info("[DRY_RUN] %s suppressed: %s", what, body)
    return {"dry_run": True}


def _create(order):
    if C.DRY_RUN:
        return _dry("order", order)
    order = {**order, "leverage": C.LEVERAGE, "margin_currency_short_name": C.MARGIN_CURRENCY,
             "position_margin_type": C.MARGIN_TYPE, "time_in_force": "good_till_cancel",
             "notification": "email_notification", "pair": order.get("pair") or C.PAIR}
    return _signed(FUT + "/orders/create", {"order": order}, retries=1)


def market_order(side, qty, pair=None, client_id=None, reduce_only=False):
    """Market order. For a PARTIAL close pass the opposite side with a quantity
    smaller than the position, which cannot flip it. `client_id` and
    `reduce_only` are accepted for compatibility but not sent — they are not in
    the documented futures payload."""
    return _create({"pair": pair, "side": side, "order_type": "market_order",
                    "total_quantity": sizing.floor_step(qty, C.QTY_STEP)})


def limit_order(side, qty, price, pair=None):
    return _create({"pair": pair, "side": side, "order_type": "limit_order",
                    "total_quantity": sizing.floor_step(qty, C.QTY_STEP),
                    "price": str(sizing.price(price))})


def set_stop(position_id, stop_price):
    """Set (or replace) the position's stop-loss. If the exchange refuses a
    second stop while one exists, the old one is cleared first."""
    if C.DRY_RUN:
        return _dry("set_stop", {"id": position_id, "stop": stop_price})
    body = {"id": position_id,
            "stop_loss": {"stop_price": str(sizing.price(stop_price)),
                          "order_type": "stop_market"}}
    try:
        return _signed(FUT + "/positions/create_tpsl", body, retries=1)
    except CoinDCXError as first:
        log.warning("create_tpsl refused (%s) — clearing the old stop and retrying", first)
        clear_position_orders(position_id)
        return _signed(FUT + "/positions/create_tpsl", body, retries=1)


def clear_position_orders(position_id):
    if C.DRY_RUN:
        return _dry("clear_position_orders", position_id)
    return _signed(FUT + "/positions/cancel_all_open_orders_for_position",
                   {"id": position_id}, retries=2)


def exit_position(position_id):
    """Close the whole position at market."""
    if C.DRY_RUN:
        return _dry("exit_position", position_id)
    return _signed(FUT + "/positions/exit", {"id": position_id}, retries=1)


def cancel(order_id):
    if C.DRY_RUN:
        return _dry("cancel", order_id)
    return _signed(FUT + "/orders/cancel", {"id": order_id}, retries=2)


def order_id_of(resp):
    if not resp or (isinstance(resp, dict) and resp.get("dry_run")):
        return None
    rows = _rows(resp) if not (isinstance(resp, dict) and resp.get("id")) else [resp]
    return rows[0].get("id") if rows else None
