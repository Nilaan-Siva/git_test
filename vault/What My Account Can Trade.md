---
tags:
  - risk
  - capital
---

# What My Account Can Trade

The most important number in this whole project isn't a strategy setting — it's how much money
is in the account. Everything follows from it.

## The uncomfortable arithmetic

The narrowest possible spread is 1 point wide, which risks about **$78 per contract**. Under
[[The 1% Rule]], affording that means the account must be at least **$7,800**.

There is no configuration that gets around this. A cheaper stock doesn't help — risk comes from
the [[Width]] of the spread, not the price of the stock. A 1-point spread on a $30 ETF still
risks $78.

> [!danger] Narrow spreads are not the small-account option
> It feels like risking less per trade should suit a small account. It's backwards.
> [[Commission|Commissions]] are a flat $2.60 round trip, so a 1-wide spread hands ~12% of every
> winner to the broker while a 3-wide hands over 4%. Trading smaller makes fees proportionally
> worse, not better.

## Account sizes, run through the real numbers

```
=== What $100 can trade ===
Risk per trade: 1.0% = $1.00
Positions at once (before the 6% total-risk cap): 6

 Width    Credit   At risk  Contracts    Fees  Verdict
    1pt    22.00     78.00          0   11.8%  cannot size a single contract
    2pt    44.00    156.00          0    5.9%  cannot size a single contract
    3pt    66.00    234.00          0    3.9%  cannot size a single contract
    5pt   110.00    390.00          0    2.4%  cannot size a single contract
   10pt   220.00    780.00          0    1.2%  cannot size a single contract

VERDICT: this account cannot trade defined-risk spreads at all.
The narrowest spread available risks $78.00 per contract, and 1.0% of $100 is only $1.00.
You would need about $7,800 for one contract of the narrowest spread -- and that width hands ~12% of every winner to commissions, so it is a floor, not a target.

The bot will refuse every trade at this size. That is the correct behaviour, not a bug.
```

```
=== What $1,000 can trade ===
Risk per trade: 1.0% = $10.00
Positions at once (before the 6% total-risk cap): 6

 Width    Credit   At risk  Contracts    Fees  Verdict
    1pt    22.00     78.00          0   11.8%  cannot size a single contract
    2pt    44.00    156.00          0    5.9%  cannot size a single contract
    3pt    66.00    234.00          0    3.9%  cannot size a single contract
    5pt   110.00    390.00          0    2.4%  cannot size a single contract
   10pt   220.00    780.00          0    1.2%  cannot size a single contract

VERDICT: this account cannot trade defined-risk spreads at all.
The narrowest spread available risks $78.00 per contract, and 1.0% of $1,000 is only $10.00.
You would need about $7,800 for one contract of the narrowest spread -- and that width hands ~12% of every winner to commissions, so it is a floor, not a target.

The bot will refuse every trade at this size. That is the correct behaviour, not a bug.
```

```
=== What $7,800 can trade ===
Risk per trade: 1.0% = $78.00
Positions at once (before the 6% total-risk cap): 6

 Width    Credit   At risk  Contracts    Fees  Verdict
    1pt    22.00     78.00          1   11.8%  fees eat most of the edge
    2pt    44.00    156.00          0    5.9%  cannot size a single contract
    3pt    66.00    234.00          0    3.9%  cannot size a single contract
    5pt   110.00    390.00          0    2.4%  cannot size a single contract
   10pt   220.00    780.00          0    1.2%  cannot size a single contract

VERDICT: tradable. Best width is 1pt -- $78.00 at risk, $22.00 credit, 11.8% to fees.
Roughly 100 trades a year with a full 12-ticker universe (~1.9 per week).
Fees are heavy at this size. Every extra dollar of capital widens the spread you can afford and shrinks that percentage.
```

```
=== What $25,000 can trade ===
Risk per trade: 1.0% = $250.00
Positions at once (before the 6% total-risk cap): 6

 Width    Credit   At risk  Contracts    Fees  Verdict
    1pt    22.00     78.00          3   11.8%  fees eat most of the edge
    2pt    44.00    156.00          1    5.9%  workable but fee-heavy
    3pt    66.00    234.00          1    3.9%  healthy
    5pt   110.00    390.00          0    2.4%  cannot size a single contract
   10pt   220.00    780.00          0    1.2%  cannot size a single contract

VERDICT: tradable. Best width is 3pt -- $234.00 at risk, $66.00 credit, 3.9% to fees.
Roughly 100 trades a year with a full 12-ticker universe (~1.9 per week).
```

```
=== What $50,000 can trade ===
Risk per trade: 1.0% = $500.00
Positions at once (before the 6% total-risk cap): 6

 Width    Credit   At risk  Contracts    Fees  Verdict
    1pt    22.00     78.00          6   11.8%  fees eat most of the edge
    2pt    44.00    156.00          3    5.9%  workable but fee-heavy
    3pt    66.00    234.00          2    3.9%  healthy
    5pt   110.00    390.00          1    2.4%  healthy
   10pt   220.00    780.00          0    1.2%  cannot size a single contract

VERDICT: tradable. Best width is 5pt -- $390.00 at risk, $110.00 credit, 2.4% to fees.
Roughly 100 trades a year with a full 12-ticker universe (~1.9 per week).
```

## What this means in practice

| Account | Reality |
|---|---|
| Under $7,800 | The bot places zero trades. Correct behaviour, not a bug. |
| $7,800 – $15,000 | Technically trades, but fees take ~12% of every winner. |
| $25,000 | Works properly. 3-wide spreads, ~4% to fees. |
| $50,000+ | Comfortable, and allows near-daily trading. |

If the real account will be small for a while, the sensible path is to keep
[[Paper Trading|paper trading]] — it's free, runs indefinitely, and proves the strategy while
you save.
