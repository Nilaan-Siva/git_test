"""Tests for strategy/quoting.py -- combo pricing and chain selection.

The sign convention is the thing under test here more than anything else. Every price in the
system is cash-flow-to-trader (positive = you pay), and a sign error in this module would
silently invert P&L everywhere downstream, so credits and debits are asserted explicitly rather
than by magnitude.
"""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Chain, Leg, OptionContract, OptionQuote, Spread
from optionsbot.strategy.quoting import (
    atm_implied_volatility,
    combo_intrinsic_price,
    combo_reference_price,
    contract_key,
    find_strike,
    intrinsic_value,
    quote_index,
    reference_price,
    select_expiration,
)

TODAY = date(2026, 8, 13)
EXP = date(2026, 9, 18)


def contract(strike: str, right: Right = Right.PUT, expiration: date = EXP) -> OptionContract:
    return OptionContract(underlying="SPY", expiration=expiration, strike=Decimal(strike), right=right)


def quote(strike: str, *, bid=None, ask=None, last=None, right=Right.PUT, expiration=EXP, iv=None, delta=None, oi=1000):
    return OptionQuote(
        contract=contract(strike, right, expiration),
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        last=None if last is None else Decimal(last),
        implied_volatility=None if iv is None else Decimal(iv),
        delta=None if delta is None else Decimal(delta),
        open_interest=oi,
    )


# ---- reference pricing -----------------------------------------------------------------------


def test_reference_price_prefers_mid_over_last():
    assert reference_price(quote("450", bid="1.00", ask="1.20", last="5.00")) == Decimal("1.10")


def test_reference_price_falls_back_to_last_when_unquoted():
    assert reference_price(quote("450", last="1.05")) == Decimal("1.05")


def test_reference_price_is_none_when_nothing_is_available():
    assert reference_price(quote("450")) is None


# ---- combo pricing ---------------------------------------------------------------------------


def test_credit_spread_prices_as_a_negative_number():
    """Sell the 450 put for 2.00, buy the 445 put for 1.00 -> a 1.00 credit received."""
    quotes = quote_index(
        Chain(
            underlying="SPY",
            as_of=TODAY,
            underlying_price=Decimal("455"),
            quotes=[quote("450", last="2.00"), quote("445", last="1.00")],
        )
    )
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert combo_reference_price(legs, quotes) == Decimal("-1.00")


def test_debit_spread_prices_as_a_positive_number():
    quotes = quote_index(
        Chain(
            underlying="SPY",
            as_of=TODAY,
            underlying_price=Decimal("455"),
            quotes=[quote("450", last="2.00"), quote("445", last="1.00")],
        )
    )
    legs = [
        Leg(contract=contract("450"), action=Action.BUY_TO_OPEN),
        Leg(contract=contract("445"), action=Action.SELL_TO_OPEN),
    ]
    assert combo_reference_price(legs, quotes) == Decimal("1.00")


def test_closing_a_credit_spread_is_a_debit():
    """Round-trip check: the closing transaction must flip sign relative to opening."""
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
            Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
        ],
    )
    quotes = quote_index(
        Chain(
            underlying="SPY",
            as_of=TODAY,
            underlying_price=Decimal("455"),
            quotes=[quote("450", last="0.80"), quote("445", last="0.30")],
        )
    )
    assert combo_reference_price(spread.closing_legs(), quotes) == Decimal("0.50")


def test_combo_price_is_none_if_any_leg_is_unpriceable():
    """A partially-priced spread is not a price -- dropping the unknown leg would understate
    the cost of the trade."""
    quotes = quote_index(
        Chain(
            underlying="SPY",
            as_of=TODAY,
            underlying_price=Decimal("455"),
            quotes=[quote("450", last="2.00"), quote("445")],  # 445 has no price at all
        )
    )
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert combo_reference_price(legs, quotes) is None


def test_combo_price_is_none_when_a_leg_is_missing_from_the_chain():
    quotes = quote_index(
        Chain(underlying="SPY", as_of=TODAY, underlying_price=Decimal("455"), quotes=[quote("450", last="2.00")])
    )
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert combo_reference_price(legs, quotes) is None


def test_combo_price_respects_leg_ratio():
    quotes = quote_index(
        Chain(underlying="SPY", as_of=TODAY, underlying_price=Decimal("455"), quotes=[quote("450", last="2.00")])
    )
    legs = [Leg(contract=contract("450"), action=Action.BUY_TO_OPEN, ratio=3)]
    assert combo_reference_price(legs, quotes) == Decimal("6.00")


# ---- intrinsic settlement --------------------------------------------------------------------


@pytest.mark.parametrize(
    "right,strike,spot,expected",
    [
        (Right.PUT, "450", "440", "10"),  # in the money
        (Right.PUT, "450", "460", "0"),  # out of the money
        (Right.CALL, "450", "460", "10"),
        (Right.CALL, "450", "440", "0"),
        (Right.PUT, "450", "450", "0"),  # exactly at the money
    ],
)
def test_intrinsic_value(right, strike, spot, expected):
    assert intrinsic_value(contract(strike, right), Decimal(spot)) == Decimal(expected)


def test_fully_breached_put_spread_settles_at_its_width():
    """Below both strikes, a short put spread costs its full width to settle -- the max loss."""
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
            Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
        ],
    )
    assert combo_intrinsic_price(spread.closing_legs(), Decimal("400")) == Decimal("5")


def test_untouched_put_spread_settles_at_zero():
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
            Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
        ],
    )
    assert combo_intrinsic_price(spread.closing_legs(), Decimal("470")) == Decimal("0")


def test_partially_breached_put_spread_settles_between_zero_and_width():
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
            Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
        ],
    )
    assert combo_intrinsic_price(spread.closing_legs(), Decimal("447")) == Decimal("3")


# ---- chain selection -------------------------------------------------------------------------


def make_chain(expirations, *, as_of=TODAY) -> Chain:
    quotes = [quote("450", last="1.00", expiration=exp, iv="0.20") for exp in expirations]
    return Chain(underlying="SPY", as_of=as_of, underlying_price=Decimal("450"), quotes=quotes)


def test_select_expiration_picks_closest_to_the_middle_of_the_window():
    chain = make_chain([TODAY.replace(day=TODAY.day) for _ in range(0)] or [
        date(2026, 9, 4),   # 22 DTE -- outside
        date(2026, 9, 18),  # 36 DTE
        date(2026, 9, 25),  # 43 DTE -- closest to the 40 midpoint
        date(2026, 10, 30),  # 78 DTE -- outside
    ])
    assert select_expiration(chain, dte_min=30, dte_max=50) == date(2026, 9, 25)


def test_select_expiration_returns_none_when_nothing_is_in_the_window():
    chain = make_chain([date(2026, 8, 21), date(2026, 12, 18)])
    assert select_expiration(chain, dte_min=30, dte_max=50) is None


def test_select_expiration_is_deterministic_on_ties():
    """Two expirations equidistant from the midpoint must resolve the same way every run, or
    backtest and live can pick different trades from the same chain."""
    chain = make_chain([date(2026, 9, 17), date(2026, 9, 27)])  # 35 and 45 DTE, midpoint 40
    assert select_expiration(chain, dte_min=30, dte_max=50) == date(2026, 9, 17)


def test_find_strike_matches_exactly_and_returns_none_otherwise():
    chain = Chain(
        underlying="SPY",
        as_of=TODAY,
        underlying_price=Decimal("450"),
        quotes=[quote("445", last="1.00"), quote("450", last="2.00")],
    )
    found = find_strike(chain.quotes, expiration=EXP, right=Right.PUT, strike=Decimal("445"))
    assert found is not None and found.contract.strike == Decimal("445")
    assert find_strike(chain.quotes, expiration=EXP, right=Right.PUT, strike=Decimal("444")) is None
    assert find_strike(chain.quotes, expiration=EXP, right=Right.CALL, strike=Decimal("445")) is None


# ---- ATM implied volatility ------------------------------------------------------------------


def test_atm_iv_picks_the_strike_nearest_spot_in_the_nearest_eligible_expiration():
    near, far = date(2026, 9, 10), date(2026, 10, 16)  # 28 and 64 DTE
    chain = Chain(
        underlying="SPY",
        as_of=TODAY,
        underlying_price=Decimal("450"),
        quotes=[
            quote("440", last="1", expiration=near, iv="0.30"),
            quote("451", last="1", expiration=near, iv="0.21"),  # nearest to spot
            quote("450", last="1", expiration=far, iv="0.99"),
        ],
    )
    assert atm_implied_volatility(chain) == Decimal("0.21")


def test_atm_iv_skips_expirations_inside_the_min_dte_floor():
    """Front-week IV is dominated by event and gamma effects, not the volatility level IV Rank
    is trying to measure, so it must not be the series that feeds the filter."""
    chain = Chain(
        underlying="SPY",
        as_of=TODAY,
        underlying_price=Decimal("450"),
        quotes=[
            quote("450", last="1", expiration=date(2026, 8, 21), iv="0.90"),  # 8 DTE
            quote("450", last="1", expiration=date(2026, 9, 18), iv="0.20"),  # 36 DTE
        ],
    )
    assert atm_implied_volatility(chain) == Decimal("0.20")


def test_atm_iv_is_none_when_no_expiration_is_far_enough_out():
    chain = make_chain([date(2026, 8, 18)])
    assert atm_implied_volatility(chain) is None


def test_atm_iv_is_none_when_no_quote_carries_an_iv():
    chain = Chain(
        underlying="SPY",
        as_of=TODAY,
        underlying_price=Decimal("450"),
        quotes=[quote("450", last="1", expiration=EXP)],  # no iv
    )
    assert atm_implied_volatility(chain) is None


# ---- indexing --------------------------------------------------------------------------------


def test_contract_key_ignores_cosmetic_fields():
    """A contract rebuilt from a later day's chain, or round-tripped through the Parquet cache,
    must index to the same key even if the provider filled in an OCC symbol."""
    plain = contract("450")
    with_symbol = OptionContract(
        underlying="SPY", expiration=EXP, strike=Decimal("450"), right=Right.PUT, occ_symbol="SPY260918P00450000"
    )
    assert contract_key(plain) == contract_key(with_symbol)


def test_contract_key_treats_equal_decimals_as_the_same_strike():
    assert contract_key(contract("450")) == contract_key(contract("450.0"))
