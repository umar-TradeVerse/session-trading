# SORB-15 — session opening-range breakout bot (multi-symbol, CoinDCX futures)

Owner: Umar. These are **his rules** for a session high/low ("liquidity") breakout strategy. Status as of **5 Nov 2026**.
His original ask was simple: *backtest the rules I gave on real data.* That line used to also say "don't drag him into
credentials or deployment unless he asks" — he has since asked, repeatedly and explicitly: DigitalOcean droplet setup,
new API keys pasted in chat, going live with real money on 3 symbols. Credentials and deployment are now squarely in
scope; the caution that still applies is the one below (real financial risk needs explicit confirmation, not silence).

## ⚠ REAL MONEY IS OFF (as of 7 Nov 2026, 00:3x IST) — read before touching `sorb15-live`
Umar asked for "only SORB-15 paper for now" on 5 Nov 23:24 IST (in his liquidity-bot session). `sorb15-live` was
locked off by a kill switch: drop-in `/etc/systemd/system/sorb15-live.service.d/off-switch.conf`
(`ConditionPathExists=!/etc/sorb15-live.disabled`) + the file `/etc/sorb15-live.disabled`. On 6 Nov 23:16 IST a
session WITHOUT that context saw it off, called it unexplained, and Umar re-enabled it — that was a mistake, not a
decision. Re-locked 7 Nov: file present, `sorb15-live` inactive, `sorb15-live-watchdog.timer` disabled. The unit
itself was left `enabled` (harmless: at boot the condition check skips it while the file exists). Paper
`sorb15.service` (Gold/Silver) keeps running. **Never remove `/etc/sorb15-live.disabled` or start `sorb15-live`
unless Umar explicitly asks to go live again in that conversation.** If he does:
`rm /etc/sorb15-live.disabled && systemctl enable --now sorb15-live.service sorb15-live-watchdog.timer`.
No real-money entry ever happened (lag guard refused a stale NatGas signal on the 6 Nov restart), so the
exchange-side stop (`create_tpsl`) is still unproven on a real position.
Also not reflected below yet: since commit 30bf0fb (5 Nov) a BTC order-book wall no longer AVOIDs the trade — it
registers a liquidity-hunt-reversal watch (`register_hunt_watch` / `check_hunt_watches` / `enter_hunt_reversal`)
and trades the reversal. Live-only, never backtested.

## LIVE DEPLOYMENT (as of 5 Nov 2026) — supersedes the engine section below until noted otherwise
Two independent systemd services on a DigitalOcean droplet (`tradeverse-bots`, 206.189.138.66, IP whitelisted on
CoinDCX), both running the identical `sorb15/main.py` / `sorb15/backtest.py` engine described in the next section —
split only by which symbols each is allowed to touch and by `DRY_RUN`:
- **`sorb15-live.service`** — **REAL MONEY**. `DRY_RUN=false`. Symbols: **BTC, Oil, NatGas** only
  (`ACTIVE_SYMBOLS=BTC,Oil,NatGas` in `.env.live`). Capital **₹10,042.53** — the actual usable futures-wallet balance
  Umar confirmed, not a round number (tied to `DRY_RUN` itself in `main()`, so it can never drift via a stale env var).
  State/heartbeat: `/tmp/sorb15_live_state.json`, `/tmp/sorb15_live_heartbeat`. Own watchdog:
  `sorb15-live-watchdog.timer` → `sorb15-live-watchdog.service` → `/usr/local/bin/sorb15-live-watchdog.sh` (same
  20-minute-stale-heartbeat restart logic as the paper watchdog, independent of it).
- **`sorb15.service`** — paper only, unchanged `DRY_RUN=true`. Symbols: **Gold, Silver** only
  (`ACTIVE_SYMBOLS=Gold,Silver` in `.env`). Capital ₹10,000 (the round number it's always backtested against).
- `main.active_symbols()` reads `C.ACTIVE_SYMBOLS` (comma-separated) to split `BT.SA134_SYMBOLS` between the two
  processes — empty/unset means every symbol (the pre-split single-service default).
- **Verified the real order path once**, deliberately, per the long-standing open-decision-4 below: `verify_orders.py`
  (places one real resting limit order 50% below market, confirms it, cancels it) against the live keys. Needed two
  fixes along the way, both committed: the test's own quantity undershot CoinDCX's REAL minimum order value (₹6,120
  on BTC — noticeably higher than what `instrument()`'s own `min_notional` field implied), and the listing check fired
  before a fresh order left CoinDCX's own `'initial'` status. Neither issue touches the live engine itself (which uses
  market orders + position-based stop tracking, never `open_orders()`).

### Slot-pairing policy (`SA134_ENFORCE_COMPANION_PAIRS`, on for both services)
The 2 shared slots may only ever hold one of `backtest.SA134_COMPANION_PAIRS`: **BTC+Oil, BTC+NatGas, or Oil+NatGas**.
Gold and Silver are in none of them — each can still open **solo**, but never as the second position alongside
anything (`backtest.compatible_with_open()`). Verified empty of disallowed overlaps across a full 5-symbol backtest
before shipping.

### Live-only order-book overlay (`ORDERBOOK_FILTER`, on for both services — effectively BTC-only)
Umar's explicit ask: "liquidity or orders which are present, long or short." CoinDCX's real public order book
(`coindcx.orderbook`, no auth) — checked directly against the live API, this **only exists for BTC**; Gold/Silver/Oil/
NatGas all return `422 Invalid pair`, every naming variant tried. Delta Exchange was checked too as an alternative
for the other 4 (no Oil/Silver/NatGas there at all; "Gold" is only PAXG, a stablecoin, not a comparable commodity
CFD) — no fix available there either. So this overlay is real for BTC and a confirmed no-op (fails open) elsewhere.
- **Entry gate**: skip the trade if a wall (`ORDERBOOK_WALL_MULT`× the average resting size) sits within
  `ORDERBOOK_GATE_PCT` of entry, against the breakout direction.
- **Stop placement**: search up to `ORDERBOOK_STOP_SEARCH_PCT` beyond the existing fixed-buffer stop for a
  protective-side wall; if found, move the stop just past it. Only ever widens, never tightens.
- **Cannot be backtested** — CoinDCX archives no historical order book anywhere, confirmed directly (every
  historical-depth/OI endpoint tried 404s). This is the one piece of the live engine that has never been, and can
  never be, validated against history the way everything else here has.
- **First live data point (5 Nov, NY session, BTC)**: a textbook clean SA4 SHORT (entry 78,262, correct stop, right
  on time) was gated by a 2.19 BTC wall (5.3× average) sitting 0.07% away — and the wall didn't hold; price blew
  through it and fell to the 76,000s in the same candle. A real, confirmed false-avoid. Defaults loosened the same
  day in response: `ORDERBOOK_WALL_MULT` 5.0→**8.0**, `ORDERBOOK_GATE_PCT` 0.3%→**0.15%** — gate stays on (Umar wants
  to keep watching it live, not disable it), just fires less often. Outcome of the loosened version: unknown, watch it.

### Session times shifted for DST (`main()`'s `C.override(SESSIONS=...)`, both services)
`config.SESSIONS` itself is untouched (so the baseline-fingerprint regression test still means what it always has) —
`main()` overrides it for the live/paper processes only. New York's original 19:00 IST open matched NYSE's real
9:30am-ET open **exactly** during US daylight saving — confirmed independently via web search, not just computed.
Both the US (fell back 1 Nov) and UK (fell back 25 Oct) are now on winter/standard time, so the real anchors moved:
NYSE now opens at 20:00 IST, LSE at 13:30 IST. Deployed: **London 15:00–16:45–20:00, New York 20:00–21:45–02:15**
(both +1h; Tokyo untouched — Japan has no DST, so its real open never moves).
- **Backtested worse, deployed anyway at Umar's explicit instruction to monitor live rather than trust the
  backtest**: shifting both London+NY, full period net −₹26,485 vs −₹2,294 baseline; NY-only, −₹15,648. Neither
  variant improved on the stale-but-empirically-better fixed blocks. Outcome of running it live: unknown, watch it.
- Fixed a real bug this surfaced: `main.ALL_SESSIONS` was a module-level dict frozen at import time, BEFORE
  `main()`'s override ever runs — the shift would have been silently ignored by `window()`/`active_session()`.
  Converted to `main.all_sessions()`, read fresh every call; regression test added so this can't silently recur.

### Built, tested, NOT deployed (backtested worse or didn't survive scrutiny — kept for reference, stay off)
- **`SA134_LIQUIDITY_HUNT_REVERSAL`** — generalizes SA1's single-first-candle sweep-reversal flip to any candle in
  the session, gated by a volume filter (`sa.good_volume`). The tight version (pullback must be the very next candle)
  is barely net-positive (+₹82 full period, first ever non-negative number) but doesn't catch the Gold 4-Nov example
  Umar flagged (that move was a multi-candle grind, no sharp pin-bar). A looser multi-candle pullback window tested
  far worse (−₹30,717). Fragile either way — not a real edge yet.
- **`SA134_HUNT_REENTRY`** — Umar's "book the 1R leg, don't move to breakeven" redesign (legs 25/25/25/25 at
  1R/2R/3R/4R, stop never moves) plus two watch-for-reentry rules (continuation after the 1R leg, reversal after the
  original stop breaks further) gated by the same volume filter. Fixes the exact 2-Nov NatGas case it was built for
  (loss cut from −₹952 to −₹513) but makes the AGGREGATE portfolio worse (−₹10,319 vs −₹2,294 baseline) — it helps
  the one trade you can see and hurts the ones you can't.
- Both are real, causality-audited code (`backtest._sa134_manage_hunt`, `sa134_entry_signal`'s hunt-reversal block),
  just toggled off (`SA134_LIQUIDITY_HUNT_REVERSAL=False`, `SA134_HUNT_REENTRY=False` by default) — don't turn them
  on without re-validating, and don't present either as a win without saying the above.
- **`SA134_PIVOT_CONFLUENCE`** (added 9 Nov) — Umar's own chart addition (standard-Fibonacci daily pivot) combined
  with the session-breakout level for SA1/SA4: a breakout only counts if the broken H/L also sits within
  `SA134_PIVOT_TOLERANCE_PCT` (0.3%, mine, untuned) of a same-side daily pivot (R1-3/P for a LONG, S1-3/P for a
  SHORT). Unlike the order-book overlay, this IS fully backtestable — the pivot comes from the prior day's candles,
  nothing live-only. Causal by construction: `backtest.daily_pivot_fib` only ever slices `candles` strictly before
  the current day's Tokyo-open index. **Tested on BTC, current live config, full period (31 Dec 2025 – 9 Nov 2026)
  and the Oct–Nov 2026 window specifically — it does not help:**
  | | n | net ₹ | win rate | avg/trade ₹ |
  |---|---|---|---|---|
  | Full period, baseline | 299 | −22,337 | 37.5% | −74.70 |
  | Full period, +pivot confluence | 187 | −18,379 | 37.4% | **−98.28** |
  | Oct–Nov, baseline | 38 | −3,277 | 36.8% | −86.23 |
  | Oct–Nov, +pivot confluence | 24 | −3,508 | 45.8% | **−146.18** |
  Win rate is flat-to-slightly-better; average economics per trade get WORSE both times, and the Oct–Nov net loss is
  actually larger despite 14 fewer trades — the filter removed 4 of 14 winners (₹3,002 of winning P&L) along with
  13 of 24 losers, and the slot-timing side effect of removing trades let 3 new (net −₹616) trades in that baseline
  never saw. Smaller headline loss on the full-period row is trade-count shrinkage, not better selection — same
  pattern as every other filter tried in this project (see "Rule test" further down). Code stays in
  (`sorb15/backtest.py`'s `daily_pivot_fib`/`_pivot_confluent`, 12 new unit tests, 173/173 passing, baseline
  fingerprint and existing causality audit untouched), **default OFF**, same as the two above.
- **On "does this need a 5th sub-agent?"** (asked 9 Nov, alongside the pivot test) — the Oct–Nov failure data says
  no, not yet, and points somewhere else instead. Broken down by exit reason (full period): `day_end` exits are
  net **+₹37,191** over 225 trades (49% win rate, avg +₹165) — the staged-TP/day-end side of this engine is fine.
  Virtually the entire net loss is `stop_loss`: 72 trades, net **−₹61,998**, 1.4% win rate (expected — "stop_loss"
  means it went against entry), avg **−₹861/trade** — 5.2× the size of an average day_end outcome. A new entry
  detector doesn't touch this; it's the "Known risk" below (fixed-margin sizing against a variable-width stop)
  showing up exactly as predicted. Also by sub-agent (Oct–Nov): SA1 is still the stronger half, now net
  *positive* (+₹437, 10 trades, 40% win) same as the older BTC-only finding above; SA4 is the one losing money
  (−₹3,714, 28 trades, 35.7% win) and is also where most of the big stop-losses sit. Recommendation: fix the known
  sizing issue (bound risk per trade regardless of stop width) before adding a 5th sub-agent — that lever is bigger
  than anything a new entry filter could plausibly add, and a prior attempt at a different new-entry idea
  (`SA134_LIQUIDITY_HUNT_REVERSAL`, above) was already "fragile... not a real edge." Not built; ask first.
- **`SA134_PIVOT_PROFIT_BOOKING`** (added 9 Nov) — Umar's management-side redesign: while a position is open, watch
  for price reaching a same-side daily pivot (R1-3 for a LONG, S1-3 for a SHORT) AND the very next candle being a
  `reversal_candle()` against the trade (same primitive the hunt-reversal features already use — no new indicator).
  On confirmation, close the WHOLE remaining position there and then watch the trade's OWN original level for a
  fresh CLEAN re-break in the SAME direction before day-end (`clear_of_level`, the identical test SA1/SA4 use) — found
  → re-enter and resume ordinary staged-TP management (a temporary pullback); not found by day-end → do nothing
  further (a genuine reversal — there's no separate rule for that case beyond the absence of a re-entry). Entry rules
  and liquidity detection untouched, exactly as asked. **Tested on BTC, same config, full period and Oct–Nov:**
  | | n | net ₹ | win rate | avg/trade ₹ |
  |---|---|---|---|---|
  | Full period, baseline | 299 | −22,345 | 37.5% | −74.73 |
  | Full period, +profit booking | 471 | **−35,126** | 36.5% | −74.58 |
  | Oct–Nov, baseline | 38 | −3,285 | 36.8% | −86.45 |
  | Oct–Nov, +profit booking | 58 | **−5,088** | 36.2% | −87.72 |
  Split by piece, the picture is more interesting than the totals: the `pivot_profit_booking` exit itself is
  genuinely good in isolation (full period: 178 occurrences, net **+₹28,109**, 49% win, avg +₹158 — better than
  baseline's day_end average of +₹165 was spread over *more* trades, worse per-trade than it). Oct–Nov original legs
  alone (ignoring re-entries) net −₹2,002 at avg **−₹53/trade — better than baseline's −₹86**. But the continuation
  re-entry half loses money on both: Oct–Nov −₹3,086 over 20 re-entries (avg −₹154), full period's stop_loss bucket
  (where many re-entries land) stays at −₹896/trade. Net effect: **worse than baseline both windows**, because the
  re-entry mechanism gives back more than the early-exit mechanism saves — the same shape of result as
  `SA134_HUNT_REENTRY` above ("book early, try to re-enter" doesn't have an edge in this engine so far, under two
  different trigger designs now). Code is in (`backtest._sa134_manage_pivot_booking`, wired only into
  `run_sa134_portfolio`, 9 new unit tests incl. hand-built genuine-reversal and temporary-pullback cases, 182/182
  passing, baseline fingerprint and causality untouched), **default OFF**. Worth knowing separately if useful later:
  the exit-only half (book profit at a confirmed pivot reversal, no re-entry) tested better than baseline in
  isolation — that's a different, smaller change from what was built here, not yet tried on its own.

### Known risk, carried over, still unresolved
Fixed-margin sizing against a variable-width stop (same issue flagged 27 Oct) means per-trade risk is uncontrolled.
The full 5-symbol portfolio backtest's max drawdown (−₹36,356 on ₹10,000 capital, baseline config) is still a large
multiple of starting capital. This is now live with real money on 3 symbols and has not been fixed. **Confirmed
again 9 Nov** on BTC alone: `stop_loss` exits average −₹861/trade vs `day_end` exits averaging +₹165/trade (see
above) — this is the single biggest lever on this engine's economics, bigger than any entry-side filter tried so far.

### Infra
DigitalOcean droplet `tradeverse-bots` (Ubuntu 24.04, `s-1vcpu-1gb`), non-root user `sorb15`, checkout at
`~/sorb15-bot`, Python venv. `root` SSH access needs the droplet's web console (Claude Code's own permission
classifier blocks placing real orders or enabling new persistent systemd units over SSH from here — those steps are
run by Umar directly in the DigitalOcean console, with exact commands provided). `git pull` + `systemctl restart` on
both services is the deploy step for ordinary code changes.

## CURRENT LIVE ENGINE (as of 27 Oct 2026) — the shared engine both services above run
The live bot (`sorb15/main.py`) no longer runs the SA1–SA5 engine described in the rest of this file.
It now runs **SA1 + SA4 + Tokyo continuation**, Umar's explicit redesign, multi-symbol since 28 Oct
(BTC, Gold, Silver, Oil, NatGas — `BT.SA134_SYMBOLS`), migrated off Railway to the DigitalOcean droplet
described above, and now split live/real-money vs paper per the section above this one:
- Sessions (IST): **Tokyo 05:30–07:15–14:00**, then London and New York (`config.py` — shifted +1h on
  the live/paper processes as of 5 Nov, see above; these are the UNSHIFTED values `config.py` itself
  still holds). One position per symbol at a time — a later session is skipped entirely for that
  symbol while an earlier one is still open. Two symbols can run concurrently, up to
  `BT.SA134_MAX_SLOTS` (2), now gated by the companion-pairs policy above.
- Only SA1 and SA4 fire, both requiring a CLEAN breakout (closes beyond the line with ≤10% of the
  candle back inside the range — tightened from the old 15%). SA1 also catches a liquidity-sweep
  reversal: an impure first breakout followed by a confirmed opposite close flips the trade.
- Stop = the broken level ± **1.5%**, set once at entry and **never moved** (no breakeven, no trail —
  Umar's explicit call: "the sl should remain as it not to be shifted").
- Targets: **50% at 2R, 25% at 3R, 25% at 4R**; whatever is still open at the next Tokyo open (the
  "day end") closes there, whichever comes first besides the stop.
- Sizing is **fixed margin**, not risk-targeted: margin/slot = capital ÷ `MAX_SLOTS` (2), exposure =
  margin × 10x leverage. `sizing.qty_for_fixed_notional()`. Capital is ₹10,042.53 on the real-money
  service, ₹10,000 on the paper one (see above) — deliberately hardcoded via `C.override(...)` at the
  top of `main()`, tied to `DRY_RUN` itself, so a stale or missing env var on the droplet can never
  silently drift either service from what was backtested.
- Shared source of truth for both backtest and live: `sorb15/backtest.py` —
  `sa134_entry_signal`, `TOKYO_SESSION`, `SA134_SL_BUFFER_PCT`, `SA134_TP_R`, `SA134_TP_FRACS`,
  `SA134_SYMBOLS`, `SA134_MAX_SLOTS`, `SA134_COMPANION_PAIRS`. Live-only order management is
  `sorb15/main.py`'s `enter()` / `manage()`, plus the order-book overlay functions above it.
- **Backtest result (BTC-only, Jan–Oct 2026, this config): 260 trades, net −₹13,134, PF 0.85, max
  drawdown −₹20,516 on ₹10,000 capital — net LOSING, deployed anyway at Umar's explicit instruction
  for live paper-data validation**, same as the earlier SA4/SA5 decisions below. Known issues, not
  yet fixed: (a) SA4 can still preempt a later SA1 liquidity-sweep flip within the same session
  (the 14 Sept case) — unresolved; (b) see "Known risk, carried over" above — still unresolved, now
  live with real money. SA1 alone, isolated, is the one profitable piece so far (58 trades, net
  +₹2,824, PF 1.18).
- Everything from "Where things stand" down to "Open decisions" below is the **OLD SA1–SA5 engine**:
  historical record of the original backtest, kept for reference, no longer what's running live.

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
sorb15/coindcx.py     futures client (signed GET-with-body wallet, create_tpsl stops, never retries orders,
                      + orderbook()/nearest_wall() -- public, live-only, BTC-only in practice)
sorb15/main.py        live loop (paper mode when DRY_RUN=true); active_symbols()/all_sessions() read their
                      ACTIVE_SYMBOLS/SESSIONS config fresh each call -- never bind these at import time
sorb15/data.py report.py notify.py
synth.py              deterministic fake candles + baseline fingerprint (tests only)
test_local.py         152 offline tests          verify_api.py (read-only) / verify_orders.py (locked,
                      places one real cancel-only order -- actually run once, 5 Nov, see above)
reference/            result files from the Colab runs AND later live/portfolio runs (see below)
```
Run tests: `DRY_RUN=true python test_local.py` → must print `152 passed, 0 failed`.
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
4. ~~The live order path... has never touched the real exchange~~ — **done, 5 Nov**: `verify_api.py` and
   `verify_orders.py` both run against the real exchange with the DigitalOcean-whitelisted keys; two real bugs in
   the test script itself fixed along the way (see "LIVE DEPLOYMENT" above). He then went live with real money on
   BTC/Oil/NatGas the same day. Stop-loss placement specifically (`create_tpsl`) is still only validated by whatever
   the first real trade on the live service actually does — watch the first one closely.

## How to work with Umar
- Plain language, short, direct. He is easily frustrated by repeated rounds and by being asked to run things — **front-load every check up front** instead of surfacing problems one at a time.
- Times in **IST**. Convert UTC timestamps from logs. Careful with DST now that London/NY are live-shifted — see above.
- Be honest about negative results; never present a filter that merely removes trades as an improvement. Never pick a winner by tuning on the same data you score it on. This held even for real-money decisions on 5 Nov: told him plainly when the order-book gate false-avoided a good trade, and when the session-time shift backtested worse — he deployed both anyway, explicitly, having seen the honest number first.
- Never claim a change works without running it. After any engine change: tests pass, **baseline fingerprint unchanged** (`synth.signature` → `('b0b0fd5f8380f66b', 242)` with all optional rules off), and `B.audit_causality` reports 0 changed decisions. For a change to a fundamental constant (session times, symbol roster) rather than an optional rule, the fingerprint test isn't the right check — apply it via `main()`'s `C.override(...)` instead, so `config.py`'s raw defaults (and the regression tests built on them) stay meaningful.
- Keep the engine causal: detectors only ever see candles that have already closed. This extends to the hunt-reentry management code (`_sa134_manage_hunt`) — verify causality there by scrambling candles strictly after each leg's own entry and confirming that leg's parameters don't change, not just by eyeballing the logic.
- Optional rules (`SA1_REQUIRE_CLEAN`, `SA2_STRICT_REJECTION`, `VETO_MODE`, `VETO_AGENTS`, `SA134_LIQUIDITY_HUNT_REVERSAL`, `SA134_HUNT_REENTRY`) are all off by default — keep it that way unless he explicitly re-approves after seeing fresh numbers.
- Never put API keys in files or commit them. They come from env vars only. He pastes credentials directly in chat when asked (his explicit choice) — treat them as sensitive in handling, not as something to refuse.
- **He now explicitly wants deployment and real-money operations** — the opposite of the original "don't drag him into credentials or deployment" rule. Still confirm explicitly before anything irreversible (going live, capital changes, real orders) — he has been giving that confirmation directly and clearly, not resisting being asked.
- **Platform limitation, not Umar's preference**: Claude Code's own permission classifier blocks (a) placing real orders on the exchange and (b) enabling new persistent systemd units (`systemctl enable --now` on a freshly-created unit) over SSH from this session. Writing the unit file is fine; starting it the first time is not — hand Umar the exact commands for those two categories specifically and let him run them in the DigitalOcean web console. Ordinary `git pull` + `systemctl restart` on an *already-enabled* service has worked fine directly.

## `reference/` (outputs from the Colab runs, plus later live/portfolio runs)
`final_trades.csv` (301 trades, IST) · `final_signals.csv` (every session incl. no-setup) · `final_sensitivity.csv` (9 exact re-runs) · `rule_test_summary.csv` + `rule_test_trades.csv` (variants 0, A–F) · `SORB15_Backtest_Data_and_Calculator.xlsx` (fee/slippage calculator) · `railway_variables.txt` (settings the old Railway backtest used, now historical) · `sa134_portfolio_*` / `variant_sa134_*` (the multi-symbol SA1+SA4+Tokyo engine's own variant runs) · `Last7Days_Portfolio_PnL.xlsx` + `last7days_portfolio_trades.csv` (the Oct 29–Nov 5 report, backtest-based substitute since Railway's logs were gone and the DO deployment was too young at the time) · monthly `*_Profit_Loss.xlsx` files.
