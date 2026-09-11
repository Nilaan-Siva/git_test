"""Tests for backtest/slippage.py.

The property that matters most is directional: slippage must always make a trade *worse* for
the trader, whether the trade is a credit or a debit. Under the cash-flow-to-trader convention
that means the fill price is always >= the reference price, with no per-strategy special cases.
A model that flattered credit spreads while penalising debit spreads would quietly manufacture
edge, so that invariant is tested from both directions.
"""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.backtest.slippage import (
    CROSS_FILL_FRACTIONS,
    RESEARCH_FILL_FRACTIONS,
    SlippageModel,
)
from optionsbot.core.enums import Action, Right
from optionsbot.core.models import Chain, Leg, OptionContract, OptionQuote
from optionsbot.strategy.quoting import combo_reference_price, quote_index

TODAY = date(2026, 8, 13)
EXP = date(2026, 9, 18)


def contract(strike: str, right: Right = Right.PUT) -> OptionContract:
    return OptionContract(underlying="SPY", expiration=EXP, strike=Decimal(strike), right=right)


def quote(strike: str, *, bid=None, ask=None, last=None, right=Right.PUT) -> OptionQuote:
    return OptionQuote(
        contract=contract(strike, right),
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        last=None if last is None else Decimal(last),
        open_interest=1000,
    )


def index(*quotes):
    return quote_index(Chain(underlying="SPY", as_of=TODAY, underlying_price=Decimal("450"), quotes=list(quotes)))


# ---- fill fraction lookup ---------------------------------------------------------------------


def test_research_fractions_tighten_as_leg_count_grows():
    """The empirical finding this model is built on: multi-leg orders fill closer to mid."""
    model = SlippageModel.realistic()
    fractions = [model.fill_fraction(n) for n in (1, 2, 3, 4)]
    assert fractions == sorted(fractions, reverse=True)
    assert model.fill_fraction(1) == RESEARCH_FILL_FRACTIONS[1]
    assert model.fill_fraction(4) == RESEARCH_FILL_FRACTIONS[4]


def test_fill_fraction_falls_back_to_the_nearest_lower_leg_count():
    """A five-leg structure should inherit the four-leg number, not revert to the much harsher
    single-leg one."""
    assert SlippageModel.realistic().fill_fraction(9) == RESEARCH_FILL_FRACTIONS[4]


def test_single_entry_models_apply_uniformly_to_any_leg_count():
    model = SlippageModel.pessimistic()
    assert model.fill_fraction(1) == model.fill_fraction(4) == CROSS_FILL_FRACTIONS[1]


def test_fill_fraction_rejects_nonsense_leg_counts():
    with pytest.raises(ValueError):
        SlippageModel.realistic().fill_fraction(0)


def test_fill_fraction_handles_a_model_with_no_low_leg_entry():
    model = SlippageModel(fill_fractions={4: Decimal("0.53")})
    assert model.fill_fraction(1) == Decimal("0.53")


# ---- leg width --------------------------------------------------------------------------------


def test_real_bid_ask_is_used_when_quoted():
    model = SlippageModel.realistic()
    assert model.leg_width(quote("450", bid="1.00", ask="1.30")) == Decimal("0.30")


def test_a_real_quoted_width_is_floored_but_never_capped():
    """A market saying it is forty cents wide is data, not an assumption to be argued with --
    only the *modelled* width is capped."""
    model = SlippageModel.realistic()
    assert model.leg_width(quote("450", bid="1.00", ask="1.40")) == Decimal("0.40")
    assert model.max_leg_width < Decimal("0.40")


def test_modelled_width_is_capped_so_expensive_contracts_are_not_overcharged():
    """The bug this guards against: assuming width scales with price charges a near-the-money
    SPY put a 20-cent spread when it really trades a penny wide, which on a 1-wide spread can
    exceed the entire credit and make any strategy look broken."""
    model = SlippageModel.realistic()
    assert model.leg_width(quote("450", last="8.00")) == model.max_leg_width


def test_modelled_width_is_floored_so_cheap_contracts_are_not_undercharged():
    model = SlippageModel.realistic()
    assert model.leg_width(quote("450", last="0.05")) == model.min_leg_width


def test_leg_width_is_none_without_any_price():
    assert SlippageModel.realistic().leg_width(quote("450")) is None


# ---- fill price: the directional invariant ------------------------------------------------------


@pytest.mark.parametrize("model_name", ["optimistic", "realistic", "pessimistic"])
@pytest.mark.parametrize(
    "short_action,long_action",
    [(Action.SELL_TO_OPEN, Action.BUY_TO_OPEN), (Action.BUY_TO_OPEN, Action.SELL_TO_OPEN)],
)
def test_slippage_never_improves_a_fill_in_either_direction(model_name, short_action, long_action):
    model = getattr(SlippageModel, model_name)()
    quotes = index(quote("450", last="2.00"), quote("445", last="1.00"))
    legs = [
        Leg(contract=contract("450"), action=short_action),
        Leg(contract=contract("445"), action=long_action),
    ]
    reference = combo_reference_price(legs, quotes)
    fill = model.fill_price_per_share(legs, quotes)
    assert fill is not None and reference is not None
    assert fill >= reference


def test_optimistic_model_fills_exactly_at_mid():
    model = SlippageModel.optimistic()
    quotes = index(quote("450", bid="1.95", ask="2.05"), quote("445", bid="0.95", ask="1.05"))
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert model.fill_price_per_share(legs, quotes) == combo_reference_price(legs, quotes)


def test_crossing_the_full_spread_equals_paying_ask_and_hitting_bid():
    """The derivation behind `(fraction - 0.5) * summed width`: at fraction 1.0 the modelled
    fill must equal buying at the ask and selling at the bid, exactly."""
    model = SlippageModel.pessimistic()
    short, long = quote("450", bid="1.90", ask="2.10"), quote("445", bid="0.90", ask="1.10")
    quotes = index(short, long)
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    # sell the 450 at its bid (1.90), buy the 445 at its ask (1.10) -> a 0.80 credit
    assert model.fill_price_per_share(legs, quotes) == Decimal("-0.80")


def _two_and_four_leg_costs(model):
    two_leg = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    four_leg = two_leg + [
        Leg(contract=contract("460", Right.CALL), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("465", Right.CALL), action=Action.BUY_TO_OPEN),
    ]
    quotes = index(
        quote("450", bid="1.90", ask="2.10"),
        quote("445", bid="0.90", ask="1.10"),
        quote("460", bid="1.90", ask="2.10", right=Right.CALL),
        quote("465", bid="0.90", ask="1.10", right=Right.CALL),
    )
    return (
        model.fill_price_per_share(two_leg, quotes) - combo_reference_price(two_leg, quotes),
        model.fill_price_per_share(four_leg, quotes) - combo_reference_price(four_leg, quotes),
    )


def test_research_model_makes_a_four_leg_condor_cheaper_to_fill_than_a_vertical():
    """This is what the research actually says, and it is counter-intuitive enough to pin down.

    A four-leg order fills at 53% of the width -- essentially at mid -- because the market maker
    prices the package rather than four separate contracts. So even though a condor has twice
    the summed leg width of a vertical, its modelled slippage is *lower*.

    The trap this creates is real: taken alone, the fill model quietly favours condors. What
    stops that from becoming a bias is elsewhere -- a condor pays double commission
    (`test_commission_scales_with_legs_and_quantity`), and the pessimistic run charges every
    leg in full (next test), which is the run a strategy is actually judged on.
    """
    two_cost, four_cost = _two_and_four_leg_costs(SlippageModel.realistic())
    assert four_cost < two_cost


def test_pessimistic_model_charges_a_condor_exactly_double_a_vertical():
    """The conservative bound, and the reason the research model's kindness to condors can't
    smuggle in a fake edge: crossing every leg in full scales strictly with leg count."""
    two_cost, four_cost = _two_and_four_leg_costs(SlippageModel.pessimistic())
    assert four_cost == two_cost * 2


def test_fill_price_is_none_when_a_leg_is_unpriceable():
    model = SlippageModel.realistic()
    quotes = index(quote("450", last="2.00"), quote("445"))
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert model.fill_price_per_share(legs, quotes) is None


def test_combo_width_is_none_when_a_leg_is_missing_from_the_chain():
    model = SlippageModel.realistic()
    quotes = index(quote("450", last="2.00"))
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert model.combo_width(legs, quotes) is None
    assert model.fill_price_per_share(legs, quotes) is None


def test_pessimistic_is_never_kinder_than_optimistic():
    quotes = index(quote("450", last="2.00"), quote("445", last="1.00"))
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert SlippageModel.pessimistic().fill_price_per_share(legs, quotes) >= SlippageModel.optimistic().fill_price_per_share(legs, quotes)


# ---- commission ---------------------------------------------------------------------------------


def test_commission_scales_with_legs_and_quantity():
    """Per contract per leg -- the reason a four-leg condor costs twice a vertical to trade."""
    model = SlippageModel.realistic()
    legs = [
        Leg(contract=contract("450"), action=Action.SELL_TO_OPEN),
        Leg(contract=contract("445"), action=Action.BUY_TO_OPEN),
    ]
    assert model.commission(legs, 1) == Decimal("1.30")
    assert model.commission(legs, 3) == Decimal("3.90")


def test_commission_respects_leg_ratio():
    model = SlippageModel.realistic()
    legs = [Leg(contract=contract("450"), action=Action.SELL_TO_OPEN, ratio=2)]
    assert model.commission(legs, 1) == Decimal("1.30")
