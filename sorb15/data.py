"""Historical 15m BTC candles for backtesting. Not used by the live bot.

Tries CoinDCX's own futures candles first (the market we trade), then falls back
to Binance. Returns closed candles only, oldest first, as dicts with t in ms.
"""
import csv
import datetime as dt
import io
import time
import zipfile
from zoneinfo import ZoneInfo

import requests

from . import config as C

IST = ZoneInfo(C.TZ)
STEP_MS = 15 * 60 * 1000
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _clean(rows):
    out, seen = [], set()
    for r in sorted(rows, key=lambda x: x["t"]):
        if r["t"] in seen:
            continue
        seen.add(r["t"])
        out.append(r)
    return out


def fetch_coindcx(start_ms, end_ms):
    url = C.PUBLIC_URL + "/market_data/candlesticks"
    rows, t = [], start_ms
    while t < end_ms:
        t2 = min(t + 3 * 86_400_000, end_ms)
        r = requests.get(url, params={"pair": C.PAIR, "from": t // 1000, "to": t2 // 1000,
                                      "resolution": "15", "pcode": "f"},
                         headers=UA, timeout=25)
        r.raise_for_status()
        js = r.json()
        for c in (js.get("data", []) if isinstance(js, dict) else js):
            ts = int(c["time"])
            ts = ts * 1000 if ts < 1e11 else ts
            rows.append({"t": ts, "open": float(c["open"]), "high": float(c["high"]),
                         "low": float(c["low"]), "close": float(c["close"]),
                         "volume": float(c.get("volume", 0))})
        t = t2
        time.sleep(0.25)
    if not rows:
        raise RuntimeError("CoinDCX returned no candles")
    return _clean(rows)


def _binance_zip(content):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    rows = []
    for line in csv.reader(io.StringIO(raw)):
        if not line or not line[0][:1].isdigit():
            continue
        ts = int(line[0])
        ts = ts // 1000 if ts > 1e14 else ts          # microsecond archives
        rows.append({"t": ts, "open": float(line[1]), "high": float(line[2]),
                     "low": float(line[3]), "close": float(line[4]), "volume": float(line[5])})
    return rows


def fetch_binance_futures(start_ms, end_ms):
    base = "https://data.binance.vision/data/futures/um"
    s = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc)
    e = dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc)
    rows, cur = [], dt.date(s.year, s.month, 1)
    this_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    while cur <= e.date():
        got = False
        if cur < this_month:
            r = requests.get(f"{base}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-{cur:%Y-%m}.zip",
                             headers=UA, timeout=60)
            if r.status_code == 200:
                rows += _binance_zip(r.content)
                got = True
        if not got:
            d = cur
            nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            while d < nxt and d <= e.date():
                r = requests.get(f"{base}/daily/klines/BTCUSDT/15m/BTCUSDT-15m-{d:%Y-%m-%d}.zip",
                                 headers=UA, timeout=60)
                if r.status_code == 200:
                    rows += _binance_zip(r.content)
                d += dt.timedelta(days=1)
                time.sleep(0.1)
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    if not rows:
        raise RuntimeError("Binance archive returned no files")
    return _clean(rows)


def fetch_history(start="2026-01-01", end=None, source="auto"):
    """Returns (candles, source_name). `start`/`end` are IST dates (YYYY-MM-DD)."""
    s = dt.datetime.fromisoformat(start).replace(tzinfo=IST) - dt.timedelta(hours=6)
    e = (dt.datetime.fromisoformat(end).replace(tzinfo=IST) + dt.timedelta(days=1)) if end \
        else dt.datetime.now(IST)
    start_ms, end_ms = int(s.timestamp() * 1000), int(e.timestamp() * 1000)
    expected = (end_ms - start_ms) / STEP_MS
    order = ["coindcx", "binance_futures"] if source == "auto" else [source]
    fetchers = {"coindcx": fetch_coindcx, "binance_futures": fetch_binance_futures}
    now_ms = int(time.time() * 1000)
    for name in order:
        try:
            print(f"trying {name} ...")
            rows = [r for r in fetchers[name](start_ms, end_ms)
                    if start_ms <= r["t"] < end_ms and r["t"] + STEP_MS <= now_ms]
            cover = len(rows) / expected
            print(f"  {len(rows):,} candles ({cover:.0%} of expected)")
            if cover < 0.9:
                print("  coverage too low, trying the next source")
                continue
            return rows, name
        except Exception as ex:
            print(f"  {name} failed: {ex}")
    raise RuntimeError("all data sources failed")


def quality(candles):
    gaps = sum(1 for a, b in zip(candles, candles[1:]) if b["t"] - a["t"] != STEP_MS)
    bad = sum(1 for c in candles
              if c["high"] < max(c["open"], c["close"]) or c["low"] > min(c["open"], c["close"]))
    f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, IST).strftime("%d %b %Y %H:%M")
    print(f"range    : {f(candles[0]['t'])} -> {f(candles[-1]['t'])} IST")
    print(f"candles  : {len(candles):,}   gaps: {gaps}   malformed: {bad}")
    return gaps, bad
