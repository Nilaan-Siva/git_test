"""A deterministic, fully synthetic chain provider.

This is a **test and development instrument, not a data source**. Nothing it returns is a real
market price, and no result produced against it is evidence that a strategy is profitable. What
it is good for:

  * exercising the backtest engine end to end before the Polygon backfill exists
  * regression-testing strategy and exit logic against a market whose behaviour is known
  * sanity checks that would be prohibitively slow or rate-limited against a live API

The generated market is an ordinary geometric-random-walk underlying with a mean-reverting
implied-volatility level that rises when price falls -- the leverage effect. That last detail
matters more than it looks: it is what makes IV Rank vary over time, and a synthetic market with
constant IV would let the IVR filter pass or fail uniformly and quietly hide bugs in it.

Options are priced with the same Black-Scholes engine the Polygon provider uses to fill in the
greeks its free tier omits (data/pricing.py), and quantised to the penny, because a market that
quotes fractions of a cent produces credit and slippage numbers that don't behave like real
ones. By default bid and ask are left None, exactly as the Polygon free tier leaves them, so
code paths that must cope with unquoted markets get exercised rather than bypassed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Optional

from optionsbot.core.clock import trading_days_between
from optionsbot.core.enums import Right
from optionsbot.core.models import Chain, OptionContract, OptionQuote
from optionsbot.data.pricing import DEFAULT_RISK_FREE_RATE, bs_greeks, bs_price, years_to_expiration
from optionsbot.data.providers.base import ChainProvider, DataUnavailableError

PENNY = Decimal("0.01")
MIN_OPTION_PRICE = Decimal("0.01")  # no exchange quotes an option below a penny


def _to_penny(value: float) -> Decimal:
    return Decimal(str(value)).quantize(PENNY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SyntheticMarketParams:
    """Parameters of the generated market.

    `variance_risk_premium` is the most consequential field here and deserves reading before
    anyone draws a conclusion from a synthetic backtest. It is the gap between the volatility
    options are *priced* at and the volatility the underlying actually *realizes*. In real index
    options that gap is persistently positive -- implied runs a few points above subsequent
    realized -- and it is the entire economic reason selling premium makes money. A strategy
    that sells options into a market with zero variance risk premium is playing a fair game
    before costs and a losing one after commissions, no matter how well it is implemented.

    So set it to 0.0 to check that the engine reports a small commission-sized loss (a strategy
    with no edge must not look profitable), and leave it positive to check that the engine can
    detect an edge that genuinely exists. Both are tests of the *engine*, not of the strategy.
    """

    start_price: float = 450.0
    annual_drift: float = 0.07
    annual_vol: float = 0.18  # the volatility the price path actually realizes
    variance_risk_premium: float = 0.03  # vol points that implied runs above realized
    iv_mean_reversion: float = 0.05
    iv_noise: float = 0.012
    # How strongly IV rises on a down day. The leverage effect is the reason IV Rank is a
    # tradable signal at all, so it is modelled explicitly rather than left to noise. Calibrated
    # roughly to index behaviour: a -1% day lifts IV about 1.5 vol points.
    iv_leverage: float = 1.5
    iv_floor: float = 0.08
    iv_cap: float = 0.75
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE

    @property
    def iv_mean(self) -> float:
        """The level implied volatility mean-reverts to."""
        return self.annual_vol + self.variance_risk_premium


class SyntheticChainProvider(ChainProvider):
    """Generates a reproducible option-chain history from a seed.

    The same seed always produces the same market, so a test that passes today passes in a year.
    """

    def __init__(
        self,
        *,
        start: date,
        end: date,
        underlyings: tuple[str, ...] = ("SPY",),
        params: Optional[SyntheticMarketParams] = None,
        # Seed 1 produces a market that trends up over a year with real pullbacks along the way
        # -- the ordinary conditions a premium-selling strategy is designed for, so the entry
        # and exit paths actually get exercised. This is a choice about test *coverage*, not a
        # favourable market cherry-picked to flatter results: other seeds produce bear regimes
        # in which the strategy correctly refuses to trade at all (seed 20260813 is one, and
        # tests assert exactly that). Never read a P&L number off this provider.
        seed: int = 1,
        strike_step: Decimal = Decimal("1"),
        # Sized to cover the configured strategy window cheaply rather than to mimic a full
        # listed chain: a 30-delta strike sits ~15 points from spot on a 450 underlying, and
        # nothing reads expirations past the 50-DTE entry ceiling except the 20-DTE floor that
        # the IV Rank series uses. Generating a realistic ±35 strikes out to 120 days triples
        # the Black-Scholes work per day for quotes no strategy ever looks at.
        strikes_each_side: int = 25,
        max_expiration_days: int = 70,
        open_interest: int = 2500,
        quote_bid_ask: bool = False,
        bid_ask_pct_of_mid: float = 0.06,
    ) -> None:
        self.start = start
        self.end = end
        self.underlyings = tuple(u.upper() for u in underlyings)
        self.params = params or SyntheticMarketParams()
        self.seed = seed
        self.strike_step = strike_step
        self.strikes_each_side = strikes_each_side
        self.max_expiration_days = max_expiration_days
        self.open_interest = open_interest
        self.quote_bid_ask = quote_bid_ask
        self.bid_ask_pct_of_mid = bid_ask_pct_of_mid
        self._paths: dict[str, dict[date, tuple[float, float]]] = {}

    # ---- market path ---------------------------------------------------------------------

    def _path(self, underlying: str) -> dict[date, tuple[float, float]]:
        """date -> (underlying price, at-the-money implied vol), generated once per underlying."""
        if underlying in self._paths:
            return self._paths[underlying]
        if underlying not in self.underlyings:
            raise DataUnavailableError(f"synthetic provider has no series for {underlying!r}")

        rng = random.Random(f"{self.seed}:{underlying}")
        p = self.params
        days = trading_days_between(self.start, self.end)
        dt = 1 / 252
        price = p.start_price
        iv = p.iv_mean
        path: dict[date, tuple[float, float]] = {}
        for day in days:
            shock = rng.gauss(0.0, 1.0)
            daily_return = (p.annual_drift - 0.5 * p.annual_vol**2) * dt + p.annual_vol * (dt**0.5) * shock
            price *= 2.718281828459045**daily_return
            iv += (
                p.iv_mean_reversion * (p.iv_mean - iv)
                - p.iv_leverage * daily_return
                + rng.gauss(0.0, p.iv_noise)
            )
            iv = min(max(iv, p.iv_floor), p.iv_cap)
            path[day] = (price, iv)
        self._paths[underlying] = path
        return path

    # ---- ChainProvider -------------------------------------------------------------------

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        path = self._path(underlying.upper())
        if as_of not in path:
            raise DataUnavailableError(f"no synthetic data for {underlying} on {as_of}")
        return _to_penny(path[as_of][0])

    def get_chain(self, underlying: str, as_of: date) -> Chain:
        underlying = underlying.upper()
        path = self._path(underlying)
        if as_of not in path:
            raise DataUnavailableError(f"no synthetic data for {underlying} on {as_of}")
        spot, atm_iv = path[as_of]

        quotes: list[OptionQuote] = []
        for expiration in _expirations(as_of, self.max_expiration_days):
            t = years_to_expiration((expiration - as_of).days)
            for strike in self._strikes(spot):
                for right in (Right.PUT, Right.CALL):
                    quote = self._quote(underlying, expiration, strike, right, spot, atm_iv, t)
                    if quote is not None:
                        quotes.append(quote)

        return Chain(underlying=underlying, as_of=as_of, underlying_price=_to_penny(spot), quotes=quotes)

    def _strikes(self, spot: float) -> list[Decimal]:
        step = self.strike_step
        centre = (Decimal(str(spot)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
        return [
            centre + step * offset
            for offset in range(-self.strikes_each_side, self.strikes_each_side + 1)
            if centre + step * offset > 0
        ]

    def _quote(
        self,
        underlying: str,
        expiration: date,
        strike: Decimal,
        right: Right,
        spot: float,
        atm_iv: float,
        t: float,
    ) -> Optional[OptionQuote]:
        iv = _smile_iv(atm_iv, spot=spot, strike=float(strike), t=t)
        is_call = right == Right.CALL
        try:
            price = bs_price(spot, float(strike), t, self.params.risk_free_rate, iv, is_call)
            greeks = bs_greeks(spot, float(strike), t, self.params.risk_free_rate, iv, is_call)
        except ValueError:
            return None

        last = _to_penny(price)
        if last < MIN_OPTION_PRICE:
            # Sub-penny options aren't listed at a tradable price; omitting them keeps the
            # strategy from "selling" contracts no market maker would fill.
            return None

        bid = ask = None
        if self.quote_bid_ask:
            half_width = last * Decimal(str(self.bid_ask_pct_of_mid)) / 2
            bid = max(PENNY, (last - half_width).quantize(PENNY, rounding=ROUND_HALF_UP))
            ask = (last + half_width).quantize(PENNY, rounding=ROUND_HALF_UP)

        return OptionQuote(
            contract=OptionContract(underlying=underlying, expiration=expiration, strike=strike, right=right),
            bid=bid,
            ask=ask,
            last=last,
            volume=self.open_interest // 10,
            open_interest=self.open_interest,
            implied_volatility=Decimal(str(round(iv, 4))),
            delta=Decimal(str(round(greeks["delta"], 4))),
            gamma=Decimal(str(round(greeks["gamma"], 6))),
            theta=Decimal(str(round(greeks["theta"], 4))),
            vega=Decimal(str(round(greeks["vega"], 4))),
        )


def _smile_iv(atm_iv: float, *, spot: float, strike: float, t: float) -> float:
    """A simple equity-index volatility smile: downside strikes carry more IV than upside ones.

    Without skew, a 30-delta put and a 30-delta call would collect the same premium, and an iron
    condor's two wings would look interchangeable -- which is not how index options trade, and
    would make backtest credits systematically too low on the put side.
    """
    moneyness = (strike - spot) / spot
    skew = -0.55 * moneyness  # richer puts (negative moneyness) -> higher IV
    curvature = 1.8 * moneyness * moneyness
    return max(0.05, atm_iv + skew + curvature)


@lru_cache(maxsize=512)
def _expirations(as_of: date, max_days: int) -> tuple[date, ...]:
    """Every Friday within `max_days`, approximating standard weekly option expirations."""
    days_until_friday = (4 - as_of.weekday()) % 7
    first = as_of + timedelta(days=days_until_friday or 7)
    out = []
    day = first
    while (day - as_of).days <= max_days:
        out.append(day)
        day += timedelta(days=7)
    return tuple(out)
