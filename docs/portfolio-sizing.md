# Portfolio sizing: the two bots as one book

Written Aug 25 2026, after the research briefing's three sizing recommendations were tested.
Two of the three did not survive contact with measurement. This file records what replaced them.

## Recommendation 1: volatility-targeted sizing for swing1000 — REJECTED

Backtested in `scripts/backtest_swing_volsizing.py` against the exact live config, same
universe, same 5 bps/side, same close-only fills, 793 days.

| config | ret | maxDD | Sharpe | ret/DD |
|---|---|---|---|---|
| **flat_98 (LIVE)** | **+61.2%** | **17.5%** | **0.76** | **3.51** |
| voltgt_40_cap98 | +54.0% | 17.3% | 0.70 | 3.12 |
| voltgt_30_cap98 | +44.1% | 16.3% | 0.62 | 2.70 |
| voltgt_10_cap98 | +11.5% | 14.8% | 0.32 | 0.78 |
| stoprisk_12pct_cap98 | +50.7% | 17.5% | 0.67 | 2.90 |
| stoprisk_2pct_cap98 | +10.5% | 7.8% | 0.49 | 1.34 |
| voltgt_20_cap200 | +20.1% | 27.9% | 0.36 | 0.72 |

Flat wins on every axis, and every config degrades **monotonically** the further it moves from
flat. That is the signature of a rule that is pure drag, not one that needs tuning. Two
mechanisms were tested — inverse realised vol, and turtle stop-distance risk — and both failed
the same way, which makes it a property of the strategy rather than of one formula.

The published Sharpe improvements are real; they just do not transfer here, for three reasons
documented in that script: the evidence is for continuously-held multi-asset portfolios where
sizing down means sizing up elsewhere (here it means idle cash); the 10-day-low trailing exit
is already volatility-adaptive, so vol gets charged twice; and breakouts occur in high-vol
names by definition, so the rule shrinks precisely the trades the strategy exists to take.

**Action: none. swing1000 stays at flat 98%.**

## Recommendation 2: barbell the moonshot — REFRAMED, not applied as stated

The finding behind it holds: every survivable fast fortune on record ran the convex sleeve at
2–3% of capital, and 50% is why the moonshot is down 49%. But the stated fix — cut
`BUDGET_FRACTION` from 0.50 to ~0.03 — is arithmetically impossible.

One option contract is indivisible. Live near-the-money 0DTE asks inside the bot's 0.6% OTM
band, sampled 16:00 UTC Aug 25:

| underlying | cheapest | typical | far side |
|---|---|---|---|
| SPY | $47 | $96 | $166 |
| QQQ | $50 | $91 | $151 |
| IWM | $14 | $21 | $54 |

At the current $50.65 ledger, 2–3% is **$1.01–$1.52**. Nothing trades there. Setting the
fraction that low would not shrink the bet — it would skip every day forever, a silent shutdown
dressed up as a risk control.

**The barbell is a portfolio ratio, not a per-bet ratio.** At that level it is nearly right
already: the moonshot's entire $100 ledger *is* the convex sleeve. What is mis-set is the ratio
against the safe core.

## Recommendation 3: size the two bots as a deliberate pair — THIS IS THE REAL FIX

Current book, and what the benchmark implies:

| sleeve | now | share | Universa-style target |
|---|---|---|---|
| swing1000 (core, trend) | $1,000 | 91% | 85–90% |
| moonshot100 (convex sleeve) | $100 started, $50.65 now | 9% at start | 2–3% |
| **total** | **$1,100** | | |

The core is close to correct. The sleeve is roughly **3× too large relative to it**. Two ways to
close the gap, and only these two, because the per-bet fraction cannot move:

1. **Grow the core.** A $100 sleeve is 2–3% of a $3,300–$5,000 core. This is the direction that
   preserves the experiment: nothing about either bot changes, the ratio fixes itself.
2. **Shrink the sleeve's ledger, not its bet.** A $30–35 ledger against the current $1,000 core
   is ~3%. But $35 no longer buys a near-the-money contract on SPY or QQQ (see the table), so
   this collapses the sleeve to IWM-only and then to nothing. Not viable at this size.

**Recommendation: option 1, and it needs your decision, not mine** — it means committing more
paper capital to the core. Nothing has been changed in either bot pending that call.

Also worth stating plainly: swing1000 returned +69.6% against SPY's +75.0% over the backtest
window. It roughly *matches* buy-and-hold, it does not beat it. Calling it the "safe core" is
about its drawdown behaviour and its trend-following correlation profile, not about it being a
proven money-maker versus just owning the index.

## What is NOT on the table, permanently

Anything that sells unbounded downside for small premium. XIV returned over 1,000% from 2010 to
2017 and then lost 90% in a single session. The options bot's spreads stay defined-risk.
