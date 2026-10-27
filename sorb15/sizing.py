"""Position sizing — ONE implementation, used by the live bot and the backtest.

CoinDCX only accepts quantities in whole steps (BTC: 0.001) and enforces a
minimum order size and notional. Sizing therefore floors to the step instead
of rounding to some number of decimals, and returns 0 when the result would be
rejected. The backtest uses the same function, so it sizes exactly as live does.

Flooring means a stop-out loses at most RISK_PER_TRADE_INR, never more.
"""
import math

from . import config as C


def floor_step(x, step):
    """Largest whole multiple of `step` that does not exceed x."""
    return round(math.floor(x / step + 1e-9) * step, 8)


def nearest_step(x, step):
    return round(round(x / step) * step, 8)


def price(p):
    """A price on the exchange's tick."""
    return nearest_step(p, C.PRICE_STEP)


def qty_for(entry, sl):
    """BTC quantity so that a stop-out loses at most RISK_PER_TRADE_INR.
    0.0 if the exchange would refuse the order (below minimum size or notional)."""
    risk_pct = abs(entry - sl) / entry
    if risk_pct <= 0:
        return 0.0
    notional_inr = min(C.RISK_PER_TRADE_INR / risk_pct, C.MAX_NOTIONAL_INR)
    q = floor_step((notional_inr / C.USDINR) / entry, C.QTY_STEP)
    if q < C.MIN_QTY - 1e-12 or q * entry < C.MIN_NOTIONAL_USDT:
        return 0.0
    return q


def qty_for_fixed_notional(entry, margin_inr):
    """BTC quantity for a fixed MARGIN allocation (not solved for a target risk) --
    exposure is margin_inr * LEVERAGE, same convention qty_for() uses for capital.
    0.0 if the exchange would refuse the order (below minimum size or notional)."""
    notional_inr = margin_inr * C.LEVERAGE
    q = floor_step((notional_inr / C.USDINR) / entry, C.QTY_STEP)
    if q < C.MIN_QTY - 1e-12 or q * entry < C.MIN_NOTIONAL_USDT:
        return 0.0
    return q


def partial_qty(qty):
    """Quantity to close at the partial target, quantised to the step.
    0.0 means the partial cannot be taken: either piece would be below the minimum."""
    part = floor_step(qty * C.PARTIAL_TP_FRAC, C.QTY_STEP)
    if part < C.MIN_QTY - 1e-12 or (qty - part) < C.MIN_QTY - 1e-12:
        return 0.0
    return part
