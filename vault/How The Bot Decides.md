---
tags:
  - strategy
---

# How The Bot Decides

Every morning the bot downloads fresh option prices for each ticker and walks through these
checks **in order**. The first failure ends the day for that ticker — it never reaches the later
checks.

## The checks

### 1. Is there enough history?
[[IV Rank]] compares today's volatility to the past year. Without a year of history there's
nothing to compare against, and the bot won't guess.

### 2. Are options expensive enough?
**Needs IV Rank above 30.** This is the big one — it rejected 91 of 164 days in
the last test, more than everything else combined.

Selling cheap options is a losing game. This check is the strategy's actual edge, which is why
it stays strict even though loosening it would produce far more trades.

### 3. Is the market trending up?
Selling puts is a mildly bullish bet. If the ticker is below its 200-day average, the bot stands
down.

> [!tip] Why 200 days and not 50
> Volatility rises when prices fall. So a *short-term* trend filter rejects exactly the
> high-volatility days that check 2 likes. Set to 50 days, the two checks cancelled out and the
> bot refused to trade for six straight months. A long-term filter lets it sell into a dip
> within a rising market — which is the setup it actually wants.

### 4. Is there an expiry in range?
**Needs 30–50 [[DTE]].** Close enough that time decay is
working, far enough that [[Gamma|things aren't violent]].

### 5. Is there a strike at the right distance?
**Needs [[Delta]] near 0.3** — roughly a 70% chance of expiring worthless.

### 6. Is the payment worth it?
**Needs at least 15% of the [[Width]] as [[Credit]].** Too
thin and [[Commission|fees]] eat the trade.

### 7. Does the risk manager allow it?
The final veto. Works out the size under [[The 1% Rule]], checks [[Portfolio Heat]], loss
limits, and whether this expiry is already held. See [[My Money Rules]].

## If all seven pass

It sells the spread, and from then on watches daily for one of three exits: the
[[Profit Target]], the stop loss, or the [[Time Stop]].

## Currently trading

SPY, QQQ, IWM, DIA, XLK, TLT, GLD, SLV, XLE, USO, XLF, EEM

[[Iron Condor]] is available but switched off until the simpler trade proves itself.
