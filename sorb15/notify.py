"""Telegram notifications and crash-safe state."""
import json
import logging
import os
import time

import requests

from . import config as C

log = logging.getLogger("notify")


# ── telegram ────────────────────────────────────────────────────────────────

def send(text, silent=False):
    if not C.TELEGRAM_BOT_TOKEN or not C.TELEGRAM_CHAT_ID:
        log.info("[telegram not configured] %s", text)
        return
    url = f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": C.TELEGRAM_CHAT_ID, "text": text,
                                     "parse_mode": "HTML",
                                     "disable_notification": silent}, timeout=15)
        if r.status_code != 200:
            log.warning("telegram %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("telegram failed: %s", e)


def signal_block(sig, session, H, L, entry=None, sl=None, action="", reason="", result=None):
    """The exact log format, rendered for Telegram."""
    d = sig.get("direction", 0)
    lines = [
        f"<b>{session}</b>  range {L:,.2f} - {H:,.2f}",
        "<pre>",
        f"SUB-AGENT      : {sig.get('sub_agent','-')}",
        f"DIRECTION      : {'LONG' if d == 1 else 'SHORT' if d == -1 else '-'}",
        f"SETUP          : {sig.get('setup','-')}",
        f"BREAKOUT       : {'YES' if sig.get('breakout') else 'NO'}",
        f"REJECTION      : {'YES' if sig.get('rejection') else 'NO'}",
        f"MOMENTUM SHIFT : {'YES' if sig.get('momentum_shift') else 'NO'}",
        f"ENTRY          : {entry if entry is not None else '-'}",
        f"SL             : {sl if sl is not None else '-'}",
        f"TP             : trail/partial",
        f"ACTION         : {action}",
        f"REASON         : {reason or sig.get('detail','')}",
    ]
    if result:
        lines.append(f"RESULT         : {result}")
    lines.append("</pre>")
    if C.DRY_RUN:
        lines.append("<i>DRY_RUN — no order was placed</i>")
    return "\n".join(lines)


# ── state ───────────────────────────────────────────────────────────────────

_DEFAULT = {"trade": None, "open_london": None, "day": None,
            "day_pnl": 0.0, "consec_losses": 0, "halted": False,
            "halt_through": None, "done_sessions": [], "last_veto": None}


def load():
    try:
        with open(C.STATE_FILE) as f:
            s = json.load(f)
        return {**_DEFAULT, **s}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT)


def save(state):
    tmp = C.STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, C.STATE_FILE)
    except OSError as e:
        log.warning("could not persist state: %s", e)


def roll_day(state, today):
    """Reset the daily counters when the IST date changes. A loss-streak halt
    lifts once `today` is past halt_through; the streak counter restarts then."""
    if state.get("day") != today:
        state.update(day=today, day_pnl=0.0, done_sessions=[], open_london=None)
    ht = state.get("halt_through")
    if state.get("halted") and ht and today > ht:
        state.update(halted=False, halt_through=None, consec_losses=0)
    return state
