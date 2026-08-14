"""Put credit spread: sell an out-of-the-money put, buy a further-out one for protection.

The bot's primary strategy, and the first one built, because it is the simplest structure that
is defined-risk in both directions: the long put caps the loss at (width - credit) no matter how
far the underlying falls, so the risk manager can size it exactly and a gap down cannot produce
an unbounded loss.

The setup, all driven by strategies.yaml:

  * only when IV Rank is high enough that the premium pays for the risk
  * short strike at roughly 30-delta -- around a 70% chance of expiring worthless
  * 30-50 DTE, where theta decay is meaningful but gamma risk is still tame
  * a strike width targeted as a FRACTION of the underlying's price, not a fixed points value --
    see StrategyParams.target_width_pct_of_spot for why. This is what lets the same strategy
    trade a $60 ETF and a $700 one without hand-tuning a width per ticker.

Exits are not this file's decision: they come from strategy/exits.py, driven by risk.yaml.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Leg, OptionQuote, OrderIntent, Spread
from optionsbot.strategy import filters
from optionsbot.strategy.base import Strategy, StrategyContext
from optionsbot.strategy.quoting import nearest_protective_strike, select_expiration


class PutCreditSpread(Strategy):
    name = StrategyName.PUT_CREDIT_SPREAD

    def propose(self, ctx: StrategyContext) -> list[OrderIntent]:
        if not self.enabled:
            return []

        for reason in (
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

        # Target a dollar width proportional to spot, then take whichever LISTED strike sits
        # closest to it -- different tickers grid their strikes differently ($1 near the money
        # on SPY, $5 or $10 elsewhere), so a strategy that wants "roughly X% of spot wide" has
        # to search the real chain rather than compute a strike and demand an exact match.
        spot = ctx.chain.underlying_price
        target_width = max(Decimal("1"), (spot * self.params.target_width_pct_of_spot).quantize(Decimal("1")))
        target_long_strike = short_quote.contract.strike - target_width
        long_quote = nearest_protective_strike(
            ctx.chain.quotes,
            expiration=expiration,
            right=Right.PUT,
            short_strike=short_quote.contract.strike,
            target_strike=target_long_strike,
        )
        if long_quote is None:
            ctx.note(
                f"no_protective_strike: no put below {short_quote.contract.strike} expiring "
                f"{expiration.isoformat()}"
            )
            return []

        # Check the width the chain actually offered, not the target -- a thin strike grid can
        # hand back something much wider than intended, and that is what has to clear the cap.
        actual_width = short_quote.contract.strike - long_quote.contract.strike
        reason = filters.check_width_suits_underlying(actual_width, spot, self.params)
        if reason:
            ctx.note(reason)
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
                    f"{ctx.underlying} {width}-wide put credit spread, "
                    f"short {short_quote.contract.strike} / long {long_quote.contract.strike}, "
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
