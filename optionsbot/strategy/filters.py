"""Composable entry gates.

Each filter is a pure function returning `None` when the gate passes, or a short human-readable
reason string when it doesn't -- the same shape as risk/limits.py, so filter rejections and risk
vetoes land in the journal looking alike and can be counted together.

The distinction from risk/: these are *strategy opinions* about whether a setup is worth taking,
and the Phase 7 learning loop is allowed to propose changes to their thresholds (which live in
strategies.yaml). Nothing in this file protects the account -- that is risk/'s job exclusively,
and it runs afterwards regardless of what these say.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from optionsbot.config.schema import StrategyParams, UniverseConfig
from optionsbot.core.models import OptionQuote
from optionsbot.data.indicators import sma


def check_iv_rank(iv_rank: Optional[Decimal], params: StrategyParams) -> Optional[str]:
    """Premium selling needs premium. Below the IVR floor, the credit doesn't pay for the risk.

    An unknown IV Rank rejects rather than passes: early in a backtest (or after a data gap)
    there isn't enough IV history to compute one, and trading blind through that window would
    manufacture trades the live bot would never take.
    """
    if params.min_iv_rank <= 0:
        return None
    if iv_rank is None:
        return "iv_rank_unknown: not enough IV history yet to compute IV Rank"
    if iv_rank < params.min_iv_rank:
        return f"iv_rank_too_low: {iv_rank:.1f} < {params.min_iv_rank} floor"
    return None


def check_earnings_blackout(
    as_of: date, earnings_dates: Sequence[date], blackout_days: int
) -> Optional[str]:
    """No new positions within `blackout_days` of an earnings release.

    Only forward-looking dates matter: an earnings event that already happened is priced in, and
    counting it would keep the bot out of the market for a week after every report for no
    reason. Broad-market ETFs (SPY, XSP) have no earnings, so this passes trivially for the
    default universe -- it exists for the moment single names are added.
    """
    for earnings in earnings_dates:
        days_away = (earnings - as_of).days
        if 0 <= days_away <= blackout_days:
            return f"earnings_blackout: {earnings.isoformat()} is {days_away}d away"
    return None


def check_liquidity(quote: OptionQuote, universe: UniverseConfig) -> Optional[str]:
    """Open-interest floor and maximum bid-ask width for a single contract.

    An illiquid contract is a trap in both directions: the entry fill is bad, and the exit fill
    (which you don't get to skip) is worse, precisely when you most need out. The width check is
    skipped when the provider gives no bid/ask -- a missing quote is not evidence of a tight
    market, and backtest/slippage.py already models the unknown width explicitly instead.
    """
    if quote.open_interest < universe.min_option_open_interest:
        return (
            f"illiquid_open_interest: {quote.contract} has OI {quote.open_interest} "
            f"< {universe.min_option_open_interest}"
        )
    spread_pct = quote.spread_pct_of_mid
    if spread_pct is not None and spread_pct > universe.max_bid_ask_spread_pct_of_mid:
        return (
            f"illiquid_spread: {quote.contract} bid-ask is {spread_pct:.1%} of mid "
            f"> {universe.max_bid_ask_spread_pct_of_mid:.1%} cap"
        )
    return None


def check_legs_liquidity(quotes: Sequence[OptionQuote], universe: UniverseConfig) -> Optional[str]:
    """Every leg must clear the liquidity floors; the first failure is reported."""
    for quote in quotes:
        reason = check_liquidity(quote, universe)
        if reason:
            return reason
    return None


def check_not_downtrend(closes: Sequence[float], *, period: int = 200) -> Optional[str]:
    """Don't sell puts into a market whose longer-term trend has broken.

    Selling put spreads is a bullish-to-neutral bet, and its losses cluster in sustained
    downtrends. The trap is that those are also the periods of high IV, so a *short-term* trend
    gate (say a 50-day average) is close to the exact inverse of the IV Rank gate: the days IVR
    likes are the days a 50-day gate rejects, and together they can veto every day in a sample.
    That is not conservatism, it is a strategy that never trades.

    A long-term average instead separates the two signals. A pullback inside an ongoing uptrend
    -- rich premium, trend intact -- passes both gates, which is the setup this strategy is
    built for. A market below its 200-day average is a genuinely different regime, and standing
    down there is the point.

    Passes when there isn't enough history for the average yet: this filter's job is to veto a
    known-bad condition, not to block trading until it has an opinion (the IVR gate already
    enforces its own warmup).
    """
    if len(closes) < period:
        return None
    average = sma(closes, period)[-1]
    if average is None:  # unreachable given the length check; belt-and-braces for callers
        return None
    if closes[-1] < average:
        return f"downtrend: last close {closes[-1]:.2f} below its {period}-day average {average:.2f}"
    return None
