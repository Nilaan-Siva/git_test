"""The Strategy interface: everything that can propose a trade.

Strategies are the only part of the system allowed to have an opinion about *what* to trade.
They are deliberately powerless about *how much*: a strategy returns `OrderIntent`s with no
quantity at all, and the risk manager (optionsbot/risk/manager.approve) is what turns an intent
into a sized `Order`, or refuses. A strategy can therefore never breach the 1% rule, however
buggy it is -- the worst it can do is propose a trade that gets vetoed.

The same Strategy objects run in the backtest, in paper trading, and (eventually) live. That is
the whole point of `StrategyContext`: it hands the strategy a chain snapshot and a pricing
function without saying where either came from, so a strategy cannot accidentally behave
differently in a backtest than it will with real money on the line.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, Mapping, Optional, Sequence

from optionsbot.config.schema import StrategyParams, UniverseConfig
from optionsbot.core.enums import StrategyName
from optionsbot.core.models import Chain, Leg, OptionQuote, OrderIntent
from optionsbot.strategy.quoting import ContractKey, quote_index


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy is allowed to look at on one underlying, on one day.

    `price_combo` is supplied by the caller and returns the *expected fill* price for a set of
    legs in cash-flow-to-trader terms (negative = credit). Strategies price their intents with
    it rather than with raw mids, so the max-loss number the risk manager sizes against already
    accounts for modelled slippage instead of assuming a fill nobody gets.
    """

    as_of: date
    chain: Chain
    quotes: Mapping[ContractKey, OptionQuote]
    params: StrategyParams
    universe: UniverseConfig
    price_combo: Callable[[Sequence[Leg]], Optional[Decimal]]
    iv_rank: Optional[Decimal] = None
    underlying_closes: Sequence[float] = ()
    earnings_dates: Sequence[date] = ()
    # From risk.yaml, not strategies.yaml -- the blackout length is a risk rule the learning
    # loop may not tune, but strategies are the ones that have to honour it at entry time.
    earnings_blackout_days: int = 7
    notes: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        chain: Chain,
        params: StrategyParams,
        universe: UniverseConfig,
        price_combo: Callable[[Sequence[Leg]], Optional[Decimal]],
        iv_rank: Optional[Decimal] = None,
        underlying_closes: Sequence[float] = (),
        earnings_dates: Sequence[date] = (),
        earnings_blackout_days: int = 7,
    ) -> "StrategyContext":
        """Build a context from a chain, indexing its quotes for lookup."""
        return cls(
            as_of=chain.as_of,
            chain=chain,
            quotes=quote_index(chain),
            params=params,
            universe=universe,
            price_combo=price_combo,
            iv_rank=iv_rank,
            underlying_closes=underlying_closes,
            earnings_dates=earnings_dates,
            earnings_blackout_days=earnings_blackout_days,
        )

    def note(self, reason: str) -> None:
        """Record why a trade was *not* proposed.

        A day with no trade is data. The journal wants "IVR 18 below the 30 floor" rather than
        silence, both so the evening report can explain an empty day and so Phase 7 can tell a
        strategy that is patiently waiting apart from one that is quietly broken.
        """
        self.notes.append(reason)

    @property
    def underlying(self) -> str:
        return self.chain.underlying


class Strategy(ABC):
    """Base class for all strategies. Subclasses set `name` and implement `propose`."""

    name: StrategyName

    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    @property
    def enabled(self) -> bool:
        return self.params.enabled

    @abstractmethod
    def propose(self, ctx: StrategyContext) -> list[OrderIntent]:
        """Zero or more unsized trade proposals for this underlying today.

        Returning an empty list is the normal case -- most days most strategies should not
        trade. Implementations should call `ctx.note(...)` explaining each rejection rather than
        returning empty silently, and must never raise on ordinary bad data (a missing strike, a
        chain with no eligible expiration); those are `note` + empty-list situations.
        """

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name.value}, enabled={self.enabled})"
