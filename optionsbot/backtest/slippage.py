"""Fill modelling: what a combo actually costs to trade, versus what it's quoted at.

Mid-price fills are the single biggest reason backtests lie. The published retail-fill research
this model follows finds that orders travel roughly **75% of the bid-ask width** for a single
leg, tightening to about **53% for a four-leg spread** -- multi-leg orders fill closer to mid
because the market maker is pricing one net package and the order can be worked.

That maps to a clean rule. Crossing from mid to the far side of the market costs exactly half a
width per leg, so a fill fraction `f` (measured bid->ask) costs `(f - 0.5)` widths per leg
relative to mid:

    adverse_per_share = (fill_fraction - 0.5) * sum(bid_ask_width of each leg)

At `f = 1.0` that is half the summed widths, i.e. paying full ask on every buy leg and hitting
full bid on every sell leg -- the true worst case. At `f = 0.5` it is zero, i.e. perfect mid
fills. Because the adjustment is always non-negative and prices follow core/models.py's
cash-flow-to-trader convention (positive = trader pays), slippage always *increases* the price:
more debit paid, or less credit received, with no sign special-casing per strategy.

**A consequence worth knowing before reading any backtest that compares structures.** Because
the research fraction falls faster with leg count than the summed width rises, this model makes
a four-leg iron condor *cheaper* to fill than a two-leg vertical. That is what the research
measures -- a package quoted as one net market fills close to its mid -- but taken alone it
tilts strategy comparison toward condors. Two things keep that honest: a condor pays double the
commission, and the pessimistic preset charges every leg in full, so slippage there scales
strictly with leg count. Judge structures on the pessimistic run.

**The honest caveat, and the single biggest assumption in the backtest.** Polygon's free tier
carries no historical bid/ask, so most backtest quotes have no real width. Rather than fabricate
bid/ask in the data layer (see core/models.OptionQuote, which keeps missing fields None on
purpose), this model substitutes an assumed width when a real one is unavailable.

Getting the *shape* of that assumption right matters more than the number. Option bid-ask width
is far closer to absolute than proportional: it is driven by tick size and market-maker
competition, not by the contract's price. SPY options -- the most liquid in the world -- quote
about a penny wide on active strikes whether the contract costs $0.40 or $4.00. A naive
percentage-of-price model therefore charges a near-the-money contract twenty times too much
slippage, which on a 1-wide spread can exceed the entire credit and make any strategy look
unprofitable for a reason that is purely an artifact of the model.

So the assumed width is a percentage clamped between an absolute floor and an absolute cap, and
the presets are calibrated to liquid index options (SPY/XSP, the only things in universe.yaml).
For a less liquid underlying these numbers are too kind and must be widened. This whole
assumption is provisional: Phase 5 records real paper fills, and Phase 7's `reconcile.py`
replaces guesswork with measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from optionsbot.core.models import Leg, OptionQuote
from optionsbot.strategy.quoting import ContractKey, combo_reference_price, contract_key, reference_price

# Fill fraction (bid->ask) by leg count, from the retail options fill-quality research. Leg
# counts between/beyond these keys fall back to the nearest lower key (see `fill_fraction`).
RESEARCH_FILL_FRACTIONS: dict[int, Decimal] = {
    1: Decimal("0.75"),
    2: Decimal("0.65"),
    3: Decimal("0.59"),
    4: Decimal("0.53"),
}

MID_FILL_FRACTIONS: dict[int, Decimal] = {1: Decimal("0.50")}
CROSS_FILL_FRACTIONS: dict[int, Decimal] = {1: Decimal("1.00")}

IBKR_COMMISSION_PER_CONTRACT = Decimal("0.65")


@dataclass(frozen=True)
class SlippageModel:
    """How an order's fill price differs from its quoted reference price.

    Use the three constructors below rather than assembling one by hand: every backtest is run
    at least twice (optimistic and pessimistic) and a strategy is only trusted if it survives
    the pessimistic run.
    """

    fill_fractions: Mapping[int, Decimal] = field(default_factory=lambda: dict(RESEARCH_FILL_FRACTIONS))
    assumed_spread_pct_of_mid: Decimal = Decimal("0.03")
    min_leg_width: Decimal = Decimal("0.01")
    max_leg_width: Decimal = Decimal("0.05")
    commission_per_contract: Decimal = IBKR_COMMISSION_PER_CONTRACT
    label: str = "realistic"

    @classmethod
    def optimistic(cls) -> "SlippageModel":
        """Perfect mid fills and penny-wide markets. The upper bound on what's achievable --
        useful only as the top of the bracket, never as the number you plan around."""
        return cls(
            fill_fractions=dict(MID_FILL_FRACTIONS),
            assumed_spread_pct_of_mid=Decimal("0.01"),
            max_leg_width=Decimal("0.02"),
            label="optimistic",
        )

    @classmethod
    def realistic(cls) -> "SlippageModel":
        """The research-backed default: 75% of width single-leg, 53% four-leg, against markets
        one to five cents wide."""
        return cls(label="realistic")

    @classmethod
    def pessimistic(cls) -> "SlippageModel":
        """Always cross the full spread, and assume markets three times wider than SPY's really
        are. This is the run that decides whether a strategy is real: if expectancy is negative
        here, the edge was fill quality, not the strategy."""
        return cls(
            fill_fractions=dict(CROSS_FILL_FRACTIONS),
            assumed_spread_pct_of_mid=Decimal("0.08"),
            min_leg_width=Decimal("0.02"),
            max_leg_width=Decimal("0.15"),
            label="pessimistic",
        )

    def fill_fraction(self, leg_count: int) -> Decimal:
        """The bid->ask fill fraction for a combo of `leg_count` legs.

        Falls back to the nearest *lower* configured leg count, so a model configured only for
        single legs (the mid/cross presets) applies that fraction uniformly, and a five-leg
        structure inherits the four-leg number rather than silently reverting to the much
        harsher single-leg one.
        """
        if leg_count <= 0:
            raise ValueError("leg_count must be positive")
        candidates = [n for n in self.fill_fractions if n <= leg_count]
        if not candidates:
            return self.fill_fractions[min(self.fill_fractions)]
        return self.fill_fractions[max(candidates)]

    def leg_width(self, quote: OptionQuote) -> Optional[Decimal]:
        """One leg's bid-ask width: the real one when quoted, otherwise the modelled
        assumption, clamped to [min_leg_width, max_leg_width].

        The clamp is what keeps the percentage from misbehaving at both ends -- a $0.10 contract
        does not trade half-a-cent wide, and a $5.00 SPY contract does not trade fifteen cents
        wide. A *real* quoted width is only floored, never capped: if the market says it is
        forty cents wide, that is data, not an assumption to be argued with.
        """
        if quote.bid is not None and quote.ask is not None:
            return max(quote.ask - quote.bid, self.min_leg_width)
        price = reference_price(quote)
        if price is None:
            return None
        modelled = abs(price) * self.assumed_spread_pct_of_mid
        return min(max(modelled, self.min_leg_width), self.max_leg_width)

    def combo_width(self, legs: Sequence[Leg], quotes: Mapping[ContractKey, OptionQuote]) -> Optional[Decimal]:
        """Summed bid-ask width across every leg, scaled by each leg's ratio."""
        total = Decimal("0")
        for leg in legs:
            quote = quotes.get(contract_key(leg.contract))
            if quote is None:
                return None
            width = self.leg_width(quote)
            if width is None:
                return None
            total += width * leg.ratio
        return total

    def fill_price_per_share(
        self, legs: Sequence[Leg], quotes: Mapping[ContractKey, OptionQuote]
    ) -> Optional[Decimal]:
        """The modelled fill price for a combo, in cash-flow-to-trader terms.

        Always at or worse than the reference price, whichever direction the trade goes.
        Returns None if any leg is unpriceable.
        """
        reference = combo_reference_price(legs, quotes)
        if reference is None:
            return None
        width = self.combo_width(legs, quotes)
        if width is None:
            return None
        adverse = (self.fill_fraction(len(legs)) - Decimal("0.5")) * width
        return reference + max(adverse, Decimal("0"))

    def commission(self, legs: Sequence[Leg], quantity: int) -> Decimal:
        """Total commission for one transaction. Brokers charge per contract per leg, so a
        four-leg iron condor costs four times a single-leg trade of the same size -- which is
        precisely why this bot doesn't chase tiny credits with wide structures."""
        contracts = sum(leg.ratio for leg in legs) * quantity
        return self.commission_per_contract * contracts
