---
tags:
  - glossary
---

# Put Credit Spread

> [!info] In one line
> The bot's main trade: sell a put, buy a cheaper one as insurance.

The trade this bot does almost exclusively.

1. **Sell** a put below the current price — collect money.
2. **Buy** another put a bit further below — pay a little back as insurance.

You keep the difference. You win if the stock stays above your sold strike, which it usually does, because you deliberately picked one the market thinks has about a 70% chance of never being reached.

**Real example.** SPY at $558. Sell the 537 put, buy the 534 put, collect $61.50. SPY has to fall 3.8% before you lose anything, and the very worst case is $238.50.

## Related

- [[Put]]
- [[Spread]]
- [[Delta]]
- [[Width]]
- [[Max Loss]]

---
[[Glossary|← Back to glossary]]
