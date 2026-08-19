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

Entry (10:03 ET, Routine `moonshot100-open`): Mon/Wed/Fri only → macro-event calendar gate
(trades through pre-open releases; skips the Sep 16 FOMC day) → volatility-regime gate
(14-day ATR% in [0.5%, 1.8%], the VIX-15–25 stand-in) → 30-minute opening range breakout →
VWAP agreement → one contract, nearest-the-money that fits ~95% of ledger, walking OTM only
as far as the budget forces → +100% take-profit limit resting immediately.
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

- Volume-confirmation on the breakout candle — plausible, unmodeled; add to backtest first.
- 15-min opening range variant — more trades, weaker levels; backtest first.
- Entry-on-break (event-driven) vs 10:03 snapshot — likely material improvement; needs a
  polling/streaming runner, not just cron Routines.
- Real intraday options data for honest backtests — paid (Polygon paid tier / CBOE).
- Prop-firm route — REJECTED: ~7% ever see a payout; fee machine.
- Crypto leverage — REJECTED: not on Alpaca paper; liquidation math is the same trap.

## Parallel tracks (context for whoever reads this cold)

- Weeklies backfill (SPY pilot) running for the validated strategy — its check-ins are
  separate Routines. PR #1 documents the whole validated-strategy arc including the
  mark-to-market engine bug found and fixed on Aug 18.
- Real-money ladder advice to the user stands: side income → savings milestones → the
  validated strategy at $5k+ — the moonshot is a paper demonstration, not the plan.
