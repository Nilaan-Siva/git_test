"""Tests for strategy/exits.py -- the profit target, stop loss, and time stop.

These rules decide when money is actually taken off the table, so the tests pin down not just
that each rule fires, but the *order* they fire in when more than one is true at once. Daily-bar
data can't say which was touched first intraday, and the choice to assume the stop is what keeps
the backtest from flattering itself.
"""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.config.schema import RiskConfig
from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Leg, OptionContract, Position, Spread
from optionsbot.strategy.exits import ExitSignal, evaluate_exit, reference_premium_per_share

RISK = RiskConfig()
TODAY = date(2026, 8, 13)
FAR_EXPIRY = date(2026, 10, 16)  # 64 DTE from TODAY -- clear of the 21-DTE time stop


def make_position(
    *, entry_price: str = "-1.00", expiration: date = FAR_EXPIRY, quantity: int = 1
) -> Position:
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(
                contract=OptionContract(underlying="SPY", expiration=expiration, strike=Decimal("450"), right=Right.PUT),
                action=Action.SELL_TO_OPEN,
            ),
            Leg(
                contract=OptionContract(underlying="SPY", expiration=expiration, strike=Decimal("445"), right=Right.PUT),
                action=Action.BUY_TO_OPEN,
            ),
        ],
    )
    entry = Decimal(entry_price)
    max_loss, max_profit = spread.defined_risk(entry)
    return Position(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=spread,
        quantity=quantity,
        entry_price_per_share=entry,
        entry_date=date(2026, 8, 1),
        max_loss_per_contract=max_loss,
        max_profit_per_contract=max_profit,
    )


def exit_at(close_price: str, *, position=None, as_of: date = TODAY):
    position = position or make_position()
    return evaluate_exit(position, close_price_per_share=Decimal(close_price), as_of=as_of, risk=RISK)


# ---- hold -------------------------------------------------------------------------------------


def test_holds_when_nothing_has_triggered():
    # Entered for a 1.00 credit, now costs 0.70 to close: a 0.30 profit, short of the 0.50 target.
    assert exit_at("0.70") is None


# ---- profit target -----------------------------------------------------------------------------


def test_closes_at_the_profit_target():
    """Entered for 1.00 credit, buy it back for 0.50 -> exactly 50% of the credit captured."""
    assert exit_at("0.50") == ExitSignal(reason="profit_target")


def test_profit_target_is_inclusive_at_the_boundary():
    assert exit_at("0.50").reason == "profit_target"
    assert exit_at("0.51") is None  # a cent short


def test_profit_target_scales_with_the_credit_received():
    fat = make_position(entry_price="-2.00")
    assert evaluate_exit(fat, close_price_per_share=Decimal("1.20"), as_of=TODAY, risk=RISK) is None
    assert evaluate_exit(fat, close_price_per_share=Decimal("1.00"), as_of=TODAY, risk=RISK).reason == "profit_target"


# ---- stop loss ----------------------------------------------------------------------------------


def test_closes_at_the_stop_loss():
    """Entered for 1.00 credit; a 2x-credit loss means it now costs 3.00 to close."""
    assert exit_at("3.00") == ExitSignal(reason="stop_loss")


def test_stop_loss_is_inclusive_at_the_boundary():
    assert exit_at("3.00").reason == "stop_loss"
    assert exit_at("2.99") is None


def test_stop_loss_fires_before_the_time_stop_when_both_are_true():
    near = make_position(expiration=date(2026, 8, 28))  # 15 DTE -- inside the 21-DTE time stop
    assert evaluate_exit(near, close_price_per_share=Decimal("3.00"), as_of=TODAY, risk=RISK).reason == "stop_loss"


def test_stop_loss_fires_before_the_profit_target_is_never_possible():
    """Sanity: the two are mutually exclusive by construction, since one needs a profit and the
    other a loss. This pins that down so a future refactor can't make both reachable."""
    for price in ("0.00", "0.50", "1.00", "2.00", "3.00", "4.00"):
        signal = exit_at(price)
        assert signal is None or signal.reason in ("profit_target", "stop_loss", "time_stop")


# ---- time stop -------------------------------------------------------------------------------------


def test_closes_at_the_time_stop_regardless_of_pnl():
    """21 DTE with the trade going nowhere: gamma risk accelerates from here, so it closes."""
    near = make_position(expiration=date(2026, 9, 3))  # 21 DTE
    assert evaluate_exit(near, close_price_per_share=Decimal("0.90"), as_of=TODAY, risk=RISK).reason == "time_stop"


def test_time_stop_does_not_fire_one_day_early():
    just_outside = make_position(expiration=date(2026, 9, 4))  # 22 DTE
    assert evaluate_exit(just_outside, close_price_per_share=Decimal("0.90"), as_of=TODAY, risk=RISK) is None


def test_profit_target_beats_the_time_stop_when_both_are_true():
    """Both are good outcomes, but the reason recorded should be the one that earned the exit."""
    near = make_position(expiration=date(2026, 9, 3))  # 21 DTE
    assert evaluate_exit(near, close_price_per_share=Decimal("0.40"), as_of=TODAY, risk=RISK).reason == "profit_target"


# ---- expiration ----------------------------------------------------------------------------------


def test_expiration_overrides_everything():
    expired = make_position(expiration=TODAY)
    for price in ("0.00", "3.00", "5.00"):
        assert evaluate_exit(expired, close_price_per_share=Decimal(price), as_of=TODAY, risk=RISK).reason == "expiration"


def test_a_position_past_its_expiration_still_reports_expiration():
    """A data gap can mean the engine only sees the position again after expiry day."""
    expired = make_position(expiration=date(2026, 8, 7))
    assert evaluate_exit(expired, close_price_per_share=Decimal("0"), as_of=TODAY, risk=RISK).reason == "expiration"


# ---- debit positions -------------------------------------------------------------------------------


def test_debit_position_takes_profit_at_the_configured_fraction_of_the_debit():
    debit = make_position(entry_price="2.00")  # paid 2.00
    # Now worth 3.00 to close (sell it back) -> the closing cash flow is -3.00, a 1.00 profit.
    assert evaluate_exit(debit, close_price_per_share=Decimal("-3.00"), as_of=TODAY, risk=RISK).reason == "profit_target"


def test_debit_position_stop_is_capped_at_the_debit_paid():
    """A debit spread cannot lose more than it cost, so a 2x-credit stop would be unreachable
    and the position would ride to expiry with no stop at all. It caps at a total loss instead.
    """
    debit = make_position(entry_price="2.00")
    assert evaluate_exit(debit, close_price_per_share=Decimal("0.00"), as_of=TODAY, risk=RISK).reason == "stop_loss"


# ---- degenerate input --------------------------------------------------------------------------------


def test_zero_premium_position_falls_through_to_the_time_stop_only():
    """A position opened at zero cost has no reference to measure a target or stop against;
    it must not divide by zero, and the time stop still governs it."""
    free = make_position(entry_price="0.00")
    assert reference_premium_per_share(free) == 0
    assert evaluate_exit(free, close_price_per_share=Decimal("1.00"), as_of=TODAY, risk=RISK) is None
    near_free = make_position(entry_price="0.00", expiration=date(2026, 9, 3))
    assert evaluate_exit(near_free, close_price_per_share=Decimal("1.00"), as_of=TODAY, risk=RISK).reason == "time_stop"


@pytest.mark.parametrize("entry,expected", [("-1.50", "1.50"), ("2.25", "2.25"), ("0", "0")])
def test_reference_premium_is_always_positive(entry, expected):
    position = make_position(entry_price=entry)
    assert reference_premium_per_share(position) == Decimal(expected)
