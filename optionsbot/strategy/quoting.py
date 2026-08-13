"""Turning a chain snapshot into a price for a multi-leg combo.

Every price this module returns follows core/models.py's cash-flow-to-trader convention:

    positive = the trader PAYS this many dollars per share (a debit)
    negative = the trader RECEIVES this many dollars per share (a credit)

so a put credit spread priced here comes back negative, and feeding that straight into
`OrderIntent.limit_price_per_share` or `Position.unrealized_pnl` is correct with no sign
juggling at the call site. The one rule to remember: a leg's contribution is `+price` when the
trader buys it (`Action.is_buy`) and `-price` when the trader sells it.

Prices are `None`, never fabricated, when the underlying quotes can't support them -- the
Polygon free tier has no historical bid/ask, so `mid` is frequently unavailable and callers
fall back to `last`. Modelling what a *fill* would cost on top of these reference prices is
deliberately not this module's job; that lives in backtest/slippage.py, where the assumption is
explicit and configurable.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Optional, Sequence

from optionsbot.core.enums import Right
from optionsbot.core.models import Chain, Leg, OptionContract, OptionQuote

# Quotes are indexed by economic identity rather than by the OptionContract object itself:
# a contract rebuilt from a later day's chain (or round-tripped through the Parquet cache) is a
# different object, and may differ in cosmetic fields like occ_symbol, but is the same contract.
ContractKey = tuple[str, date, Decimal, Right]


def contract_key(contract: OptionContract) -> ContractKey:
    return (contract.underlying, contract.expiration, contract.strike, contract.right)


def quote_index(chain: Chain) -> dict[ContractKey, OptionQuote]:
    """Index a chain for O(1) lookup by contract identity."""
    return {contract_key(q.contract): q for q in chain.quotes}


def reference_price(quote: OptionQuote) -> Optional[Decimal]:
    """The best available single price for one contract: the bid/ask mid when both sides are
    quoted, otherwise the last trade. None when neither is available."""
    mid = quote.mid
    return mid if mid is not None else quote.last


def combo_price(legs: Sequence[Leg], price_for: Callable[[OptionContract], Optional[Decimal]]) -> Optional[Decimal]:
    """Signed per-share price of a multi-leg combo, given a per-contract pricing function.

    Returns None if *any* leg cannot be priced -- a partially-priced spread is not a price, and
    silently dropping a leg would understate risk.
    """
    total = Decimal("0")
    for leg in legs:
        price = price_for(leg.contract)
        if price is None:
            return None
        total += (price if leg.action.is_buy else -price) * leg.ratio
    return total


def combo_reference_price(legs: Sequence[Leg], quotes: Mapping[ContractKey, OptionQuote]) -> Optional[Decimal]:
    """Signed per-share mid/last price of a combo against an indexed chain."""

    def price_for(contract: OptionContract) -> Optional[Decimal]:
        quote = quotes.get(contract_key(contract))
        return None if quote is None else reference_price(quote)

    return combo_price(legs, price_for)


def intrinsic_value(contract: OptionContract, underlying_price: Decimal) -> Decimal:
    """Per-share intrinsic value at expiration (cash settlement value of the contract itself)."""
    if contract.right == Right.CALL:
        return max(Decimal("0"), underlying_price - contract.strike)
    return max(Decimal("0"), contract.strike - underlying_price)


def combo_intrinsic_price(legs: Sequence[Leg], underlying_price: Decimal) -> Decimal:
    """Signed per-share settlement price of a combo at expiration.

    Used to settle positions the backtest holds to expiry: there is no market to close into, so
    the position resolves at intrinsic value. Never None -- intrinsic value is always defined.
    """
    price = combo_price(legs, lambda contract: intrinsic_value(contract, underlying_price))
    assert price is not None  # intrinsic_value never returns None, so combo_price can't either
    return price


def atm_implied_volatility(chain: Chain, *, min_dte: int = 20) -> Optional[Decimal]:
    """The at-the-money implied vol of the nearest expiration at least `min_dte` out.

    This is the series that feeds IV Rank (see data/indicators.iv_rank), so it deliberately
    skips the very front expirations, whose IV is dominated by event/gamma effects rather than
    the general volatility level the IVR filter is trying to measure.
    """
    eligible = [exp for exp in chain.expirations() if (exp - chain.as_of).days >= min_dte]
    if not eligible:
        return None
    expiration = min(eligible)
    candidates = [
        q for q in chain.filter(expiration=expiration) if q.implied_volatility is not None
    ]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda q: abs(q.contract.strike - chain.underlying_price))
    return nearest.implied_volatility


def select_expiration(chain: Chain, *, dte_min: int, dte_max: int) -> Optional[date]:
    """The expiration inside [dte_min, dte_max] closest to the middle of that window.

    Picking the midpoint rather than the first match keeps the strategy's realised DTE stable
    week to week, which matters because the backtest and the live bot must make the same choice
    from the same chain for their results to be comparable.
    """
    target = (dte_min + dte_max) / 2
    eligible = [exp for exp in chain.expirations() if dte_min <= (exp - chain.as_of).days <= dte_max]
    if not eligible:
        return None
    return min(eligible, key=lambda exp: (abs((exp - chain.as_of).days - target), exp))


def find_strike(
    quotes: Iterable[OptionQuote], *, expiration: date, right: Right, strike: Decimal
) -> Optional[OptionQuote]:
    """The quote for one exact strike, or None if that strike isn't listed."""
    for quote in quotes:
        contract = quote.contract
        if contract.expiration == expiration and contract.right == right and contract.strike == strike:
            return quote
    return None
