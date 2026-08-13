"""Put credit spread: sell an out-of-the-money put, buy a further-out one for protection.

The bot's primary strategy, and the first one built, because it is the simplest structure that
is defined-risk in both directions: the long put caps the loss at (width - credit) no matter how
far the underlying falls, so the risk manager can size it exactly and a gap down cannot produce
an unbounded loss.

The setup, all driven by strategies.yaml:

  * only when IV Rank is high enough that the premium pays for the risk
  * short strike at roughly 30-delta -- around a 70% chance of expiring worthless
  * 30-50 DTE, where theta decay is meaningful but gamma risk is still tame
  * a fixed strike width (1-wide by default, which is what makes this viable on a small account)

Exits are not this file's decision: they come from strategy/exits.py, driven by risk.yaml.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Leg, OptionQuote, OrderIntent, Spread
from optionsbot.strategy import filters
from optionsbot.strategy.base import Strategy, StrategyContext
from optionsbot.strategy.quoting import find_strike, select_expiration


class PutCreditSpread(Strategy):
    name = StrategyName.PUT_CREDIT_SPREAD

    def propose(self, ctx: StrategyContext) -> list[OrderIntent]:
        if not self.enabled:
            return []

        for reason in (
            filters.check_width_suits_underlying(
                self.params.spread_width, ctx.chain.underlying_price, self.params
            ),
            filters.check_iv_rank(ctx.iv_rank, self.params),
            filters.check_earnings_blackout(ctx.as_of, ctx.earnings_dates, ctx.earnings_blackout_days),
            filters.check_not_downtrend(ctx.underlying_closes, period=self.params.trend_sma_period),
        ):
            if reason:
                ctx.note(reason)
                return []

        expiration = select_expiration(ctx.chain, dte_min=self.params.target_dte_min, dte_max=self.params.target_dte_max)
        if expiration is None:
            ctx.note(
                f"no_expiration_in_window: nothing between {self.params.target_dte_min} and "
                f"{self.params.target_dte_max} DTE"
            )
            return []

        short_quote = self._select_short_put(ctx, expiration)
        if short_quote is None:
            return []

        long_strike = short_quote.contract.strike - self.params.spread_width
        if long_strike <= 0:
            ctx.note(f"invalid_long_strike: {short_quote.contract.strike} - {self.params.spread_width} <= 0")
            return []
        long_quote = find_strike(ctx.chain.quotes, expiration=expiration, right=Right.PUT, strike=long_strike)
        if long_quote is None:
            ctx.note(f"long_strike_not_listed: no {long_strike} put expiring {expiration.isoformat()}")
            return []

        reason = filters.check_legs_liquidity([short_quote, long_quote], ctx.universe)
        if reason:
            ctx.note(reason)
            return []

        spread = Spread(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                Leg(contract=short_quote.contract, action=Action.SELL_TO_OPEN),
                Leg(contract=long_quote.contract, action=Action.BUY_TO_OPEN),
            ],
        )

        price = ctx.price_combo(spread.legs)
        if price is None:
            ctx.note("unpriceable: one or both legs have no bid/ask or last price")
            return []
        if price >= 0:
            ctx.note(f"not_a_credit: modelled fill is a {price} debit, not a credit")
            return []

        credit = -price
        width = spread.width
        assert width is not None  # a two-leg vertical always has a width
        min_credit = width * self.params.min_credit_pct_of_width
        if credit < min_credit:
            ctx.note(f"credit_too_small: {credit} < {min_credit} ({self.params.min_credit_pct_of_width:.0%} of {width} width)")
            return []

        try:
            max_loss, max_profit = spread.defined_risk(price)
        except ValueError as exc:  # credit exceeding width means the chain data is inconsistent
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
                    f"{ctx.underlying} {self.params.spread_width}-wide put credit spread, "
                    f"short {short_quote.contract.strike} / long {long_strike}, "
                    f"{(expiration - ctx.as_of).days} DTE, "
                    f"short delta {abs(short_quote.delta or Decimal('0')):.2f}, "
                    f"IVR {ctx.iv_rank if ctx.iv_rank is None else round(ctx.iv_rank, 1)}, "
                    f"credit {credit} vs max loss {max_loss}"
                ),
            )
        ]

    def _select_short_put(self, ctx: StrategyContext, expiration) -> Optional[OptionQuote]:
        """The short leg: the put closest to the target delta, if it's close enough.

        The tolerance check matters. `Chain.nearest_to_delta` always returns *something* when
        any put has a delta, so without it a chain with only far-OTM 5-delta strikes listed
        would happily sell one and call it a 30-delta trade.
        """
        quote = ctx.chain.nearest_to_delta(self.params.short_delta_target, expiration=expiration, right=Right.PUT)
        if quote is None or quote.delta is None:
            ctx.note(f"no_delta_data: no put with a known delta expiring {expiration.isoformat()}")
            return None
        distance = abs(abs(quote.delta) - self.params.short_delta_target)
        if distance > self.params.short_delta_tolerance:
            ctx.note(
                f"no_strike_near_target_delta: closest is {abs(quote.delta):.3f} vs target "
                f"{self.params.short_delta_target} (tolerance {self.params.short_delta_tolerance})"
            )
            return None
        return quote
