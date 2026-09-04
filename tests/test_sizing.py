"""Tests for risk/sizing.py -- the ONLY place position size is computed."""
from decimal import Decimal

import pytest

from optionsbot.risk.sizing import clamp_to_heat_room, size_by_risk_pct


def test_size_by_risk_pct_basic():
    # $10,000 equity, 1% risk = $100 budget, $50/contract max loss -> 2 contracts
    assert size_by_risk_pct(Decimal("10000"), Decimal("0.01"), Decimal("50")) == 2


def test_size_by_risk_pct_floors_down_never_rounds_up():
    # $100 budget / $60 per contract = 1.66 -> floors to 1, never 2
    assert size_by_risk_pct(Decimal("10000"), Decimal("0.01"), Decimal("60")) == 1


def test_size_by_risk_pct_small_account_correctly_refuses_to_trade():
    # the exact example from the project plan: $2,000 account, $350 max loss per contract,
    # 1% budget = $20 -- not enough for even one contract. Must return 0, not round up.
    assert size_by_risk_pct(Decimal("2000"), Decimal("0.01"), Decimal("350")) == 0


def test_size_by_risk_pct_zero_equity_returns_zero():
    assert size_by_risk_pct(Decimal("0"), Decimal("0.01"), Decimal("50")) == 0


def test_size_by_risk_pct_negative_equity_returns_zero():
    assert size_by_risk_pct(Decimal("-500"), Decimal("0.01"), Decimal("50")) == 0


def test_size_by_risk_pct_zero_risk_pct_returns_zero():
    assert size_by_risk_pct(Decimal("10000"), Decimal("0"), Decimal("50")) == 0


def test_size_by_risk_pct_rejects_non_positive_max_loss():
    with pytest.raises(ValueError):
        size_by_risk_pct(Decimal("10000"), Decimal("0.01"), Decimal("0"))
    with pytest.raises(ValueError):
        size_by_risk_pct(Decimal("10000"), Decimal("0.01"), Decimal("-10"))


def test_size_by_risk_pct_huge_max_loss_vs_small_account():
    assert size_by_risk_pct(Decimal("500"), Decimal("0.01"), Decimal("100000")) == 0


def test_clamp_to_heat_room_reduces_when_room_is_tight():
    # wants 2 contracts at $100 max loss each ($200 total), but only $150 of heat room left
    assert clamp_to_heat_room(2, Decimal("100"), Decimal("150")) == 1


def test_clamp_to_heat_room_unchanged_when_room_is_ample():
    assert clamp_to_heat_room(2, Decimal("100"), Decimal("10000")) == 2


def test_clamp_to_heat_room_zero_room_returns_zero():
    assert clamp_to_heat_room(2, Decimal("100"), Decimal("0")) == 0


def test_clamp_to_heat_room_negative_room_returns_zero():
    assert clamp_to_heat_room(2, Decimal("100"), Decimal("-50")) == 0


def test_clamp_to_heat_room_zero_quantity_returns_zero():
    assert clamp_to_heat_room(0, Decimal("100"), Decimal("1000")) == 0


def test_clamp_to_heat_room_never_exceeds_input_quantity():
    # room would allow 100 contracts, but only 1 was requested
    assert clamp_to_heat_room(1, Decimal("100"), Decimal("10000")) == 1
