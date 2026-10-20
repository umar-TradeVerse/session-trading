"""Tables for the backtest notebook. Needs pandas; the live bot never imports this."""
import numpy as np
import pandas as pd

from . import config as C


def frames(trades, signals):
    t, s = pd.DataFrame(trades), pd.DataFrame(signals)
    if len(t):
        t["month"] = pd.to_datetime(t["date"]).dt.strftime("%Y-%m")
    return t, s


def stats(g, label=""):
    if g is None or len(g) == 0:
        return {"": label, "trades": 0}
    w, l = g[g.pnl_inr > 0], g[g.pnl_inr <= 0]
    eq = C.CAPITAL_INR + g.pnl_inr.cumsum()
    pk = np.maximum.accumulate(np.r_[C.CAPITAL_INR, eq.values])[1:]
    se = g.pnl_inr.std(ddof=1) / np.sqrt(len(g)) if len(g) > 1 else np.nan
    gse = g.gross_inr.std(ddof=1) / np.sqrt(len(g)) if len(g) > 1 else np.nan
    return {"": label, "trades": len(g), "win": f"{len(w)/len(g):.1%}",
            "gross_inr": round(g.gross_inr.sum()), "net_inr": round(g.pnl_inr.sum()),
            "cost_inr": round(g.gross_inr.sum() - g.pnl_inr.sum()),
            "PF": round(w.pnl_inr.sum() / max(abs(l.pnl_inr.sum()), 1), 2),
            "exp_inr": round(g.pnl_inr.mean(), 1), "avg_R": round(g.R.mean(), 3),
            "avg_risk_inr": round(g.risk_inr.mean()), "cost_in_R": round(g.cost_in_R.mean(), 2),
            "maxDD": round((eq - pk).min()),
            "t_gross": round(g.gross_inr.mean() / gse, 2) if gse == gse and gse else np.nan,
            "t_net": round(g.pnl_inr.mean() / se, 2) if se == se and se else np.nan}


def by(trades_df, col):
    return pd.DataFrame([stats(g, k) for k, g in trades_df.groupby(col, sort=True)])


def overall(trades_df):
    return pd.DataFrame([stats(trades_df, "ALL")])


def funnel(signals_df):
    s = signals_df.copy()
    s["outcome"] = np.where(s.action == "ENTER", "ENTER " + s.sub_agent,
                            "AVOID " + s.sub_agent.where(s.sub_agent != "-", "no setup"))
    return s.groupby("outcome").size().sort_values(ascending=False).to_frame("sessions")


def log_block(row):
    r = row
    print("=" * 62)
    print(f"{r['date']}  {r['session']}   range {r['range_low']:,.2f} - {r['range_high']:,.2f}"
          f"  ({r['range_pct']*100:.3f}%)")
    print("-" * 62)
    for label, key in [("SUB-AGENT", "sub_agent"), ("DIRECTION", "direction"), ("SETUP", "setup")]:
        print(f"{label:15s}: {r[key]}")
    for label, key in [("BREAKOUT", "breakout"), ("REJECTION", "rejection"),
                       ("MOMENTUM SHIFT", "momentum_shift")]:
        print(f"{label:15s}: {'YES' if r[key] else 'NO'}")
    print(f"{'ENTRY':15s}: {r.get('entry', '-')}")
    print(f"{'SL':15s}: {r.get('sl', '-')}")
    print(f"{'TP':15s}: {r.get('tp', '-')}")
    print(f"{'ACTION':15s}: {r['action']}")
    print(f"{'REASON':15s}: {r['reason']}")
    if pd.notna(r.get("pnl_inr", np.nan)):
        print(f"{'RESULT':15s}: Rs {r['pnl_inr']:,.2f}  ({r.get('exit_reason', '')})")
    print()


def paste_summary(trades_df, signals_df, label="FINAL CONFIG"):
    """One block of plain text to paste back, so no screenshots are needed."""
    a = stats(trades_df, "ALL")
    lines = [f"=== SORB15 SUMMARY — {label} ===",
             f"trades {a['trades']}  win {a['win']}  gross Rs {a['gross_inr']:,}  "
             f"cost Rs {a['cost_inr']:,}  net Rs {a['net_inr']:,}",
             f"PF {a['PF']}  exp Rs {a['exp_inr']}  avgR {a['avg_R']}  maxDD Rs {a['maxDD']:,}  "
             f"cost_in_R {a['cost_in_R']}  t_gross {a['t_gross']}  t_net {a['t_net']}", ""]
    for k, g in trades_df.groupby("sub_agent"):
        x = stats(g, k)
        lines.append(f"{k}: n={x['trades']} win={x['win']} gross={x['gross_inr']:,} "
                     f"net={x['net_inr']:,} PF={x['PF']} avgR={x['avg_R']}")
    lines.append("")
    for k, g in trades_df.groupby(["session", "side"]):
        x = stats(g, "")
        lines.append(f"{k[0]:8s} {k[1]:5s}: n={x['trades']} gross={x['gross_inr']:,} net={x['net_inr']:,}")
    lines.append("")
    lines.append("exits: " + ", ".join(f"{k}={v}" for k, v in
                                       trades_df.exit_reason.value_counts().items()))
    lines.append("funnel: " + ", ".join(f"{k}={v}" for k, v in
                                        funnel(signals_df).sessions.items()))
    return "\n".join(lines)
