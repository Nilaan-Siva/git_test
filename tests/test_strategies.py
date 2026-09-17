"""Tests for the strategy implementations and the registry.

Strategies are the only component allowed an opinion about what to trade, so these tests care
about two things: that a valid setup produces a *correctly structured* intent (right legs, right
direction, honest max-loss arithmetic), and that every rejection path records a reason instead
of returning empty silently. A strategy that quietly declines is indistinguishable in a report
from one that is broken.
"""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.config.schema import StrategiesConfig, StrategyParams, UniverseConfig
from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Chain, OptionContract, OptionQuote
from optionsbot.strategy.base import StrategyContext
from optionsbot.strategy.iron_condor import IronCondor, iron_condor_risk
from optionsbot.strategy.put_credit_spread import PutCreditSpread
from optionsbot.strategy.quoting import combo_reference_price, quote_index
from optionsbot.strategy.registry import (
    STRATEGY_CLASSES,
    build_enabled_strategies,
    build_strategy,
    params_for,
)

TODAY = date(2026, 8, 13)
EXP = date(2026, 9, 22)  # 40 DTE -- the middle of the default 30-50 window
UNIVERSE = UniverseConfig()
SPOT = Decimal("450")
UPTREND = [float(300 + i) for i in range(200)] + [449.0, 450.0]


def put_quote(strike: int, *, delta: str, price: str, oi: int = 5000, expiration: date = EXP) -> OptionQuote:
    return OptionQuote(
        contract=OptionContract(
            underlying="SPY", expiration=expiration, strike=Decimal(strike), right=Right.PUT
        ),
        last=Decimal(price),
        open_interest=oi,
        implied_volatility=Decimal("0.20"),
        delta=Decimal(delta),
    )


def call_quote(strike: int, *, delta: str, price: str, oi: int = 5000, expiration: date = EXP) -> OptionQuote:
    return OptionQuote(
        contract=OptionContract(
            underlying="SPY", expiration=expiration, strike=Decimal(strike), right=Right.CALL
        ),
        last=Decimal(price),
        open_interest=oi,
        implied_volatility=Decimal("0.20"),
        delta=Decimal(delta),
    )


def make_context(
    quotes, *, params=None, iv_rank="60", closes=None, earnings=(), spot=SPOT
) -> StrategyContext:
    chain = Chain(underlying="SPY", as_of=TODAY, underlying_price=spot, quotes=list(quotes))
    indexed = quote_index(chain)
    return StrategyContext.build(
        chain=chain,
        params=params or StrategyParams(),
        universe=UNIVERSE,
        # No slippage: these tests are about strike selection and structure, not fills.
        price_combo=lambda legs: combo_reference_price(legs, indexed),
        iv_rank=None if iv_rank is None else Decimal(iv_rank),
        underlying_closes=UPTREND if closes is None else closes,
        earnings_dates=earnings,
    )


def default_put_chain():
    """A chain whose 439 put sits at 30 delta, with a 438 available one point below."""
    return [
        put_quote(441, delta="-0.36", price="4.90"),
        put_quote(440, delta="-0.33", price="4.60"),
        put_quote(439, delta="-0.30", price="4.30"),
        put_quote(438, delta="-0.27", price="4.00"),
        put_quote(437, delta="-0.24", price="3.72"),
    ]


# ---- put credit spread: the happy path -----------------------------------------------------------


def test_proposes_a_correctly_structured_put_credit_spread():
    ctx = make_context(default_put_chain())
    intents = PutCreditSpread(ctx.params).propose(ctx)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.strategy == StrategyName.PUT_CREDIT_SPREAD

    short, long = intent.spread.legs
    assert short.action == Action.SELL_TO_OPEN and short.contract.strike == Decimal("439")
    # 0.4% of spot (450) targets a 2-wide spread, and 437 is exactly 2 below the short.
    assert long.action == Action.BUY_TO_OPEN and long.contract.strike == Decimal("437")
    assert short.contract.right == long.contract.right == Right.PUT
    # long strike is BELOW the short: the protective leg must be further out of the money
    assert long.contract.strike < short.contract.strike


def test_intent_is_priced_as_a_credit_with_matching_defined_risk():
    ctx = make_context(default_put_chain())
    intent = PutCreditSpread(ctx.params).propose(ctx)[0]

    # sell the 439 for 4.30, buy the 437 for 3.72 -> 0.58 credit on a 2-wide spread
    assert intent.limit_price_per_share == Decimal("-0.58")
    assert intent.max_profit_per_contract == Decimal("58.00")  # 0.58 * 100
    assert intent.max_loss_per_contract == Decimal("142.00")  # (2.00 - 0.58) * 100
    # the two must sum to the full width, or the risk manager is sizing against a fiction
    assert intent.max_loss_per_contract + intent.max_profit_per_contract == Decimal("200")


def test_intent_carries_a_rationale_for_the_journal():
    ctx = make_context(default_put_chain())
    intent = PutCreditSpread(ctx.params).propose(ctx)[0]
    assert "put credit spread" in intent.rationale
    assert "439" in intent.rationale


def wide_put_chain():
    """$1-spaced puts from 430 to 441 -- wide enough to test width scaling, not just a single
    candidate below the short."""
    return [
        put_quote(441, delta="-0.36", price="4.90"),
        put_quote(440, delta="-0.33", price="4.60"),
        put_quote(439, delta="-0.30", price="4.30"),
        put_quote(438, delta="-0.27", price="4.00"),
        put_quote(437, delta="-0.24", price="3.72"),
        put_quote(436, delta="-0.21", price="3.44"),
        put_quote(435, delta="-0.18", price="3.16"),
        put_quote(434, delta="-0.15", price="2.90"),
        put_quote(433, delta="-0.12", price="2.65"),
        put_quote(432, delta="-0.09", price="2.40"),
        put_quote(431, delta="-0.06", price="2.20"),
        put_quote(430, delta="-0.03", price="2.00"),
    ]


@pytest.mark.parametrize(
    "target_pct,expected_long_strike",
    [
        (Decimal("0.004"), Decimal("437")),  # 0.4% of 450 -> target width 2
        (Decimal("0.01"), Decimal("435")),  # 1% of 450 -> target width 4
        (Decimal("0.02"), Decimal("430")),  # 2% of 450 -> target width 9, right at the cap
    ],
)
def test_width_scales_as_a_fraction_of_spot(target_pct, expected_long_strike):
    """target_width_pct_of_spot -- not a fixed points value -- drives strike selection: the same
    setting has to widen when spot is higher, which is the whole point of the rebuild."""
    params = StrategyParams(target_width_pct_of_spot=target_pct)
    ctx = make_context(wide_put_chain(), params=params)
    intent = PutCreditSpread(params).propose(ctx)[0]
    assert intent.spread.legs[1].contract.strike == expected_long_strike


# ---- put credit spread: rejection paths ------------------------------------------------------------


def test_disabled_strategy_proposes_nothing():
    params = StrategyParams(enabled=False)
    ctx = make_context(default_put_chain(), params=params)
    assert PutCreditSpread(params).propose(ctx) == []


def test_rejects_and_notes_low_iv_rank():
    ctx = make_context(default_put_chain(), iv_rank="10")
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("iv_rank_too_low" in note for note in ctx.notes)


def test_rejects_and_notes_a_downtrend():
    ctx = make_context(default_put_chain(), closes=[float(500 - i) for i in range(201)])
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("downtrend" in note for note in ctx.notes)


def test_rejects_and_notes_an_earnings_blackout():
    ctx = make_context(default_put_chain(), earnings=[date(2026, 8, 17)])
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("earnings_blackout" in note for note in ctx.notes)


def test_rejects_when_no_expiration_is_in_the_dte_window():
    near = [put_quote(439, delta="-0.30", price="4.30", expiration=date(2026, 8, 21))]  # 8 DTE
    ctx = make_context(near)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("no_expiration_in_window" in note for note in ctx.notes)


def test_rejects_when_nothing_is_near_the_target_delta():
    """`Chain.nearest_to_delta` always returns something if any put has a delta, so without the
    tolerance check a chain of far-OTM 5-delta strikes would be sold as a '30-delta trade'."""
    far_otm = [put_quote(400, delta="-0.05", price="0.40"), put_quote(399, delta="-0.04", price="0.35")]
    ctx = make_context(far_otm)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("no_strike_near_target_delta" in note for note in ctx.notes)


def test_rejects_when_no_put_carries_a_delta():
    no_delta = [
        OptionQuote(
            contract=OptionContract(underlying="SPY", expiration=EXP, strike=Decimal("439"), right=Right.PUT),
            last=Decimal("4.30"),
            open_interest=5000,
        )
    ]
    ctx = make_context(no_delta)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("no_delta_data" in note for note in ctx.notes)


def test_finds_the_nearest_protective_strike_when_the_target_is_not_listed():
    """A gap in the strike grid is not a rejection -- the nearest listed strike still protects
    the short leg, just at a different width than the target. This is the capability the
    rebuild exists for: real chains don't always list a strike at the exact target."""
    gapped = [put_quote(439, delta="-0.30", price="4.30"), put_quote(435, delta="-0.22", price="3.40")]
    ctx = make_context(gapped)
    intent = PutCreditSpread(ctx.params).propose(ctx)[0]
    assert intent.spread.legs[1].contract.strike == Decimal("435")


def test_rejects_when_nothing_protects_the_short_leg():
    """No put listed below the short strike at all -- distinct from a gap, this is genuinely
    'no usable spread here', not the strategy being picky about width."""
    only_short = [put_quote(439, delta="-0.30", price="4.30")]
    ctx = make_context(only_short)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("no_protective_strike" in note for note in ctx.notes)


def test_rejects_an_illiquid_leg():
    thin = [put_quote(439, delta="-0.30", price="4.30"), put_quote(438, delta="-0.27", price="4.00", oi=3)]
    ctx = make_context(thin)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("illiquid_open_interest" in note for note in ctx.notes)


def test_rejects_a_credit_too_small_to_be_worth_the_commissions():
    """A 1-wide spread collecting three cents is $3 of max profit against $2.60 of round-trip
    commission -- structurally not a trade, however good the signal looks."""
    stingy = [put_quote(439, delta="-0.30", price="4.03"), put_quote(438, delta="-0.27", price="4.00")]
    ctx = make_context(stingy)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("credit_too_small" in note for note in ctx.notes)


def test_rejects_when_the_combo_prices_as_a_debit():
    """Inconsistent data can invert the strikes' prices. Selling a spread for a debit is never
    a put credit spread, and the defined-risk arithmetic would be wrong if it went through."""
    inverted = [put_quote(439, delta="-0.30", price="4.00"), put_quote(438, delta="-0.27", price="4.30")]
    ctx = make_context(inverted)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("not_a_credit" in note for note in ctx.notes)


def test_rejects_an_unpriceable_leg():
    unpriced = [
        put_quote(439, delta="-0.30", price="4.30"),
        OptionQuote(
            contract=OptionContract(underlying="SPY", expiration=EXP, strike=Decimal("438"), right=Right.PUT),
            open_interest=5000,
            delta=Decimal("-0.27"),
        ),
    ]
    ctx = make_context(unpriced)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("unpriceable" in note for note in ctx.notes)


def test_rejects_when_the_credit_would_exceed_the_width():
    """Impossible pricing -- a 1-wide spread cannot pay more than $1. Caught rather than
    allowed to produce a negative max loss the risk manager would happily size against."""
    impossible = [put_quote(439, delta="-0.30", price="6.00"), put_quote(438, delta="-0.27", price="4.00")]
    ctx = make_context(impossible)
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("inconsistent_pricing" in note for note in ctx.notes)


def sparse_cheap_chain():
    """A cheap underlying's strikes are spaced $5 apart -- the nearest listed protective strike
    can land far wider than the target, and the width cap has to catch that regardless of what
    the target was aiming for."""
    return [
        put_quote(30, delta="-0.30", price="1.20"),
        put_quote(25, delta="-0.20", price="0.80"),
    ]


def test_rejects_a_width_too_wide_for_the_underlying():
    """The target width is tiny (0.4% of $58 rounds to $1), but the nearest real strike on this
    sparsely-listed cheap name is $5 away -- 8.6% of spot, well past the 2% cap. Max loss is
    still bounded, so no risk limit catches it; the strategy has to notice that the structure
    the chain handed back is not the one it means to place."""
    ctx = make_context(sparse_cheap_chain(), spot=Decimal("58"))
    assert PutCreditSpread(ctx.params).propose(ctx) == []
    assert any("width_unsuitable_for_underlying" in note for note in ctx.notes)


def test_accepts_the_same_width_on_a_high_priced_underlying():
    """The mirror of the test above: the widest strike this chain offers below the short (437,
    2 points down) is only 0.26% of a $772 underlying, well inside the cap."""
    ctx = make_context(default_put_chain(), spot=Decimal("772"))
    assert len(PutCreditSpread(ctx.params).propose(ctx)) == 1


def test_finds_a_suitable_width_on_a_cheap_underlying():
    """The whole point of the rebuild: this same chain, at this same cheap spot, would have
    failed under the old fixed 3-point width (5% of $58, past the cap). Dynamic width instead
    targets $1 here and finds a listed strike exactly there, so the trade goes through."""
    ctx = make_context(default_put_chain(), spot=Decimal("58"))
    intent = PutCreditSpread(ctx.params).propose(ctx)[0]
    assert intent.spread.legs[1].contract.strike == Decimal("438")
    assert intent.spread.width == Decimal("1")


# ---- iron condor -------------------------------------------------------------------------------------


def condor_chain():
    return [
        put_quote(435, delta="-0.16", price="2.10"),
        put_quote(434, delta="-0.15", price="1.90"),
        call_quote(465, delta="0.16", price="2.00"),
        call_quote(466, delta="0.15", price="1.80"),
    ]


def condor_params(**kw):
    base = dict(enabled=True, short_delta_target=Decimal("0.16"), min_iv_rank=Decimal("50"))
    base.update(kw)
    return StrategyParams(**base)


def test_iron_condor_builds_four_legs_in_the_right_order_and_direction():
    params = condor_params()
    ctx = make_context(condor_chain(), params=params)
    intents = IronCondor(params).propose(ctx)

    assert len(intents) == 1
    legs = intents[0].spread.legs
    assert [leg.action for leg in legs] == [
        Action.SELL_TO_OPEN,
        Action.BUY_TO_OPEN,
        Action.SELL_TO_OPEN,
        Action.BUY_TO_OPEN,
    ]
    short_put, long_put, short_call, long_call = (leg.contract for leg in legs)
    # the protective wings sit further out than the shorts, in opposite directions
    assert long_put.strike < short_put.strike < short_call.strike < long_call.strike


def test_iron_condor_max_loss_uses_the_widest_wing_not_the_sum():
    """Only one side of a condor can be breached at expiration. Summing both sides would halve
    the size the risk manager allows for no reason."""
    params = condor_params()
    ctx = make_context(condor_chain(), params=params)
    intent = IronCondor(params).propose(ctx)[0]

    # credit = (2.10 - 1.90) + (2.00 - 1.80) = 0.40, widest wing = 1.00
    assert intent.limit_price_per_share == Decimal("-0.40")
    assert intent.max_profit_per_contract == Decimal("40.00")
    assert intent.max_loss_per_contract == Decimal("60.00")


def test_iron_condor_is_disabled_by_default():
    config = StrategiesConfig()
    assert config.iron_condor.enabled is False


def test_iron_condor_rejects_an_inverted_structure():
    """A 'condor' whose short put sits above its short call is not a condor; the risk
    arithmetic would be meaningless."""
    # Deltas are chosen so each wing's short leg is unambiguous and its protective strike IS
    # listed -- otherwise the proposal fails earlier on a missing strike and never reaches the
    # inversion check this test exists to exercise.
    params = condor_params(short_delta_target=Decimal("0.50"), short_delta_tolerance=Decimal("0.50"))
    crossed = [
        put_quote(470, delta="-0.50", price="22.00"),  # short put, deep ITM
        put_quote(469, delta="-0.40", price="21.30"),  # its protective wing, one below
        call_quote(430, delta="0.50", price="21.00"),  # short call, far BELOW the short put
        call_quote(431, delta="0.62", price="20.30"),  # its protective wing, one above
    ]
    ctx = make_context(crossed, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("inverted_condor" in note for note in ctx.notes)


def test_iron_condor_rejects_when_a_wing_has_no_strike_at_the_target_delta():
    params = condor_params()
    put_only = [put_quote(435, delta="-0.16", price="2.10"), put_quote(434, delta="-0.15", price="1.90")]
    ctx = make_context(put_only, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("no_delta_data" in note or "near_target_delta" in note for note in ctx.notes)


def test_iron_condor_is_skipped_when_disabled():
    params = condor_params(enabled=False)
    ctx = make_context(condor_chain(), params=params)
    assert IronCondor(params).propose(ctx) == []
    assert ctx.notes == []  # a disabled strategy is silent, not a rejection worth journalling


def test_iron_condor_rejects_and_notes_low_iv_rank():
    """A condor needs both wings paid for, so it carries a higher IVR floor than a put spread."""
    params = condor_params()
    ctx = make_context(condor_chain(), params=params, iv_rank="40")
    assert IronCondor(params).propose(ctx) == []
    assert any("iv_rank_too_low" in note for note in ctx.notes)


def test_iron_condor_rejects_and_notes_an_earnings_blackout():
    params = condor_params()
    ctx = make_context(condor_chain(), params=params, earnings=[date(2026, 8, 15)])
    assert IronCondor(params).propose(ctx) == []
    assert any("earnings_blackout" in note for note in ctx.notes)


def test_iron_condor_rejects_when_no_expiration_is_in_the_dte_window():
    params = condor_params()
    near = date(2026, 8, 21)
    quotes = [
        put_quote(435, delta="-0.16", price="2.10", expiration=near),
        put_quote(434, delta="-0.15", price="1.90", expiration=near),
        call_quote(465, delta="0.16", price="2.00", expiration=near),
        call_quote(466, delta="0.15", price="1.80", expiration=near),
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("no_expiration_in_window" in note for note in ctx.notes)


def test_iron_condor_rejects_when_a_wing_has_no_strike_near_the_target_delta():
    params = condor_params()
    quotes = [
        put_quote(435, delta="-0.16", price="2.10"),
        put_quote(434, delta="-0.15", price="1.90"),
        call_quote(500, delta="0.01", price="0.05"),  # nothing near 16 delta on the call side
        call_quote(501, delta="0.01", price="0.04"),
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("near_target_delta" in note for note in ctx.notes)


def test_iron_condor_rejects_when_a_protective_wing_is_not_listed():
    """The call wing's protective strike sits ABOVE its short -- the one place a condor differs
    structurally from two copies of the same wing code, so it gets its own test."""
    params = condor_params()
    quotes = [
        put_quote(435, delta="-0.16", price="2.10"),
        put_quote(434, delta="-0.15", price="1.90"),
        call_quote(465, delta="0.16", price="2.00"),  # no 466 listed above it
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("long_strike_not_listed" in note for note in ctx.notes)


def test_iron_condor_rejects_an_illiquid_leg():
    params = condor_params()
    quotes = [
        put_quote(435, delta="-0.16", price="2.10"),
        put_quote(434, delta="-0.15", price="1.90"),
        call_quote(465, delta="0.16", price="2.00"),
        call_quote(466, delta="0.15", price="1.80", oi=2),
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("illiquid_open_interest" in note for note in ctx.notes)


def test_iron_condor_rejects_an_unpriceable_leg():
    params = condor_params()
    unpriced = OptionQuote(
        contract=OptionContract(underlying="SPY", expiration=EXP, strike=Decimal("466"), right=Right.CALL),
        open_interest=5000,
        delta=Decimal("0.15"),
    )
    quotes = [
        put_quote(435, delta="-0.16", price="2.10"),
        put_quote(434, delta="-0.15", price="1.90"),
        call_quote(465, delta="0.16", price="2.00"),
        unpriced,
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("unpriceable" in note for note in ctx.notes)


def test_iron_condor_rejects_when_the_structure_prices_as_a_debit():
    params = condor_params()
    quotes = [
        put_quote(435, delta="-0.16", price="1.90"),  # protective wing costs more than the short
        put_quote(434, delta="-0.15", price="2.10"),
        call_quote(465, delta="0.16", price="1.80"),
        call_quote(466, delta="0.15", price="2.00"),
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("not_a_credit" in note for note in ctx.notes)


def test_iron_condor_rejects_a_credit_too_small_for_the_risk():
    params = condor_params()
    quotes = [
        put_quote(435, delta="-0.16", price="2.05"),
        put_quote(434, delta="-0.15", price="2.00"),
        call_quote(465, delta="0.16", price="2.05"),
        call_quote(466, delta="0.15", price="2.00"),
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("credit_too_small" in note for note in ctx.notes)


def test_iron_condor_rejects_an_impossible_credit():
    """Credit exceeding the widest wing would produce a negative max loss that the risk manager
    would happily size against."""
    params = condor_params()
    quotes = [
        put_quote(435, delta="-0.16", price="3.00"),
        put_quote(434, delta="-0.15", price="1.90"),
        call_quote(465, delta="0.16", price="3.00"),
        call_quote(466, delta="0.15", price="1.80"),
    ]
    ctx = make_context(quotes, params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("inconsistent_pricing" in note for note in ctx.notes)


def test_iron_condor_rejects_a_put_wing_priced_below_zero_strike():
    # Width guard relaxed for the same reason as the put-spread equivalent above.
    params = condor_params(spread_width=Decimal("500"), max_spread_width_pct_of_spot=Decimal("10"))
    ctx = make_context(condor_chain(), params=params)
    assert IronCondor(params).propose(ctx) == []
    assert any("invalid_long_strike" in note for note in ctx.notes)


@pytest.mark.parametrize(
    "put_width,call_width,credit,expected_loss,expected_profit",
    [
        ("1", "1", "0.40", "60", "40"),
        ("2", "1", "0.40", "160", "40"),  # widest wing governs
        ("1", "5", "1.00", "400", "100"),
    ],
)
def test_iron_condor_risk_arithmetic(put_width, call_width, credit, expected_loss, expected_profit):
    max_loss, max_profit = iron_condor_risk(
        put_width=Decimal(put_width),
        call_width=Decimal(call_width),
        credit_per_share=Decimal(credit),
        multiplier=100,
    )
    assert max_loss == Decimal(expected_loss)
    assert max_profit == Decimal(expected_profit)


def test_iron_condor_risk_rejects_a_debit():
    with pytest.raises(ValueError):
        iron_condor_risk(put_width=Decimal("1"), call_width=Decimal("1"), credit_per_share=Decimal("-1"), multiplier=100)


def test_iron_condor_risk_rejects_impossible_credit():
    with pytest.raises(ValueError):
        iron_condor_risk(put_width=Decimal("1"), call_width=Decimal("1"), credit_per_share=Decimal("2"), multiplier=100)


# ---- registry -------------------------------------------------------------------------------------------


def test_registry_builds_only_enabled_strategies():
    config = StrategiesConfig()  # put credit spread on, condor and wheel off
    strategies = build_enabled_strategies(config)
    assert [s.name for s in strategies] == [StrategyName.PUT_CREDIT_SPREAD]


def test_registry_builds_both_when_both_are_enabled():
    config = StrategiesConfig(iron_condor=condor_params())
    assert len(build_enabled_strategies(config)) == 2


def test_registry_skips_strategies_declared_in_config_but_not_implemented():
    """The wheel is configurable but unimplemented. An unimplemented strategy left enabled in
    config must not stop the bot from starting."""
    config = StrategiesConfig(wheel=StrategyParams(enabled=True))
    assert StrategyName.WHEEL not in {s.name for s in build_enabled_strategies(config)}


def test_params_for_matches_each_registered_strategy():
    config = StrategiesConfig()
    for name in STRATEGY_CLASSES:
        assert isinstance(params_for(config, name), StrategyParams)


def test_build_strategy_raises_on_an_unregistered_name():
    with pytest.raises(KeyError):
        build_strategy(StrategyName.COVERED_CALL, StrategiesConfig())


def test_params_for_raises_loudly_when_config_has_no_matching_field():
    """StrategiesConfig names its fields after the enum values. If that ever stops holding, this
    must fail rather than silently hand back defaults and disable a strategy's tuning."""
    with pytest.raises(KeyError, match="covered_call"):
        params_for(StrategiesConfig(), StrategyName.COVERED_CALL)
