"""Tests for risk/exposure.py -- portfolio heat and position lookups."""
from datetime import date
from decimal import Decimal

from optionsbot.core.enums import Action, StrategyName, Right
from optionsbot.core.models import Leg, OptionContract, Position, Spread
from optionsbot.risk.exposure import (
    heat_pct_of_equity,
    positions_for_underlying,
    positions_in_bucket,
    total_heat,
)


def make_position(underlying: str, max_loss: str, quantity: int = 1) -> Position:
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying=underlying,
        legs=[
            Leg(
                contract=OptionContract(underlying=underlying, expiration=date(2026, 9, 18), strike=Decimal("450"), right=Right.PUT),
                action=Action.SELL_TO_OPEN,
            ),
            Leg(
                contract=OptionContract(underlying=underlying, expiration=date(2026, 9, 18), strike=Decimal("445"), right=Right.PUT),
                action=Action.BUY_TO_OPEN,
            ),
        ],
    )
    return Position(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=spread,
        quantity=quantity,
        entry_price_per_share=Decimal("-1.50"),
        entry_date=date(2026, 8, 1),
        max_loss_per_contract=Decimal(max_loss),
        max_profit_per_contract=Decimal("150"),
    )


def test_total_heat_sums_across_positions_and_quantity():
    positions = [make_position("SPY", "350", quantity=2), make_position("QQQ", "200", quantity=1)]
    assert total_heat(positions) == Decimal("900")  # 350*2 + 200*1


def test_total_heat_empty_is_zero():
    assert total_heat([]) == Decimal("0")


def test_heat_pct_of_equity():
    positions = [make_position("SPY", "500")]
    assert heat_pct_of_equity(positions, Decimal("10000")) == Decimal("0.05")


def test_heat_pct_of_equity_zero_equity_returns_zero():
    positions = [make_position("SPY", "500")]
    assert heat_pct_of_equity(positions, Decimal("0")) == Decimal("0")


def test_positions_for_underlying_filters_correctly():
    positions = [make_position("SPY", "350"), make_position("QQQ", "200"), make_position("SPY", "300")]
    spy_positions = positions_for_underlying(positions, "spy")  # lowercase input
    assert len(spy_positions) == 2
    assert all(p.spread.underlying == "SPY" for p in spy_positions)


def test_positions_in_bucket_filters_across_multiple_underlyings():
    positions = [make_position("SPY", "350"), make_position("QQQ", "200"), make_position("AAPL", "100")]
    bucket_positions = positions_in_bucket(positions, ["SPY", "XSP", "QQQ", "IWM"])
    assert len(bucket_positions) == 2
    assert {p.spread.underlying for p in bucket_positions} == {"SPY", "QQQ"}


def test_positions_in_bucket_empty_when_no_match():
    positions = [make_position("AAPL", "100")]
    assert positions_in_bucket(positions, ["SPY", "QQQ"]) == []
