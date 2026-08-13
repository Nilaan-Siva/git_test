"""Iron condor: a put credit spread and a call credit spread on the same expiration.

A range-bound bet. It collects two credits instead of one for roughly the same capital at risk,
because only one side can finish in the money -- the underlying cannot be below the put spread
and above the call spread at the same expiration. That is the whole economic argument for the
structure, and it's why max loss is `max(put_width, call_width) - total_credit` rather than the
sum of the two sides' losses.

Disabled by default in strategies.yaml. It wants a higher IV Rank than a put credit spread (a
neutral position needs both sides to be paid for) and it carries four legs, so it pays four
commissions each way and gets the worst of the liquidity floors. Enable it only once the put
credit spread has a validated walk-forward backtest to compare against.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Leg, OptionQuote, OrderIntent, Spread
from optionsbot.strategy import filters
from optionsbot.strategy.base import Strategy, StrategyContext
from optionsbot.strategy.quoting import find_strike, select_expiration


def iron_condor_risk(
    *, put_width: Decimal, call_width: Decimal, credit_per_share: Decimal, multiplier: int
) -> tuple[Decimal, Decimal]:
    """(max_loss, max_profit) per contract, in dollars.

    Only one side of a condor can be breached at expiration, so the loss is bounded by the wider
    of the two spreads, less the total credit collected -- not by the two sides added together.
    Getting this wrong in the pessimistic direction would halve the position size the risk
    manager allows; getting it wrong the other way would understate risk, which is worse.
    """
    if credit_per_share <= 0:
        raise ValueError("an iron condor must be opened for a net credit")
    widest = max(put_width, call_width)
    if credit_per_share > widest:
        raise ValueError("credit received cannot exceed the widest spread's width")
    mult = Decimal(multiplier)
    return (widest - credit_per_share) * mult, credit_per_share * mult


class IronCondor(Strategy):
    name = StrategyName.IRON_CONDOR

    def propose(self, ctx: StrategyContext) -> list[OrderIntent]:
        if not self.enabled:
            return []

        for reason in (
            filters.check_iv_rank(ctx.iv_rank, self.params),
            filters.check_earnings_blackout(ctx.as_of, ctx.earnings_dates, ctx.earnings_blackout_days),
        ):
            if reason:
                ctx.note(reason)
                return []

        expiration = select_expiration(
            ctx.chain, dte_min=self.params.target_dte_min, dte_max=self.params.target_dte_max
        )
        if expiration is None:
            ctx.note(
                f"no_expiration_in_window: nothing between {self.params.target_dte_min} and "
                f"{self.params.target_dte_max} DTE"
            )
            return []

        put_side = self._select_side(ctx, expiration, Right.PUT)
        if put_side is None:
            return []
        call_side = self._select_side(ctx, expiration, Right.CALL)
        if call_side is None:
            return []

        short_put, long_put = put_side
        short_call, long_call = call_side
        if short_put.contract.strike >= short_call.contract.strike:
            ctx.note(
                f"inverted_condor: short put {short_put.contract.strike} is not below short "
                f"call {short_call.contract.strike}"
            )
            return []

        legs_quotes = [short_put, long_put, short_call, long_call]
        reason = filters.check_legs_liquidity(legs_quotes, ctx.universe)
        if reason:
            ctx.note(reason)
            return []

        spread = Spread(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                Leg(contract=short_put.contract, action=Action.SELL_TO_OPEN),
                Leg(contract=long_put.contract, action=Action.BUY_TO_OPEN),
                Leg(contract=short_call.contract, action=Action.SELL_TO_OPEN),
                Leg(contract=long_call.contract, action=Action.BUY_TO_OPEN),
            ],
        )

        price = ctx.price_combo(spread.legs)
        if price is None:
            ctx.note("unpriceable: at least one leg has no bid/ask or last price")
            return []
        if price >= 0:
            ctx.note(f"not_a_credit: modelled fill is a {price} debit, not a credit")
            return []

        credit = -price
        put_width = short_put.contract.strike - long_put.contract.strike
        call_width = long_call.contract.strike - short_call.contract.strike
        min_credit = max(put_width, call_width) * self.params.min_credit_pct_of_width
        if credit < min_credit:
            ctx.note(f"credit_too_small: {credit} < {min_credit} for the risk taken")
            return []

        try:
            max_loss, max_profit = iron_condor_risk(
                put_width=put_width,
                call_width=call_width,
                credit_per_share=credit,
                multiplier=short_put.contract.multiplier,
            )
        except ValueError as exc:
            ctx.note(f"inconsistent_pricing: {exc}")
            return []

        return [
            OrderIntent(
                strategy=self.name,
                spread=spread,
                limit_price_per_share=price,
                max_loss_per_contract=max_loss,
                max_profit_per_contract=max_profit,
                rationale=(
                    f"{ctx.underlying} iron condor {long_put.contract.strike}/"
                    f"{short_put.contract.strike}/{short_call.contract.strike}/"
                    f"{long_call.contract.strike}, {(expiration - ctx.as_of).days} DTE, "
                    f"IVR {ctx.iv_rank if ctx.iv_rank is None else round(ctx.iv_rank, 1)}, "
                    f"credit {credit} vs max loss {max_loss}"
                ),
            )
        ]

    def _select_side(
        self, ctx: StrategyContext, expiration: date, right: Right
    ) -> Optional[tuple[OptionQuote, OptionQuote]]:
        """(short, long) quotes for one wing, or None with a note explaining why not.

        The long strike sits further out of the money than the short one, which means *below*
        for the put wing and *above* for the call wing -- the one place a condor differs
        structurally from two copies of the same code.
        """
        short_quote = ctx.chain.nearest_to_delta(self.params.short_delta_target, expiration=expiration, right=right)
        if short_quote is None or short_quote.delta is None:
            ctx.note(f"no_delta_data: no {right.value.lower()} with a known delta expiring {expiration.isoformat()}")
            return None
        if abs(abs(short_quote.delta) - self.params.short_delta_target) > self.params.short_delta_tolerance:
            ctx.note(
                f"no_{right.value.lower()}_near_target_delta: closest is {abs(short_quote.delta):.3f} "
                f"vs target {self.params.short_delta_target}"
            )
            return None

        offset = -self.params.spread_width if right == Right.PUT else self.params.spread_width
        long_strike = short_quote.contract.strike + offset
        if long_strike <= 0:
            ctx.note(f"invalid_long_strike: {long_strike} for the {right.value.lower()} wing")
            return None
        long_quote = find_strike(ctx.chain.quotes, expiration=expiration, right=right, strike=long_strike)
        if long_quote is None:
            ctx.note(f"long_strike_not_listed: no {long_strike} {right.value.lower()} expiring {expiration.isoformat()}")
            return None
        return short_quote, long_quote
