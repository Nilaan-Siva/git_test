# The $100 Moonshot — running notes and learnings

Durable memory for the 30-day experiment (Aug 18 – ~Sep 17, 2026). Container crashes have
eaten conversation context more than once; this file and the journal are the record. Update
at every meaningful change or lesson. The journal (`data/daily100/journal.jsonl`) holds the
trades; this file holds the *thinking*.

## The bet, on the record

- $100 paper ledger (Alpaca paper acct PA3BH6B6X054), target $1,000 in 30 days at the user's
  explicit, twice-warned request. Stated odds: **<1–2% of reaching $1,000; most likely end
  is -50–90%.** Ruin line: ledger < $5 (can't afford any contract) ends the experiment early.
- Instrument: same-day (0DTE) SPY options — the only liquid, legal instrument where $100 has
  any mathematical path to 10x in a month. It pays for that path with ~-60% median daily
  theta bleed on losers.
- This is NOT the validated strategy. That is the put-credit-spread config on PR #1
  (5% risk / strict filters / 6 liquid tickers, walk-forward PASS at +$41.32/trade OOS),
  which is the thing that might someday deserve real money.

## The machine (current spec — moonshot100.py)

Entry (10:03 ET, Routine `moonshot100-open`), every gate in order: kill switch (ledger ≥ $5) →
Mon/Wed/Fri only → macro-event calendar (trades through pre-open releases; skips the Sep 16
FOMC day) → 09:50–10:50 ET entry window (added Aug 20; no late second looks, including from
me) → volatility-regime gate (14-day ATR% in [0.5%, 1.8%], the VIX-15–25 stand-in) →
30-minute opening range breakout, scanned across SPY/QQQ/IWM, strongest push wins →
≥0.1% escape beyond the range edge (added Aug 20; pokes are noise) → VWAP agreement → one
contract within 0.6% of spot costing ≤50% of ledger, else skip → +100% take-profit limit
resting immediately.
Midday (12:33 ET, `moonshot100-stopcheck`): sell if bid ≤ 50% of entry.
Close (15:47 ET, `moonshot100-close`): book the TP if it filled, else market-sell; reconciles
Alpaca's ~15:30+ expiry auto-liquidations from order history instead of erroring.
Kill switch (code-level): ledger < $5 → no new entries, experiment over, final report due.

Evidence stack: options.cafe ORB backtest (299 trades, 42.5% win, +100%TP/-50%stop,
+62.78%); SSRN 6355218 "Regime-Conditional Alpha in SPY 0DTE ORB" (46.8% → 65.4% win rate
under M/W/F + macro exclusion + VIX 15–25); quantish.io refinement (2.2 Sharpe).

## Ledger history

| Day | Date | Action | P&L | Ledger |
|-----|------|--------|-----|--------|
| 1 | Aug 18 | GLD flip +0.31, SOXS flip -0.66 (track-switch churn), SPY 769p 0.85→1.11 | +25.65 | **$125.65** |
| 2 | Aug 19 | SPY 772c 1.04→0.29 (midday stop) | -75.00 | **$50.65** |
| 3 | Aug 20 | no trade — Thursday, day-of-week filter | 0.00 | **$50.65** |

Two trades in, the sample is exactly what the on-record odds predicted: one +26, one -75,
net -49% of starting capital in 48 hours. Nothing here contradicts the bake-off's finding
that the average trade is negative; it is a two-trade sample of a negative-expectancy process.

## Day 2 lessons (Aug 19) — the false breakout post-mortem

The 10:03 decision correctly said "chop day, no trade." The loss happened because deploying
the multi-ticker scan mid-morning included re-running the entry at ~10:20, which caught the
very FIRST poke above the range — SPY was back inside the range within a minute of the fill.
The "real" breakout at 10:37 pushed on just 1.11x average volume (the head-fake signature),
peaked at 11:01, and bled all afternoon; the midday stop sold at 0.29 and saved ~$25 vs
holding to the close.

1. **Process error, mine:** one decision per day means ONE. A code-level entry window
   (09:50–10:50 ET) now makes late entries impossible rather than merely discouraged.
2. **Pokes are noise:** entries now require ≥0.1% escape beyond the range edge — backtested,
   improves avg/trade from -28.6% to -16.9% and win rate 28%→40% (5 trades/yr pass).
3. **Volume filter rejected honestly:** modeled no benefit with a 10:03 snapshot proxy;
   noted, not deployed. Over-filtering (both filters) = 2 trades/yr = useless for a 30-day run.
4. **Variance drag, the third arrival of the sizing lesson:** the both-filters variant had a
   POSITIVE average (+2.7%/trade) and still shrank $100 to $19.90 — a -95% loss needs +1,900%
   to recover. Arithmetic average up, geometric outcome down. Bet size beats bet quality;
   Kelly wept.

## Deep-dive research + strategy bake-off (Aug 20)

Swept the stop-loss literature, ticker-liquidity comparisons, unconventional setups (gap
fills, first-hour reversals, power hour), and structural alternatives (debit spreads vs naked
longs). Then TESTED the promising ones instead of trusting them. All numbers below are from
backtest_moonshot.py over 249 days of real SPY minute bars, modelled premiums.

| variant (all same entry stack unless noted) | trades | win% | avg/trade | $100 → |
|---|---|---|---|---|
| live bot, 95% bet | 5 | 40.0% | -16.9% | 2.46 (ruined) |
| + half out at +50% (partial profit) | 5 | 40.0% | **-26.9%** | 2.16 (ruined) |
| + breakeven trail after +50% | 5 | 40.0% | -16.9% | 2.46 (ruined) |
| + trail with continuous stop | 5 | 20.0% | -22.8% | 11.38 |
| **same trades, 50% bet** | 5 | 40.0% | -16.9% | **32.62** |
| **same trades, 25% bet** | 5 | 40.0% | -16.9% | **69.32** |
| ORB only (no M/W/F, no regime), 25% | 64 | 26.6% | -12.3% | 4.98 (ruined) |
| gap-fade, 25% | 144 | 27.8% | -28.6% | 4.31 (ruined) |
| gap-fade + continuous stop, 25% | 144 | 22.2% | -20.3% | 4.52 (ruined) |

### What the bake-off proves

1. **Every long-0DTE variant has negative average return.** Nine structurally different
   configurations — different entries (momentum, breakout, gap-fade), different exits
   (fixed TP, partial, trailing, three stop regimes), different filters — and not one is
   positive. The signal is not the problem. **Theta is.** Buying a decaying asset and
   needing a move big enough to outrun the decay is a losing proposition on average,
   and no entry filter fixes it.
2. **Bet size is the ONLY lever that changed survival.** Identical trades, identical win
   rate: 95% → $2.46 (dead), 50% → $32.62, 25% → $69.32. Fourth independent derivation of
   the same law. Sizing dominates selection.
3. **Partial profit-taking HURT** (-16.9% → -26.9%): in a strategy whose rare winners must
   be enormous, capping half of them at +50% removes the only thing paying for the losers.
   Textbook advice, wrong for this payoff shape. Test everything.
4. **Trailing stops did nothing** at these thresholds (too few trades reach the +50% arm).
   The literature's hybrid (fixed stop → trail once profitable) is sound in general;
   it has nothing to work with here.
5. **The gap-fill edge is real but not tradeable this way.** Gaps fill 59-69% of the time
   (structural: thin overnight liquidity overshoots, the session corrects it) — but 144
   modelled trades still lost, because the option decays faster than the gap fills. An edge
   in the *underlying* is not automatically an edge in *options on* the underlying.

### The synthesis that matters

The main repo strategy **sells** put credit spreads — it collects theta. The moonshot
**buys** naked 0DTE options — it pays theta. Same market, opposite side of the same trade.
One passes walk-forward validation; the other loses in every configuration tested. That is
not a coincidence, and it is the single most useful thing this whole experiment produced.

### Adopted / rejected from the research

- **SPY-only for the scan (adopt):** SPY 0DTE spreads are $0.01-0.03 wide; QQQ next, IWM
  widest. At $100 scale, crossing a wider spread is a pure tax. QQQ/IWM stay in the scan
  only because the diversification of breakout opportunities was the user's explicit ask;
  noted that SPY fills are strictly better.
- **Debit spreads instead of naked longs (noted, not adopted):** defined risk, lower cost,
  less theta exposure — the research's clear recommendation for small accounts. Not adopted
  because a spread's cost at our ledger size collapses the position count to ~1 contract
  with worse fills; revisit if the ledger grows.
- **Selling premium instead of buying it (the real answer):** that is literally the main
  strategy in this repo. The moonshot cannot adopt it — credit spreads need margin far
  beyond a $100 ledger. This is the honest wall the experiment keeps hitting.

## Sizing change (Aug 20) — 95% → 50% per trade

Acted on the bake-off's one durable finding. BUDGET_FRACTION 0.95 → 0.50, plus a new
MAX_OTM_PCT = 0.6% quality floor on strike distance.

Why the floor exists: one option contract is indivisible, so at a small ledger a smaller
budget does not buy a smaller slice of the same trade — it buys a *further out-of-the-money*
strike, which is a different (worse) bet with a wider spread. The backtest's "50% bet" row
assumed the same ATM contract at half size, which is impossible below roughly $200 of ledger.
The floor keeps the bot honest: near-the-money or no trade.

Consequence at today's $50.65 ledger: budget $25.32 → max contract price $0.25, strike within
±$4.62 of spot on SPY. Some days nothing will qualify and the bot will sit in cash. **Those
skips are the sizing discipline working, not the bot failing.** As the ledger grows the floor
stops binding; if the ledger shrinks it binds harder, which is the correct direction — a
smaller account should trade less, not gamble harder to catch up.

Honest cost of the change: the "four consecutive doubles to $1,000" geometry is gone. At 50%
sizing a double only grows the ledger 50%, so $1,000 now needs many more good days. Traded
certain-ruin-with-a-lottery-ticket for a slower path with survivable variance. The on-record
odds of hitting $1,000 go DOWN, not up; the odds of the experiment still being alive on day 30
(and therefore of learning anything) go up a lot.

## Day 1 lessons (Aug 18)

1. **The win was luck, and we can prove it.** The put was bought under the old naive rule
   (mid-day "below the open" check) — `backtest_moonshot.py` shows that rule averages
   **-31%/trade over 249 days**. Right outcome, wrong process. Don't confuse the two.
2. **Exits dominate entries.** Every entry filter combined moved modeled avg/trade by a few
   points; the -50% stop structure moved it ~6–10. The midday stop-check came out of this.
3. **The theta tax is the boss.** Median no-stop trade bleeds ~-60% by 15:45. The +100% TP
   hits only ~19–23% of days. This is why 95% sizing compounds to ruin in every modeled
   variant — the published +62% versions of this strategy sized SMALL. The Dummies 1% rule
   and our own model agree; the moonshot knowingly violates both. That's the bet.
4. **Sell before the bell, always.** Day 1's put was worth $1.63 at expiry vs the $1.11
   forced exit — and it doesn't matter. ITM options held through expiry become stock
   assignments; the $52 of hindsight was the price of not being short 100 SPY overnight.
5. **Infrastructure is a position too.** A container crash ate the turn that traded SOXS;
   the journal and git caught what memory lost. Everything important gets journaled and
   pushed the moment it happens.

## Backtest reference (backtest_moonshot.py, 12mo SPY minute bars, BS-modeled premiums)

| variant | trades | win% | avg/trade | $100 endpoint |
|---------|--------|------|-----------|----------------|
| naive above/below open | 249 | 29.3% | -31.1% | ruin (2 days) |
| ORB | 65 | 29.2% | -28.4% | ruin |
| ORB+VWAP | 65 | 29.2% | -28.4% | ruin |
| full stack | 32 | 28.1% | -34.6% | ruin (~7 wks) |
| full stack + midday stop | 32 | 28.1% | -28.6% | ruin, slower |
| full stack + continuous stop (published) | 32 | 21.9% | -24.2% | ruin, slowest |

Model limits: flat 14% IV, no smile, no IV crush, no spread crossing, snapshot entry at
10:03 rather than entry on the break. Real fills are worse; real premiums are somewhat
cheaper. Ranks structures; does not price destiny. Upgrade path: paid intraday options data.

## Bot-craft audit (LuxAlgo guide + r/algotrading sweep, Aug 18)

Already in place: paper-first, journal-everything, real-time alerting via Routines, cost
modeling in the main bot's backtests, volatility pause, walk-forward validation (main bot),
retry-and-reconcile on broker/infra failures.
Adopted from the sweep: code-level kill switch (was prompt-level only); this notes file as
persistent memory. Deliberately violated, on the record: 1–2% position sizing (moonshot is
95% by design — that IS the moonshot). Noted for later: slippage haircut in the moonshot
backtest; entry-on-the-break rather than 10:03 snapshot (needs intraday event-driven runs).
Best line of the sweep: *"Most trading bots don't fail because the code is bad — they fail
because someone believed automation could replace thinking."*

## Idea backlog (not implemented, with reasons)

- Entry-on-break (event-driven) vs the 10:03 snapshot — the largest remaining upgrade;
  needs a polling/streaming runner, not cron Routines. Still unbuilt.
- Real intraday options data for honest backtests — paid (Massive/Polygon paid tier, CBOE).
  Every backtest number here is BS-modelled and therefore optimistic on fills.
- Slippage haircut in the moonshot backtest — modelled fills assume mid; real 0DTE crossing
  costs would push every already-negative variant further negative.
- TESTED AND REJECTED Aug 20 (see bake-off): volume-confirmation on the breakout candle (no
  modelled benefit), partial profit-taking (actively harmful, -16.9%→-26.9%), breakeven
  trailing stops (no effect at these thresholds), gap-fade entries (144 trades, -28.6%/trade).
- REJECTED earlier: prop firms (~7% ever see a payout — a fee machine); crypto leverage (not
  on Alpaca paper; liquidation math is the same trap); debit spreads (right idea per the
  research, but at a $50 ledger the fills and contract count collapse — revisit if it grows).

## Parallel tracks (context for whoever reads this cold)

- **SPY weeklies pilot: COMPLETE and validated (Aug 20).** Adding every-Friday expirations to
  the validated put-credit-spread strategy did exactly what it was meant to: 6-month SPY went
  5 trades/+$176.76 per trade → **8 trades/+$351.72 at 87.5% win rate**, chain coverage 95%→99%,
  and the old #1 no-trade reason (`no_expiration_in_window`, 188+ hits) collapsed to 2. The
  binding constraint is now IV-rank, a quality gate, rather than a data gap. The single-ticker
  18-month walk-forward came back INCONCLUSIVE — 0/1/5/8 trades per fold against a 10-trade
  bar — which is a statement about sample size, not about the strategy.
- **5-ticker weeklies extension running** (QQQ, DIA, IWM, XLK, GLD; launched 12:28 UTC Aug 20,
  free tier, ETA ~Aug 24). Its completion triggers the properly-powered 6-ticker walk-forward —
  the real verdict on whether weeklies upgrade the validated strategy.
- PR #1 documents the validated-strategy arc, including the mark-to-market engine bug found
  and fixed on Aug 18 that had corrupted every prior backtest number in the project.
- Real-money ladder advice to the user stands: side income → savings milestones → the
  validated strategy at $5k+ — the moonshot is a paper demonstration, not the plan.

## Aug 24 — the swing pivot (parallel track: swing1000)

User: "Let's do swing trading and see how much profit we can attain... look up more strategies
like fair value gaps and etc." Bake-off run the same day on 3y of free Alpaca daily bars,
5 bps/side, close-only fills, one position at a time (scripts/backtest_swing.py):

| strategy | 3y return | trades | win% | avg/trade | maxDD | verdict |
|---|---|---|---|---|---|---|
| 20d breakout | **+82.1%** | 24 | 54% | +2.88% | 18% | deployed |
| SPY buy-and-hold | +68.7% | — | — | — | ~25% | benchmark |
| momentum rotation | -14.4% | 33 | 45% | +0.21% | 55% | rejected |
| RSI(2) Connors | -26.9% | 135 | 58% | -0.21% | 49% | rejected |
| fair value gap | **-90.2%** | 81 | 43% | -1.40% | 92% | rejected, emphatically |

Lessons worth keeping:
* FVG finally got its numeric burial: 43% win rate with a 2R target means the "imbalance
  fills" story simply does not survive costs on the daily timeframe. It was rejected on
  research before; now it is rejected on measurement.
* RSI(2) is the blog world's favorite and it LOST here — high win rate (58%) hiding a
  negative expectancy, the classic mean-reversion shape. Win rate is not edge.
* The breakout's edge lives in single stocks: ETF-only collapses to +8.7%. Concentration
  is the price of the return; the 10-day-low exit is the only bear-market defense.
* Robust to 2x slippage (+77.9% at 10 bps). 24 trades is still a small sample; the window
  was a bull tape. On the record: a bear year will likely give back a chunk.

Deployed: scripts/swing1000.py, $1,000 paper ledger, 16-ticker universe (9 ETFs + 7 megacaps),
daily Routine swing1000-daily at 19:35 UTC weekdays, kill switch at $100 equity.
Moonshot100 continues in parallel on its own $100 ledger and Routines (day 5: skip).
