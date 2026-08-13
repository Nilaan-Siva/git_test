"""Tests for core/models.py, with extra scrutiny on the sign convention and defined_risk math
since these are the calculations a subtle bug here costs real money."""
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from optionsbot.core.enums import Action, PositionStatus, Right, StrategyName
from optionsbot.core.models import Chain, Leg, OptionContract, OptionQuote, OrderIntent, Position, Spread


def make_put(strike: str, expiration: date = date(2026, 9, 18)) -> OptionContract:
    return OptionContract(underlying="SPY", expiration=expiration, strike=Decimal(strike), right=Right.PUT)


def make_vertical(short_strike: str, long_strike: str) -> Spread:
    """A put credit spread: sell the higher strike, buy the lower strike (protection)."""
    return Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(contract=make_put(short_strike), action=Action.SELL_TO_OPEN),
            Leg(contract=make_put(long_strike), action=Action.BUY_TO_OPEN),
        ],
    )


# ---- OptionContract ----------------------------------------------------------------------


def test_contract_dte():
    c = make_put("450")
    assert c.dte(date(2026, 8, 19)) == 30


def test_contract_rejects_non_positive_strike():
    with pytest.raises(ValidationError):
        OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("0"), right=Right.PUT)


def test_contract_normalizes_underlying_case():
    c = OptionContract(underlying="spy", expiration=date(2026, 9, 18), strike=Decimal("450"), right=Right.CALL)
    assert c.underlying == "SPY"


# ---- Spread / width / defined_risk --------------------------------------------------------


def test_vertical_width():
    spread = make_vertical(short_strike="450", long_strike="445")
    assert spread.width == Decimal("5")


def test_spread_rejects_mismatched_underlying_leg():
    with pytest.raises(ValidationError):
        Spread(
            strategy=StrategyName.PUT_CREDIT_SPREAD,
            underlying="SPY",
            legs=[
                Leg(contract=make_put("450"), action=Action.SELL_TO_OPEN),
                Leg(
                    contract=OptionContract(underlying="QQQ", expiration=date(2026, 9, 18), strike=Decimal("445"), right=Right.PUT),
                    action=Action.BUY_TO_OPEN,
                ),
            ],
        )


def test_defined_risk_credit_spread():
    spread = make_vertical(short_strike="450", long_strike="445")
    # sold for a $1.50 credit -> entry_price_per_share is negative under our convention
    max_loss, max_profit = spread.defined_risk(Decimal("-1.50"))
    assert max_profit == Decimal("150.00")
    assert max_loss == Decimal("350.00")


def test_defined_risk_debit_spread():
    call_spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(contract=OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("450"), right=Right.CALL), action=Action.BUY_TO_OPEN),
            Leg(contract=OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("455"), right=Right.CALL), action=Action.SELL_TO_OPEN),
        ],
    )
    max_loss, max_profit = call_spread.defined_risk(Decimal("2.00"))
    assert max_loss == Decimal("200.00")
    assert max_profit == Decimal("300.00")


def test_defined_risk_rejects_credit_exceeding_width():
    spread = make_vertical(short_strike="450", long_strike="445")
    with pytest.raises(ValueError, match="cannot exceed spread width"):
        spread.defined_risk(Decimal("-6.00"))


def test_defined_risk_raises_for_non_vertical():
    single_leg = Spread(
        strategy=StrategyName.WHEEL,
        underlying="SPY",
        legs=[Leg(contract=make_put("450"), action=Action.SELL_TO_OPEN)],
    )
    with pytest.raises(NotImplementedError):
        single_leg.defined_risk(Decimal("-1.50"))


def test_closing_legs_mirror_actions():
    spread = make_vertical(short_strike="450", long_strike="445")
    closing = spread.closing_legs()
    assert closing[0].action == Action.BUY_TO_CLOSE  # was SELL_TO_OPEN
    assert closing[1].action == Action.SELL_TO_CLOSE  # was BUY_TO_OPEN


# ---- Position P&L: the sign-convention symmetry test --------------------------------------


def make_position(entry_price: str, max_loss: str = "350.00", max_profit: str = "150.00") -> Position:
    spread = make_vertical(short_strike="450", long_strike="445")
    return Position(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=spread,
        quantity=2,
        entry_price_per_share=Decimal(entry_price),
        entry_date=date(2026, 8, 1),
        max_loss_per_contract=Decimal(max_loss),
        max_profit_per_contract=Decimal(max_profit),
    )


def test_unrealized_pnl_credit_spread_profit():
    # opened for $1.50 credit (entry = -1.50), now costs $0.75 to close -> $0.75/share profit
    pos = make_position(entry_price="-1.50")
    pnl = pos.unrealized_pnl(close_price_per_share=Decimal("0.75"))
    assert pnl == Decimal("150.00")  # 0.75 * 100 * 2 contracts


def test_unrealized_pnl_credit_spread_loss():
    # opened for $1.50 credit, now costs $3.50 to close (spread went against us) -> loss
    pos = make_position(entry_price="-1.50")
    pnl = pos.unrealized_pnl(close_price_per_share=Decimal("3.50"))
    assert pnl == Decimal("-400.00")  # -(−1.50+3.50) * 100 * 2 = -2.00*200


def test_unrealized_pnl_debit_spread_profit():
    # opened for $2.00 debit (entry = +2.00), now worth $2.50 to close (received as credit) -> profit
    pos = make_position(entry_price="2.00", max_loss="200.00", max_profit="300.00")
    pnl = pos.unrealized_pnl(close_price_per_share=Decimal("-2.50"))
    assert pnl == Decimal("100.00")  # -(2.00 + -2.50) * 100 * 2 = 0.50*200


def test_close_sets_realized_pnl_and_status():
    pos = make_position(entry_price="-1.50")
    pos.close(exit_price_per_share=Decimal("0.75"), exit_date=date(2026, 8, 20), reason="profit_target")
    assert pos.status == PositionStatus.CLOSED
    assert pos.realized_pnl == Decimal("150.00")
    assert pos.close_reason == "profit_target"


def test_position_dte_uses_nearest_leg():
    pos = make_position(entry_price="-1.50")
    assert pos.dte(date(2026, 8, 19)) == 30


def test_total_max_loss_scales_with_quantity():
    pos = make_position(entry_price="-1.50")
    assert pos.total_max_loss == Decimal("700.00")  # 350 * 2 contracts


# ---- OrderIntent ---------------------------------------------------------------------------


# ---- OptionQuote / Chain --------------------------------------------------------------------


def make_quote(strike: str, right: Right, delta: str, bid: str | None = "1.00", ask: str | None = "1.10") -> OptionQuote:
    contract = OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal(strike), right=right)
    return OptionQuote(
        contract=contract,
        bid=Decimal(bid) if bid else None,
        ask=Decimal(ask) if ask else None,
        delta=Decimal(delta),
        open_interest=500,
    )


def test_quote_mid_and_spread_pct():
    q = make_quote("450", Right.PUT, delta="-0.30", bid="1.00", ask="1.20")
    assert q.mid == Decimal("1.10")
    assert q.spread_pct_of_mid == pytest.approx(Decimal("0.181818"), abs=Decimal("0.0001"))


def test_quote_mid_is_none_without_both_sides():
    q = make_quote("450", Right.PUT, delta="-0.30", bid=None, ask="1.20")
    assert q.mid is None
    assert q.spread_pct_of_mid is None


def test_chain_expirations_and_filter():
    exp = date(2026, 9, 18)
    chain = Chain(
        underlying="SPY",
        as_of=date(2026, 8, 19),
        underlying_price=Decimal("450"),
        quotes=[
            make_quote("440", Right.PUT, delta="-0.15"),
            make_quote("445", Right.PUT, delta="-0.30"),
            make_quote("455", Right.CALL, delta="0.30"),
        ],
    )
    assert chain.expirations() == [exp]
    puts = chain.filter(right=Right.PUT)
    assert len(puts) == 2
    assert all(q.contract.right == Right.PUT for q in puts)


def test_chain_nearest_to_delta_picks_closest_match():
    chain = Chain(
        underlying="SPY",
        as_of=date(2026, 8, 19),
        underlying_price=Decimal("450"),
        quotes=[
            make_quote("440", Right.PUT, delta="-0.15"),
            make_quote("445", Right.PUT, delta="-0.32"),
            make_quote("435", Right.PUT, delta="-0.10"),
        ],
    )
    nearest = chain.nearest_to_delta(Decimal("0.30"), expiration=date(2026, 9, 18), right=Right.PUT)
    assert nearest.contract.strike == Decimal("445")


def test_chain_nearest_to_delta_none_when_no_candidates():
    chain = Chain(underlying="SPY", as_of=date(2026, 8, 19), underlying_price=Decimal("450"), quotes=[])
    assert chain.nearest_to_delta(Decimal("0.30"), expiration=date(2026, 9, 18), right=Right.PUT) is None


def test_order_intent_rejects_negative_max_loss():
    spread = make_vertical(short_strike="450", long_strike="445")
    with pytest.raises(ValidationError):
        OrderIntent(
            strategy=StrategyName.PUT_CREDIT_SPREAD,
            spread=spread,
            limit_price_per_share=Decimal("-1.50"),
            max_loss_per_contract=Decimal("-1"),
            max_profit_per_contract=Decimal("150"),
        )
