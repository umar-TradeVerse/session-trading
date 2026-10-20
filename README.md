# SORB-15

Session Opening Range Breakout on BTC/USDT perpetuals, CoinDCX futures, 15-minute candles.
Four sub-agents behind one orchestrator. Runs 24x7 on Railway, sleeps outside the trading windows,
reports everything to Telegram.

```
sorb15/
  config.py      every setting, read from environment variables; override() for sweeps
  subagents.py   SA1 / SA2 / SA3 / SA4 — the ONE implementation, causal, used by backtest AND live
  backtest.py    candle-by-candle replay through those same detectors
  data.py        CoinDCX history loader (backtest only)
  report.py      backtest tables (backtest only; needs pandas)
  main.py        live runner: session clock, entry, management, paper mode
  coindcx.py     futures API client
  notify.py      Telegram + crash-safe state
test_local.py    79 offline tests (incl. a mocked-HTTP suite for every exchange call)
verify_api.py    read-only connection check: wallets, positions, contract spec, Telegram
verify_orders.py locked test: places and cancels ONE resting order that cannot fill
```

## One engine, no look-ahead

The backtest and the live bot call the same `subagents.detect()`. The backtest feeds it history one
candle at a time — after candle *k* closes it passes candles 0..*k* and nothing else — and enters at
the open of candle *k+1*. The live bot passes the candles that have closed so far and enters right
after the close. So a rule cannot see the future in the backtest, and the bot cannot behave
differently from what was tested.

`python test_local.py` includes an end-to-end audit: it scrambles every candle after each trade's
entry, re-runs the whole engine, and checks that no entry decision changed.

## The four sub-agents

| | Setup | Direction |
|---|---|---|
| **SA1** | The clear breakout forms on the session's **second candle**. No further confirmation. | Follows the breakout |
| **SA2** | Price taps, or comes very close to, a marked line. Wait for the **rejection candle**. A tap alone is never an entry. | Rejection at the HIGH → SHORT · at the LOW → LONG |
| **SA3** | **Continuation** of an SA2 London trade still open when New York opens, and only when NY confirms the same direction. | Same as the open trade |
| **SA4** | Clean breakout whose candle is not touching the marked lines — **vetoed** if a doji/reversal then confirms the opposite way. | Follows the breakout, unless momentum flips |

Evaluated in order **SA3 → SA1 → SA4 → SA2**; the first with something to say takes the session.
One trade per session.

**SA4 waits.** The bot cannot know a reversal is coming, so after a clean breakout it watches the
next `MOMENTUM_WINDOW` candles (default 2) as they close. A reversal candle followed by a close past
it vetoes the trade; otherwise it enters when the window ends.

**SA3 is dormant.** Trades are closed at the session end (18:45 London), so no SA2 London trade is
ever still open when New York starts. SA3 can only fire if London trades are allowed to carry over.

## Sessions (IST)

| | Open | Entry deadline | Close |
|---|---|---|---|
| London | 13:00 | 14:45 | 18:45 |
| New York | 19:00 | 20:45 | 01:15 |

## Paper mode

`DRY_RUN=true` (the default) runs a **paper account**: real signals, real entries at the real price,
and the full management — stop, partial at +0.5R, breakeven, trail — evaluated against live
candles, with P&L after fees reported to Telegram. No order is ever sent. Compare it against the
backtest after a week.

## Optional rules (all off by default)

Added after two trades were flagged on the charts (16 Oct London SA1, 15 Oct New York SA2). With every
switch off the bot behaves exactly as before — checked by fingerprint on fixed test data.

| Switch | What it does |
|---|---|
| `SA1_REQUIRE_CLEAN=true` | A candle that touches the line is not a clear breakout, even if it closed beyond it. |
| `SA2_STRICT_REJECTION=true` | Only a doji or long-wick rejection counts, not any red candle that reached the high. |
| `VETO_MODE=next_candle` | After an SA1/SA2 signal, wait one candle. If it closes against the trade, cancel. Costs one candle of delay. |
| `VETO_MODE=sa4_style` | Wait `MOMENTUM_WINDOW` candles. A doji/reversal candle followed by a close past it cancels. |

A veto skips the session. It does not flip to the other side.

## Position sizing, and what the exchange forces on a ₹5,000 account

CoinDCX accepts BTC quantities only in steps of **0.001** (about ₹7,100 of position), with a minimum
of 0.001 BTC and 60 USDT notional. Sizing is therefore coarse, and the backtest uses the same
`sizing.py` as the live bot so it is coarse in the same way.

- Quantity is **floored** to the step, so a stop-out loses at most the ₹100 target, never more.
- Measured on the 287-trade history: average real risk **₹83**, range ₹50–100. The most common
  size is 0.002 BTC.
- A stop wide enough to need less than 0.001 BTC is **skipped** (2 of 287 trades).
- The 50% partial must be two valid orders. 0.003 BTC splits 0.001 / 0.002 (a third, not half);
  0.001 BTC cannot split at all, so **about 1 trade in 10 takes no partial**.

## Live-only safety

- **Exchange-side stop on the position**, set straight after entry and confirmed by reading the
  position back, so a restart cannot leave it naked.
- **If the stop cannot be confirmed, the position is closed** and you are told. Same if a stop move
  fails and the old stop cannot be restored.
- **Orders are never retried.** If an order call errors the bot checks the real position before
  deciding — a lost reply must not become a second position.
- **The session is marked done before any order is sent**, so a crash mid-entry cannot enter twice.
- **Entry lag guard**: a signal older than `MAX_ENTRY_LAG_SECONDS` (default 90) is skipped, not
  chased — the backtest enters at the open of the next candle, and a late entry is a different trade.
- **The exchange is the source of truth**; two consecutive polls with no position are required
  before the bot believes a stop filled.
- Circuit breakers: daily loss limit, and a halt after `MAX_CONSEC_LOSSES` losses in a row
  (counted across days; skips `HALT_DAYS` extra days, then resumes).

## Setup

1. **Telegram**: @BotFather → `/newbot` → token. Message the bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` for your chat id.
2. **CoinDCX**: API Dashboard → key with futures trading. Fund the **Futures** wallet.
3. **GitHub**: `git init && git add . && git commit -m "SORB-15" && git push` — `.env` is git-ignored.
4. **Railway**: New Project → Deploy from GitHub → Variables → paste `.env.example`, fill it in.
5. Leave `DRY_RUN=true`. Watch a week.

## Backtest

```bash
python -m sorb15.backtest --start 2026-01-01
```

or open `SORB15_Final_Backtest.ipynb` in Colab.

## Going live — in this order, stopping at the first thing that fails

1. `verify_api.py` passes with your real credentials (not just SKIPPED). It shows which futures
   wallet holds your funds — set `MARGIN_CURRENCY` to match.
2. `CONFIRM_LIVE_TEST=YES python verify_orders.py` — creates and cancels one resting order.
   Proves your key can trade and the order payload is accepted.
3. A week on `DRY_RUN=true`, compared against your own charts.
4. **The first live trade, watched by you.** Set `DRY_RUN=false` with `RISK_PER_TRADE_INR=50`, and
   be at the CoinDCX app when a signal fires. Check, in the app: the position opened at the size
   Telegram reported; a **stop-loss is shown on the position**; and later that the stop moves when
   the bot says "STOP MOVED". The stop and trail calls are the one part no script can test without
   an open position, so this step is the test.
5. Only after a few clean trades, raise the risk.

If the bot cannot confirm a stop on a new position it closes the position and tells you. If you see
"STOP NOT CONFIRMED" on the first live trade, that is the exchange and the bot disagreeing about
the stop field — send me the message.

## Before live money — confirm

1. **Your real fees** (`TAKER_FEE`, `MAKER_FEE`). Costs are the dominant drag on this strategy.
2. **`USDINR`**. (The contract spec — step, minimum size, tick, minimum notional — is read from CoinDCX every time the bot starts, so there is nothing to type.)
3. That the **Futures** wallet is funded, and `MARGIN_CURRENCY` matches it (INR or USDT).
