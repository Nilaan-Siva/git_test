---
tags:
  - overview
---

# Start Here

This vault explains an options trading bot in plain language. It is rebuilt from the bot's
actual settings every time, so it can't drift out of date.

## What the bot does, in four sentences

It sells [[Put Credit Spread|put credit spreads]] on large, heavily-traded ETFs. Each trade is a
bet that the market won't fall more than about 3% in the next few weeks — and it gets paid up
front for taking that bet. Every trade has a known worst case, capped at **1% of the account**.
Most days it decides not to trade at all.

## The one thing to understand

**The bot's main job is saying no.** In the last test it examined 164 trading days and traded on
2 of them. That isn't the bot being broken or slow — selling options only pays when they're
expensive, and most days they aren't. See [[How The Bot Decides]].

## Where to go next

| Note | What's in it |
|---|---|
| [[How The Bot Decides]] | The checks it runs every morning, in order |
| [[My Money Rules]] | The limits protecting the account |
| [[What My Account Can Trade]] | What different account sizes can actually do |
| [[Trade Journal]] | Every trade it has made, explained |
| [[Latest Results]] | How the most recent test went |
| [[Glossary]] | Every term, in plain English |
| [[Learning Log]] | What changed over time, and why |

## Current setup

- **Trading**: SPY, QQQ, IWM, DIA, XLK, TLT, GLD, SLV, XLE, USO, XLF, EEM
- **Risk per trade**: 1.0% of the account
- **Most at risk at once**: 6%
- **Status**: still in testing. No real money is connected, and none will be until it proves
  itself on [[Paper Trading|paper]].
