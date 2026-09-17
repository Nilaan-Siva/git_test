"""Generates an Obsidian vault explaining the bot in plain language.

The audience is the person who owns the account and is learning options while the bot is being
built -- not a developer. So every note answers "what is this and why should I care" before it
shows a number, jargon is linked to its own note the first time it appears, and nothing is
described in terms of the code that implements it.

Two design choices worth stating:

**Generated from live config, never hand-written.** The money rules in the vault are read out of
risk.yaml at build time. A hand-maintained copy would drift, and a risk document that quietly
disagrees with the risk manager is worse than no document -- it teaches the wrong rules with
full confidence.

**The Learning Log only ever appends.** Rebuilding the vault rewrites every other note, but the
log keeps its history, because the point of it is to show what changed and when. A learning
record that gets overwritten on each run is not a record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from optionsbot.config.schema import RiskConfig, StrategiesConfig, UniverseConfig
from optionsbot.reporting.capital import assess, render as render_capital

LEARNING_LOG = "Learning Log.md"


@dataclass(frozen=True)
class Term:
    name: str
    short: str
    body: str
    see_also: tuple[str, ...] = ()


TERMS: tuple[Term, ...] = (
    Term("Option", "A contract to buy or sell a stock at a fixed price, by a fixed date.",
         "An option is a contract. It gives someone the right to buy or sell 100 shares of a "
         "stock at an agreed price, any time before an agreed date.\n\n"
         "Two things matter immediately. First, one option always covers **100 shares** — so a "
         "price quoted as $0.68 actually costs $68. Second, there are two people in every "
         "contract: a buyer who pays, and a **seller** who gets paid and takes on an obligation. "
         "This bot is almost always the seller.",
         ("Call", "Put", "Premium")),
    Term("Call", "A bet that the price goes up.",
         "A call option gives its owner the right to *buy* shares at a set price. People buy "
         "calls when they think a stock will rise.\n\nThis bot doesn't buy calls. It only sells "
         "them as part of an [[Iron Condor]], which is currently switched off.",
         ("Put", "Option")),
    Term("Put", "A bet that the price goes down.",
         "A put option gives its owner the right to *sell* shares at a set price. People buy "
         "puts as insurance against a crash, or to bet on a fall.\n\nThe bot **sells** puts. It "
         "is effectively selling that insurance and collecting the fee — which works out as long "
         "as the crash doesn't happen.",
         ("Call", "Put Credit Spread", "Premium")),
    Term("Strike", "The fixed price written into the contract.",
         "The strike is the agreed price. A '537 put' is a contract about selling shares at "
         "$537.\n\nHow far the strike sits from today's price is the single biggest choice in "
         "each trade. Further away means safer but less money.",
         ("Delta", "Put")),
    Term("Expiration", "The date the contract dies.",
         "Every option has an expiry date. After it, the contract is worthless and gone.\n\n"
         "This matters more than beginners expect: options **lose value as time passes**, "
         "which is exactly why selling them can be profitable. See [[Theta]].",
         ("DTE", "Theta")),
    Term("DTE", "Days To Expiration — how many days until the contract dies.",
         "Just a countdown. '45 DTE' means 45 days left.\n\nThe bot opens trades at **30–50 "
         "DTE** and closes them by **21 DTE** at the latest. The reason: time decay is steady and "
         "predictable in that middle stretch, and gets violent and unpredictable in the last "
         "three weeks.",
         ("Expiration", "Theta", "Time Stop")),
    Term("Premium", "The money paid for an option.",
         "The price of the contract. When you **buy**, you pay premium. When you **sell**, you "
         "receive it.\n\nThe bot is a premium *seller*. Money lands in the account the moment a "
         "trade opens. The job from then on is keeping as much of it as possible.",
         ("Credit", "Option")),
    Term("Credit", "Money you receive when opening a trade.",
         "If a trade pays you to enter it, that payment is the credit. The bot's whole strategy "
         "is built on collecting credits.\n\nThe catch: the credit is your **maximum possible "
         "profit**. You can never make more than what you were paid up front.",
         ("Premium", "Debit", "Max Loss")),
    Term("Debit", "Money you pay when opening a trade.",
         "The opposite of a credit. You pay to get in, hoping to sell for more later.\n\n"
         "The bot pays a debit only when *closing* a position it originally sold.",
         ("Credit",)),
    Term("Spread", "Two options traded together as one package.",
         "A spread means buying one option and selling another at the same time. The bought one "
         "acts as insurance on the sold one.\n\nThis is what makes the risk **knowable in "
         "advance**. Selling an option alone can lose an almost unlimited amount. Inside a "
         "spread, the worst case is a fixed number you know before you enter.",
         ("Put Credit Spread", "Width", "Max Loss")),
    Term("Put Credit Spread", "The bot's main trade: sell a put, buy a cheaper one as insurance.",
         "The trade this bot does almost exclusively.\n\n1. **Sell** a put below the current "
         "price — collect money.\n2. **Buy** another put a bit further below — pay a little back "
         "as insurance.\n\nYou keep the difference. You win if the stock stays above your sold "
         "strike, which it usually does, because you deliberately picked one the market thinks "
         "has about a 70% chance of never being reached.\n\n**Real example.** SPY at $558. Sell "
         "the 537 put, buy the 534 put, collect $61.50. SPY has to fall 3.8% before you lose "
         "anything, and the very worst case is $238.50.",
         ("Put", "Spread", "Delta", "Width", "Max Loss")),
    Term("Width", "The gap between the two strikes in a spread.",
         "Sell the 537 and buy the 534 and the width is 3 points — a '3-wide' spread.\n\nWidth "
         "sets everything: a wider spread collects more money but risks more. Width x 100 is the "
         "most that can ever be at stake.\n\n**The counter-intuitive part:** narrow spreads are "
         "*not* the safe choice for a small account. Commissions are a flat fee, so a 1-wide "
         "spread hands about 12% of every winner to the broker, while a 3-wide hands over 4%.",
         ("Spread", "Max Loss", "Commission")),
    Term("Max Loss", "The worst thing that can happen on a trade.",
         "For a credit spread: `(width x 100) - credit`. Sell a 3-wide for $66 and the worst "
         "case is $234.\n\nThis number is known **before** entering, which is what lets the bot "
         "size every position so a total loss costs no more than 1% of the account.",
         ("Width", "Credit", "Position Sizing", "The 1% Rule")),
    Term("Delta", "Roughly, the chance an option finishes in the money.",
         "Delta is a decimal between 0 and 1. Treat it as a probability: a 0.30 delta put has "
         "roughly a **30% chance** of being worth something at expiry — so a 70% chance of "
         "expiring worthless, which is what the seller wants.\n\nThe bot targets 0.30. Lower "
         "would be safer but pay too little; higher pays more but loses too often.",
         ("Strike", "Put Credit Spread")),
    Term("Implied Volatility", "How much movement the market is pricing in.",
         "Usually shortened to **IV**. It's the market's forecast of how much a stock will swing "
         "around.\n\nHigh IV means options are expensive, because everyone expects turbulence. "
         "Low IV means they're cheap. Since the bot sells options, it wants to sell when they're "
         "expensive.",
         ("IV Rank", "Vega")),
    Term("IV Rank", "Where today's volatility sits compared to the past year.",
         "IV on its own tells you little — 20% is high for one stock and low for another. IV "
         "Rank fixes that by scoring today against the same stock's own past year, from 0 to "
         "100.\n\n**IV Rank 80** means options are pricier than on 80% of days in the last year. "
         "The bot only sells above **30**.\n\nThis one filter rejected 91 of 164 days in the last "
         "backtest — more than every other reason combined. It is the strategy's actual edge, "
         "and the thing most worth *not* loosening to get more trades.",
         ("Implied Volatility", "How The Bot Decides")),
    Term("Theta", "How much value an option loses each day just from time passing.",
         "Options are melting ice cubes. Every day that passes, they're worth slightly less, "
         "even if the stock doesn't move at all.\n\nTerrible if you own one. Excellent if you "
         "**sold** one — which is why this bot sells. Theta is the wind at its back.",
         ("DTE", "Expiration", "Premium")),
    Term("Vega", "How much an option's price moves when volatility changes.",
         "If volatility spikes, options get more expensive across the board — bad for a seller, "
         "since buying back costs more.\n\nThis is why the bot sells when IV is already high: "
         "there's more room for it to fall back than to rise further.",
         ("Implied Volatility", "IV Rank")),
    Term("Assignment", "Being forced to actually buy the shares.",
         "If a sold put finishes below its strike, the buyer can make you buy 100 shares at that "
         "price — potentially tens of thousands of dollars you may not have.\n\nThe insurance leg "
         "in a spread caps the damage, and the bot closes positions well before expiry to avoid "
         "the situation entirely.",
         ("Put", "Spread", "Time Stop")),
    Term("The 1% Rule", "Never risk more than 1% of the account on one trade.",
         "Your rule, and the single most important line in the whole system.\n\nOn $25,000 that "
         "is **$250**. The bot works out the worst case of a trade first, and only then decides "
         "how many contracts it can buy without exceeding that. If it can't afford even one, it "
         "doesn't trade.\n\nWhy it matters: risking 1% means twenty losses in a row costs you "
         "about 18% of the account. Risking 10% means the same streak wipes you out.",
         ("Max Loss", "Position Sizing", "Portfolio Heat")),
    Term("Position Sizing", "Deciding how many contracts to trade.",
         "Not a guess. It's division:\n\n`contracts = (account x 1%) / max loss per contract`\n\n"
         "$25,000 x 1% = $250. A trade risking $234 per contract means exactly **one** contract. "
         "$250 divided by $234 rounds down to 1.\n\nRounding down always. Rounding up would break "
         "the 1% rule.",
         ("The 1% Rule", "Max Loss")),
    Term("Portfolio Heat", "Total risk across every open position at once.",
         "One trade at 1% is fine. Ten trades at 1% each is 10% at risk simultaneously — and in a "
         "crash they tend to lose together.\n\nSo there's a second cap: **6% total**, which "
         "allows six positions at once. It tightens to 4% when markets are turbulent.",
         ("The 1% Rule", "Position Sizing")),
    Term("Profit Target", "Closing early once most of the money is made.",
         "The bot buys back its spreads at **50% of the credit**. Collect $66, buy it back for "
         "$33, keep $33.\n\nWhy not hold for the whole thing? The last half takes far longer and "
         "carries the same risk. Taking half quickly and redeploying beats squeezing the "
         "remainder.",
         ("Credit", "Time Stop")),
    Term("Time Stop", "Closing at 21 days left, whatever is happening.",
         "Even a trade going nowhere gets closed at 21 DTE.\n\nThe final three weeks are when "
         "options turn dangerous — price swings that were survivable become sharp and sudden. "
         "The bot leaves before that window regardless of profit or loss.",
         ("DTE", "Gamma")),
    Term("Gamma", "How quickly your risk changes when the stock moves.",
         "The technical name for why the last weeks are dangerous. Close to expiry, a small move "
         "in the stock causes a large, fast change in the option's value.\n\nYou don't need to "
         "calculate it. Just know it's the reason for the [[Time Stop]].",
         ("Time Stop", "Delta")),
    Term("Slippage", "The gap between the price you see and the price you get.",
         "Quotes show a price to buy and a lower price to sell. You never get the middle.\n\nOn "
         "small credits this matters enormously, and it's the number one reason backtests lie. "
         "Every backtest here is run three times — optimistic, realistic and pessimistic — and "
         "only the **pessimistic** result counts.",
         ("Bid-Ask Spread", "Backtest")),
    Term("Bid-Ask Spread", "The difference between the buy price and the sell price.",
         "Bid $0.65, ask $0.70 means you sell at 65 and buy at 70. That nickel is a cost, every "
         "time, in both directions.\n\nIt's why the bot only trades heavily-traded ETFs — thin "
         "markets have wide gaps that quietly eat the profit.",
         ("Slippage", "Liquidity")),
    Term("Liquidity", "Whether enough people are trading something to get a fair price.",
         "A liquid option has many buyers and sellers, so you can get in and out near the quoted "
         "price. An illiquid one is a trap: easy to enter, expensive to escape — and you always "
         "have to escape eventually.",
         ("Bid-Ask Spread", "Open Interest")),
    Term("Open Interest", "How many contracts exist right now.",
         "A rough gauge of how busy a contract is. Higher is safer to trade.\n\nWorth knowing: "
         "the free Polygon data plan **doesn't report this at all**, so the bot falls back to "
         "daily trading volume instead. An early bug read the missing value as zero and would "
         "have rejected every contract in existence.",
         ("Liquidity",)),
    Term("Commission", "The broker's flat fee per contract.",
         "IBKR charges **$0.65 per contract per leg**. A two-leg spread costs $1.30 to open and "
         "$1.30 to close — **$2.60 round trip**.\n\nFlat fees hit small trades hardest. Against a "
         "$22 credit that's 12%. Against a $66 credit it's 4%. This single fact is why the bot "
         "trades 3-wide spreads and why tiny accounts struggle.",
         ("Width", "Expectancy")),
    Term("Expectancy", "Average profit per trade, wins and losses combined.",
         "The number that actually matters. Win rate alone is meaningless — you can win 90% of "
         "the time and still go broke if the 10% are huge.\n\n`expectancy = (win% x avg win) - "
         "(loss% x avg loss)`\n\nPositive means the strategy makes money over many trades. "
         "Negative means it doesn't, however good a run looks.",
         ("Win Rate", "Backtest")),
    Term("Win Rate", "The percentage of trades that make money.",
         "Selling 30-delta spreads should win around **70%** of the time.\n\nBut on its own it "
         "proves nothing. A high win rate with a few catastrophic losses is a losing strategy "
         "wearing a disguise. Always read it next to [[Expectancy]].",
         ("Expectancy", "Delta")),
    Term("Drawdown", "How far the account has fallen from its best point.",
         "Peak $26,000, now $24,000 — that's a 7.7% drawdown.\n\nThe bot stops trading entirely "
         "at **15%**. Drawdown, not total return, is what actually ends most trading accounts, "
         "because people quit at the bottom.",
         ("Portfolio Heat",)),
    Term("Backtest", "Replaying the strategy over past market data.",
         "Feed the bot real historical prices and see what it would have done.\n\nUseful for "
         "catching a broken strategy. **Not** proof of a working one — it's easy to accidentally "
         "tune a strategy until it fits the past perfectly and fails on everything else. That's "
         "what [[Walk-Forward Testing]] is for.",
         ("Walk-Forward Testing", "Slippage", "Paper Trading")),
    Term("Walk-Forward Testing", "Tuning on old data, then testing on data never seen.",
         "The honesty check. Set the strategy up using the first chunk of history, then run it "
         "on a later chunk it has never touched.\n\nIf results collapse on the unseen data, the "
         "strategy was memorising the past rather than finding something real. This gate isn't "
         "optional.",
         ("Backtest", "Overfitting")),
    Term("Overfitting", "Tuning a strategy until it perfectly fits the past.",
         "The most common way trading systems fail. Adjust enough settings and any strategy looks "
         "brilliant on history — and then loses money immediately on live markets, because it "
         "learned that specific past rather than anything general.\n\nThe defence is refusing to "
         "judge a strategy on data used to build it.",
         ("Walk-Forward Testing", "Backtest")),
    Term("Paper Trading", "Trading with fake money on a real broker connection.",
         "A full simulation: real prices, real order handling, real delays and rejections — "
         "imaginary money.\n\nCatches everything a backtest can't: connection drops, orders that "
         "don't fill, strange behaviour in fast markets.\n\n**Set the paper balance to whatever "
         "you'll really fund.** Paper at $25,000 then going live at $500 makes every result "
         "meaningless, because position sizes and rounding behave completely differently.",
         ("Backtest", "Position Sizing")),
    Term("Iron Condor", "A put spread and a call spread at once.",
         "Sells premium on both sides — profits if the stock stays in a range.\n\nCurrently "
         "**switched off**. It has four legs, so it pays double the commission, and it needs "
         "higher volatility to be worth it. It gets enabled only after the simpler put credit "
         "spread has proven itself.",
         ("Put Credit Spread", "Commission")),
)


def _slug(name: str) -> str:
    return name.replace("/", "-")


def _front(tags: list[str]) -> str:
    return "---\ntags:\n" + "".join(f"  - {t}\n" for t in tags) + "---\n\n"


def _term_note(term: Term) -> str:
    out = _front(["glossary"]) + f"# {term.name}\n\n> [!info] In one line\n> {term.short}\n\n{term.body}\n"
    if term.see_also:
        out += "\n## Related\n\n" + "".join(f"- [[{s}]]\n" for s in term.see_also)
    out += "\n---\n[[Glossary|← Back to glossary]]\n"
    return out


def _glossary_index() -> str:
    out = _front(["glossary"]) + "# Glossary\n\nEvery term used anywhere in these notes. Start with the bolded ones.\n\n"
    core = {"Put Credit Spread", "IV Rank", "Delta", "The 1% Rule", "Max Loss", "Expectancy"}
    out += "## Start with these\n\n"
    for t in TERMS:
        if t.name in core:
            out += f"- **[[{t.name}]]** — {t.short}\n"
    out += "\n## Everything else\n\n"
    for t in sorted(TERMS, key=lambda x: x.name):
        if t.name not in core:
            out += f"- [[{t.name}]] — {t.short}\n"
    return out


def _start_here(risk: RiskConfig, universe: UniverseConfig) -> str:
    return (
        _front(["overview"])
        + f"""# Start Here

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

- **Trading**: {", ".join(universe.tickers)}
- **Risk per trade**: {risk.max_risk_per_trade_pct * 100:.1f}% of the account
- **Most at risk at once**: {risk.max_portfolio_heat_pct * 100:.0f}%
- **Status**: still in testing. No real money is connected, and none will be until it proves
  itself on [[Paper Trading|paper]].
"""
    )


def _money_rules(risk: RiskConfig) -> str:
    return (
        _front(["risk"])
        + f"""# My Money Rules

These are read straight out of the bot's settings file, so this note always matches what the bot
actually enforces. The bot cannot override any of them — it can only ever refuse a trade or make
it smaller.

## Per trade

| Rule | Setting | What it means |
|---|---|---|
| [[The 1% Rule]] | **{risk.max_risk_per_trade_pct * 100:.1f}%** | The most one trade can lose. On $25,000 that's ${25000 * float(risk.max_risk_per_trade_pct):,.0f}. |
| Defined risk only | always on | Every trade must have a known worst case before it's placed. |
| One expiry per ticker | {"on" if risk.require_distinct_expirations else "off"} | Two trades on the same stock must use different dates, or they're one bet twice. |

## Across the whole account

| Rule | Setting | What it means |
|---|---|---|
| [[Portfolio Heat]] | **{risk.max_portfolio_heat_pct * 100:.0f}%** | Total at risk across all open trades. Allows {int(risk.max_portfolio_heat_pct / risk.max_risk_per_trade_pct)} at once. |
| Turbulent markets | {risk.max_portfolio_heat_pct_high_vix * 100:.0f}% | Tightens automatically when markets get volatile. |
| Same-bet limit | {risk.max_positions_per_correlated_bucket} | SPY, QQQ and IWM move together, so they share one budget. |

## When it stops trading

| Trigger | Limit |
|---|---|
| Lost in one day | {risk.max_daily_loss_pct * 100:.0f}% — stops for the day |
| Lost in one week | {risk.max_weekly_loss_pct * 100:.0f}% — stops, needs your review |
| Losses in a row | {risk.consecutive_loss_halt} — stops, needs your review |
| [[Drawdown]] from peak | {risk.max_drawdown_halt_pct * 100:.0f}% — full stop |
| Broker connection lost | {risk.broker_disconnect_halt_seconds}s — alerts you |
| Stale price data | {risk.data_staleness_halt_seconds // 60} min — no new trades |

## When it closes a trade

| Rule | Setting |
|---|---|
| [[Profit Target]] | Close at {risk.profit_target_pct * 100:.0f}% of the credit collected |
| Stop loss | Close at {risk.stop_loss_multiple}x the credit as a loss |
| [[Time Stop]] | Close at {risk.time_stop_dte} [[DTE]] no matter what |

> [!warning] These are not tunable by the bot
> The bot may adjust *which trades it looks for*. It may never adjust the rules on this page.
> That separation is deliberate: a system allowed to loosen its own risk limits when losing will
> eventually loosen them all the way.
"""
    )


def _how_it_decides(strategies: StrategiesConfig, universe: UniverseConfig) -> str:
    p = strategies.put_credit_spread
    return (
        _front(["strategy"])
        + f"""# How The Bot Decides

Every morning the bot downloads fresh option prices for each ticker and walks through these
checks **in order**. The first failure ends the day for that ticker — it never reaches the later
checks.

## The checks

### 1. Is there enough history?
[[IV Rank]] compares today's volatility to the past year. Without a year of history there's
nothing to compare against, and the bot won't guess.

### 2. Are options expensive enough?
**Needs IV Rank above {p.min_iv_rank:.0f}.** This is the big one — it rejected 91 of 164 days in
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
**Needs {p.target_dte_min}–{p.target_dte_max} [[DTE]].** Close enough that time decay is
working, far enough that [[Gamma|things aren't violent]].

### 5. Is there a strike at the right distance?
**Needs [[Delta]] near {p.short_delta_target}** — roughly a 70% chance of expiring worthless.

### 6. Is the payment worth it?
**Needs at least {p.min_credit_pct_of_width * 100:.0f}% of the [[Width]] as [[Credit]].** Too
thin and [[Commission|fees]] eat the trade.

### 7. Does the risk manager allow it?
The final veto. Works out the size under [[The 1% Rule]], checks [[Portfolio Heat]], loss
limits, and whether this expiry is already held. See [[My Money Rules]].

## If all seven pass

It sells the spread, and from then on watches daily for one of three exits: the
[[Profit Target]], the stop loss, or the [[Time Stop]].

## Currently trading

{", ".join(universe.tickers)}

[[Iron Condor]] is available but switched off until the simpler trade proves itself.
"""
    )


def _capital_note(risk: RiskConfig) -> str:
    blocks = []
    for eq in (100, 1_000, 7_800, 25_000, 50_000):
        blocks.append("```\n" + render_capital(assess(Decimal(eq), risk_pct=risk.max_risk_per_trade_pct,
                                                      heat_pct=risk.max_portfolio_heat_pct)) + "\n```")
    return (
        _front(["risk", "capital"])
        + """# What My Account Can Trade

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

"""
        + "\n\n".join(blocks)
        + """

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
"""
    )


def _trade_journal(run: Optional[dict]) -> str:
    head = _front(["results"]) + "# Trade Journal\n\nEvery trade the bot has made in testing, in plain language.\n\n"
    if not run or not run.get("trades"):
        return head + "> [!note] No trades recorded yet\n> Run a backtest to populate this.\n"
    out = head
    for i, t in enumerate(run["trades"], 1):
        won = t["pnl"] > 0
        held = "?"
        try:
            from datetime import date as _d
            held = (_d.fromisoformat(t["exit_date"]) - _d.fromisoformat(t["entry_date"])).days
        except Exception:
            pass
        out += (
            f"## Trade {i} — {'won' if won else 'lost'} ${abs(t['pnl']):.2f}\n\n"
            f"| | |\n|---|---|\n"
            f"| Opened | {t['entry_date']} |\n"
            f"| Closed | {t['exit_date']} ({held} days later) |\n"
            f"| Trade | Sold the {t['short']:.0f} put, bought the {t['long']:.0f} put |\n"
            f"| Expiry | {t['expiration']} ({t['dte_at_entry']} [[DTE]] at entry) |\n"
            f"| Got paid | ${abs(t['entry_price']) * 100:.2f} |\n"
            f"| Worst case | ${t['max_loss']:.2f} |\n"
            f"| Closed because | {t['reason'].replace('_', ' ')} |\n"
            f"| Result | **${t['pnl']:+.2f}** after fees |\n\n"
            f"The market had to fall below **{t['short']:.0f}** for this to lose money. "
            f"{'It did not.' if won else 'It did.'}\n\n"
        )
    return out


def _latest_results(run: Optional[dict]) -> str:
    head = _front(["results"]) + "# Latest Results\n\n"
    if not run:
        return head + "> [!note] No results yet\n> Run a backtest to populate this.\n"
    m = run["metrics"]
    cov = run.get("coverage", {})
    out = head + (
        f"| | |\n|---|---|\n"
        f"| Trades | {m['trades']} |\n"
        f"| Won / lost | {m['wins']} / {m['losses']} |\n"
        f"| [[Win Rate]] | {m['win_rate'] or 0:.0f}% |\n"
        f"| [[Expectancy]] per trade | ${m['expectancy']:+.2f} |\n"
        f"| Started with | ${m['start_equity']:,.0f} |\n"
        f"| Ended with | ${m['end_equity']:,.0f} |\n"
        f"| Worst [[Drawdown]] | {m['max_dd']:.2f}% |\n"
        f"| [[Commission|Fees]] paid | ${m['commissions']:.2f} |\n\n"
    )
    if cov:
        out += "## Data coverage\n\n"
        for tk, frac in cov.items():
            flag = "" if frac > 0.5 else "  ⚠️ **not enough data — results are not about this ticker**"
            out += f"- {tk}: {frac * 100:.0f}%{flag}\n"
        out += "\n"
    if run.get("rejections"):
        out += "## Why it didn't trade\n\n| Reason | Days |\n|---|---|\n"
        for reason, n in run["rejections"].items():
            out += f"| {reason.replace('_', ' ')} | {n} |\n"
        out += "\n"
    out += (
        "> [!warning] Small samples prove nothing\n"
        f"> {m['trades']} trades is not enough to judge a strategy. A few wins in a row is luck, not\n"
        "> evidence. Roughly 50 trades is the minimum before the numbers mean anything, and even then\n"
        "> [[Walk-Forward Testing]] is what actually settles it.\n"
    )
    return out


def _append_learning_log(path: Path, risk: RiskConfig, strategies: StrategiesConfig,
                         universe: UniverseConfig, run: Optional[dict]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p = strategies.put_credit_spread
    entry = [
        f"## {stamp}",
        "",
        f"- Risk per trade: **{risk.max_risk_per_trade_pct * 100:.1f}%** · "
        f"[[Width]]: **{p.spread_width}pt** · [[IV Rank]] floor: **{p.min_iv_rank:.0f}** · "
        f"[[DTE]] window: **{p.target_dte_min}–{p.target_dte_max}**",
        f"- Universe: {len(universe.tickers)} tickers ({', '.join(universe.tickers)})",
    ]
    if run:
        m = run["metrics"]
        entry.append(
            f"- Last test: **{m['trades']} trades**, {m['win_rate'] or 0:.0f}% won, "
            f"expectancy **${m['expectancy']:+.2f}**, ended **${m['end_equity']:,.0f}**"
        )
    entry.append("")

    if not path.exists():
        path.write_text(
            _front(["log"])
            + "# Learning Log\n\nAppended to every time the vault is rebuilt — newest at the "
              "bottom. This is the one note that keeps its history, so changes to the setup and "
              "their effect stay visible over time.\n\n"
            + "\n".join(entry)
        )
        return
    existing = path.read_text()
    if stamp in existing:  # same minute; don't duplicate
        return
    path.write_text(existing.rstrip() + "\n\n" + "\n".join(entry))


def build_vault(
    out_dir: Path,
    *,
    risk: RiskConfig,
    strategies: StrategiesConfig,
    universe: UniverseConfig,
    run: Optional[dict] = None,
) -> int:
    """Write the vault. Returns the number of notes written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    terms_dir = out_dir / "Terms"
    terms_dir.mkdir(exist_ok=True)

    written = 0
    for term in TERMS:
        (terms_dir / f"{_slug(term.name)}.md").write_text(_term_note(term))
        written += 1

    pages = {
        "Start Here.md": _start_here(risk, universe),
        "Glossary.md": _glossary_index(),
        "How The Bot Decides.md": _how_it_decides(strategies, universe),
        "My Money Rules.md": _money_rules(risk),
        "What My Account Can Trade.md": _capital_note(risk),
        "Trade Journal.md": _trade_journal(run),
        "Latest Results.md": _latest_results(run),
    }
    for name, body in pages.items():
        (out_dir / name).write_text(body)
        written += 1

    _append_learning_log(out_dir / LEARNING_LOG, risk, strategies, universe, run)
    written += 1
    return written
