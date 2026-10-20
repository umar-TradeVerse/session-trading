# SORB-15 — session opening-range breakout bot (BTC 15m, CoinDCX futures)

Owner: Umar. These are **his rules** for a session high/low ("liquidity") breakout strategy. Status as of **18 Oct 2026**.
His original ask was simple: *backtest the rules I gave on real data.* Do not drag him into credentials or deployment unless he asks.

## Where things stand (read this first)
- The backtest is done, on real CoinDCX candles (31 Dec 2025 → 17 Oct 2026, 27,882 candles, no gaps).
- **The rules, as specified, lose money after costs.** 301 trades, net **−₹4,805** on ₹5,000 (ends at ₹195), PF 0.53, t(net) −4.17.
- Gross edge is indistinguishable from zero. The loss is costs (fees ₹5,977 + slippage ₹2,026 + funding ₹67) on a ₹3,266 gross.
- Nothing has been run live. **No credentials were ever entered. `DRY_RUN=true` stays true.** Do not deploy.
- Open decision for Umar: stop here, or try a *different design* (not another tuning of this one). See "Open decisions".

## The rules (Umar's — do not change without asking)
- BTC only · 15m candles · CoinDCX futures `B-BTC_USDT` · 10x isolated · ₹5,000 capital · risk ₹100/trade.
- Sessions (IST): **London 13:00–18:45, New York 19:00–01:15.** The first 15m candle of each session sets the range (high/low).
- A breakout candle must **close** beyond the level. Entry is on the **next** candle's open, never the trigger candle.
- Entry deadline: London 14:45, New York 20:45. **One trade per session.**
- Stop = broken level ± buffer (0.15%; SA2 uses 0.45%). Partial 50% at +0.5R. Trail 0.25R behind best price from +0.5R, intrabar. Breakeven stop at +1R to entry +0.16%. Flatten at session end.
- "Clear breakout" currently = closes beyond the line AND (≥50% of the candle beyond it OR ≤15% of the candle back inside the range). Umar later said a candle *touching* the line should not count — that is the optional `SA1_REQUIRE_CLEAN` rule, **off by default**.

## The four sub-agents (as implemented in `sorb15/subagents.py`)
Router order: SA3 → SA1 → SA4 → SA2. The first one with something to say takes the session.
- **SA1** — clear breakout on the *second* candle (first after the range candle); no confirmation needed.
- **SA2** — price taps/nears a line, then a rejection candle → trade. Rejection at HIGH → SHORT, at LOW → LONG. Stands down if any candle closes beyond a line. Rejection types: doji, long wick, or (loosest) "any red candle that reached the high / green candle that reached the low".
- **SA3** — NY continuation of an SA2 London trade still open at 18:45. Dormant in practice (trades close at session end).
- **SA4** — a clean breakout candle (not touching the lines); waits `MOMENTUM_WINDOW` (2) candles; a doji/reversal candle followed by a close past it → AVOID.

## Architecture — ONE engine
```
sorb15/subagents.py   causal detectors; used by backtest AND live (never fork this)
sorb15/backtest.py    replays history one candle at a time; audit_causality() scrambles the future
sorb15/sizing.py      shared sizing: floors to 0.001 BTC, enforces min size / min notional 60 USDT
sorb15/config.py      all settings from env vars; C.override(**kw) context manager for runs
sorb15/coindcx.py     futures client (signed GET-with-body wallet, create_tpsl stops, never retries orders)
sorb15/main.py        live loop (paper mode when DRY_RUN=true)
sorb15/data.py report.py notify.py
synth.py              deterministic fake candles + baseline fingerprint (tests only)
test_local.py         124 offline tests          verify_api.py / verify_orders.py   exchange checks (not needed for backtesting)
reference/            result files from the Colab runs (see below)
```
Run tests: `DRY_RUN=true python test_local.py` → must print `124 passed, 0 failed`.
Candles are **not** in the repo (they lived in Colab). Fetch them (public API, no keys):
```python
import os; os.environ["DRY_RUN"] = "true"
from sorb15 import backtest as B, data, report
candles, source = data.fetch_history("2026-01-01")      # cache to CSV; takes ~1 min
trades, signals = B.run(candles)                         # overrides: B.run(candles, TAKER_FEE=0.0004, VETO_MODE="next_candle")
T, S = report.frames(trades, signals); print(report.overall(T)); print(report.by(T, "sub_agent"))
```

## Assumptions that are MINE, not Umar's — verify before trusting any number
- **Taker fee 0.059%/side** — never confirmed against his CoinDCX account (0.05% + 18% GST would fit). Maker 0%, unused (all exits are market/stop).
- **Slippage 0.02%/side**, funding 0.01%/8h, USD/INR 88, maintenance margin 0.5%.
- **SA2 stop buffer 0.45%** was chosen by a parameter sweep, not given by Umar. The doji/pin/tap/touch tolerances and the 2-candle momentum window are my interpretations of his wording.
- Daily loss limit ₹500 and the 4-loss halt are my safety rules.
- **Slippage is applied to the fill prices, so it sits INSIDE the "gross" figure.** The report's `cost_inr` = fees + funding only. (An early notebook mislabelled this; corrected.) Always break it out: gross before slippage / slippage / fees+funding.
- Sizing is coarse: 0.001 BTC steps ≈ ₹7,100. Average real risk is ~₹82 (not ₹100); ~1 trade in 10 cannot split a 50% partial.

## Results so far (real data)
**Final backtest** — 301 trades, win 61.5%, avg win ₹30 vs avg loss ₹89 (break-even needs ~75% wins).
Gross before slippage +₹3,266 · slippage −₹2,026 · fees −₹5,977 · funding −₹67 · **net −₹4,805** · max DD −₹4,811.
- Exits: trailing_stop 189 trades +₹5,425 (96% win) · stop_loss 89 trades −₹9,472 (0% win) · other 23 trades −₹758.
- Both halves lose (−₹2,463 / −₹2,341). 8 of 10 full months lose; March +₹12 and May +₹21 were flat.
- By sub-agent: SA1 75 trades −₹1,318 · SA2 220 trades −₹3,144 · SA4 6 trades −₹343. Every session and side is negative.
- **Costs vs fees:** break-even taker fee is 0.0116% (at 0.02% slippage) or 0.0316% (zero slippage). At 0.059% even perfect fills lose ₹2,779. Exact re-runs: fee 0.04% → −₹3,216, fee 0.02% → −₹1,163.

**Two trades Umar flagged on charts** (both reproduced exactly): 16 Oct London SA1 LONG (13:15 candle only *touched* the line, 54% beyond; next candle reversed) and 15 Oct NY SA2 SHORT (loosest rejection type; the buy breakout was the entry candle itself). Neither was a data error — the rules had gaps.

**Rule test** (optional switches, `reference/rule_test_summary.csv`) — none of them works:
| Variant | Trades | Net ₹ | Net/trade | PF |
|---|---|---|---|---|
| 0 Baseline | 301 | −4,805 | −16.0 | 0.53 |
| A SA1: touching candle ≠ breakout | 265 | −4,535 | −17.1 | 0.49 |
| B SA2: doji / long-wick only | 252 | −5,642 | −22.4 | 0.42 |
| C wait 1 candle, cancel if it reverses | 164 | −2,550 | −15.5 | 0.48 |
| D SA4-style veto | 260 | −6,918 | −26.6 | 0.34 |
| E = A + C | 149 | −2,319 | −15.6 | 0.47 |
| F = A + B + C | 117 | −2,538 | −21.7 | 0.37 |
Key insight on C: it removes 143 trades that lost ₹5,342 in the baseline (it *does* select losers), but the 158 trades it keeps made **+₹537 with immediate entry and −₹2,370 once entry is delayed a candle**. The next candle carries real information; waiting one candle to read it costs more than it saves. Smaller nets in C/E/F come mostly from fewer trades (fewer fees), not better expectancy.
Class findings on the 301 trades: touching SA1 candles (41) lost *less* per trade (−₹9.9) than clear ones (34, −₹26.8); SA2's loosest rejection type (85) is its *best* (−₹4.3/trade vs −₹20.6). 14 trades stopped out inside the entry candle lost ₹1,512 — avoiding all of them still leaves ≈ −₹3,300.

## Open decisions (Umar's call — ask, don't assume)
1. **Stop, or a different design?** Do not tune this one further. Costs take ~₹27/trade against ~₹82 average risk because stops are tight; a design aiming for bigger moves per trade with fewer trades is the only direction that changes that. That is a new strategy, not a tweak.
2. His **real** fee and slippage → re-run via `B.run(candles, TAKER_FEE=..., SLIPPAGE=...)` (exact; the trade set can change when costs change).
3. **Flip after a veto:** a veto currently *skips* the session. Umar expects the 15 Oct NY case to become a BUY. That needs his definition of what qualifies the buy (e.g. two clean closes beyond the line).
4. The live order path (stops via `create_tpsl`, trailing, partial close) is built from the exchange docs and a futures client library and covered by mocked-HTTP tests, but **has never touched the real exchange**. Only matters if he ever goes live: run `verify_api.py`, the locked `verify_orders.py`, then a supervised minimum-size first trade.

## How to work with Umar
- Plain language, short, direct. He is easily frustrated by repeated rounds and by being asked to run things — **front-load every check up front** instead of surfacing problems one at a time.
- Times in **IST**. Convert UTC timestamps from logs.
- Be honest about negative results; never present a filter that merely removes trades as an improvement. Never pick a winner by tuning on the same data you score it on.
- Never claim a change works without running it. After any engine change: tests pass, **baseline fingerprint unchanged** (`synth.signature` → `('b0b0fd5f8380f66b', 242)` with all optional rules off), and `B.audit_causality` reports 0 changed decisions.
- Keep the engine causal: detectors only ever see candles that have already closed.
- Optional rules (`SA1_REQUIRE_CLEAN`, `SA2_STRICT_REJECTION`, `VETO_MODE`, `VETO_AGENTS`) are all off by default — keep it that way.
- Never put API keys in files or commit them. They come from env vars only.

## `reference/` (outputs from the Colab runs)
`final_trades.csv` (301 trades, IST) · `final_signals.csv` (every session incl. no-setup) · `final_sensitivity.csv` (9 exact re-runs) · `rule_test_summary.csv` + `rule_test_trades.csv` (variants 0, A–F) · `SORB15_Backtest_Data_and_Calculator.xlsx` (fee/slippage calculator) · `railway_variables.txt` (settings the backtest used).
