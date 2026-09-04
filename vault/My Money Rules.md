---
tags:
  - risk
---

# My Money Rules

These are read straight out of the bot's settings file, so this note always matches what the bot
actually enforces. The bot cannot override any of them — it can only ever refuse a trade or make
it smaller.

## Per trade

| Rule | Setting | What it means |
|---|---|---|
| [[The 1% Rule]] | **1.0%** | The most one trade can lose. On $25,000 that's $250. |
| Defined risk only | always on | Every trade must have a known worst case before it's placed. |
| One expiry per ticker | on | Two trades on the same stock must use different dates, or they're one bet twice. |

## Across the whole account

| Rule | Setting | What it means |
|---|---|---|
| [[Portfolio Heat]] | **6%** | Total at risk across all open trades. Allows 6 at once. |
| Turbulent markets | 4% | Tightens automatically when markets get volatile. |
| Same-bet limit | 4 | SPY, QQQ and IWM move together, so they share one budget. |

## When it stops trading

| Trigger | Limit |
|---|---|
| Lost in one day | 3% — stops for the day |
| Lost in one week | 6% — stops, needs your review |
| Losses in a row | 5 — stops, needs your review |
| [[Drawdown]] from peak | 15% — full stop |
| Broker connection lost | 60s — alerts you |
| Stale price data | 5 min — no new trades |

## When it closes a trade

| Rule | Setting |
|---|---|
| [[Profit Target]] | Close at 50% of the credit collected |
| Stop loss | Close at 2.0x the credit as a loss |
| [[Time Stop]] | Close at 21 [[DTE]] no matter what |

> [!warning] These are not tunable by the bot
> The bot may adjust *which trades it looks for*. It may never adjust the rules on this page.
> That separation is deliberate: a system allowed to loosen its own risk limits when losing will
> eventually loosen them all the way.
