"""Tests for backtest/metrics.py.

The recurring theme: a metric that cannot be computed returns None, never 0.0. A zero Sharpe or
a zero profit factor reads as a real, poor result; None reads as "not enough data", and the
difference decides whether a strategy gets rejected or gets more history.
"""
from datetime import date, timedelta
from decimal import Decimal

from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import Leg, OptionContract, Position, Spread
from optionsbot.backtest.metrics import (
    cagr_pct,
    compute_metrics,
    daily_returns,
    max_drawdown_pct,
    sharpe_ratio,
    sortino_ratio,
)

START = date(2026, 1, 5)


def curve(*values: str, start: date = START) -> list[tuple[date, Decimal]]:
    return [(start + timedelta(days=i), Decimal(v)) for i, v in enumerate(values)]


def closed_position(pnl: str) -> Position:
    spread = Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying="SPY",
        legs=[
            Leg(
                contract=OptionContract(
                    underlying="SPY", expiration=date(2026, 3, 20), strike=Decimal("450"), right=Right.PUT
                ),
                action=Action.SELL_TO_OPEN,
            ),
            Leg(
                contract=OptionContract(
                    underlying="SPY", expiration=date(2026, 3, 20), strike=Decimal("449"), right=Right.PUT
                ),
                action=Action.BUY_TO_OPEN,
            ),
        ],
    )
    position = Position(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=spread,
        quantity=1,
        entry_price_per_share=Decimal("-0.30"),
        entry_date=START,
        max_loss_per_contract=Decimal("70"),
        max_profit_per_contract=Decimal("30"),
    )
    position.close(Decimal("0.10"), date(2026, 2, 1), "profit_target")
    position.realized_pnl = Decimal(pnl)
    return position


# ---- returns and drawdown ----------------------------------------------------------------------


def test_daily_returns():
    assert daily_returns(curve("100", "110", "99")) == [0.1, -0.1]


def test_daily_returns_skips_a_zero_denominator_instead_of_exploding():
    assert daily_returns(curve("0", "100")) == []


def test_daily_returns_of_a_single_point_is_empty():
    assert daily_returns(curve("100")) == []


def test_max_drawdown_measures_peak_to_trough():
    assert max_drawdown_pct(curve("100", "120", "90", "150")) == Decimal("25")


def test_max_drawdown_of_a_monotonic_rise_is_zero():
    assert max_drawdown_pct(curve("100", "110", "120")) == Decimal("0")


def test_max_drawdown_of_an_empty_curve_is_none():
    assert max_drawdown_pct([]) is None


# ---- risk-adjusted ratios ------------------------------------------------------------------------


def test_sharpe_needs_at_least_two_returns():
    assert sharpe_ratio([]) is None
    assert sharpe_ratio([0.01]) is None


def test_sharpe_is_none_with_no_variance():
    """A flat return series has no risk to divide by. Returning 0.0 would read as a real,
    mediocre Sharpe rather than as an undefined one."""
    assert sharpe_ratio([0.01, 0.01, 0.01]) is None


def test_sharpe_is_positive_for_a_rising_series_and_negative_for_a_falling_one():
    rising = sharpe_ratio([0.01, 0.02, 0.005, 0.015])
    falling = sharpe_ratio([-0.01, -0.02, -0.005, -0.015])
    assert rising is not None and rising > 0
    assert falling is not None and falling < 0


def test_sortino_is_none_when_there_is_no_downside():
    """Nothing to penalise means the ratio is undefined, not infinite and not zero."""
    assert sortino_ratio([0.01, 0.02, 0.03]) is None


def test_sortino_exceeds_sharpe_when_losses_are_smaller_than_gains():
    """The reason Sortino is the honest measure for premium selling: Sharpe punishes the upside
    variance a credit strategy structurally cannot have."""
    returns = [0.03, -0.005, 0.04, -0.004, 0.05]
    assert sortino_ratio(returns) > sharpe_ratio(returns)


def test_sortino_needs_at_least_two_returns():
    assert sortino_ratio([-0.01]) is None


# ---- CAGR ------------------------------------------------------------------------------------------


def test_cagr_of_a_doubling_over_one_year():
    two_points = [(date(2025, 1, 1), Decimal("100")), (date(2026, 1, 1), Decimal("200"))]
    result = cagr_pct(two_points)
    assert result is not None and 99 < result < 101


def test_cagr_is_none_without_two_points():
    assert cagr_pct(curve("100")) is None


def test_cagr_is_none_when_equity_hits_zero():
    """A wiped-out account has no meaningful growth rate -- and a fractional power of zero
    would raise rather than return a number."""
    assert cagr_pct([(date(2025, 1, 1), Decimal("100")), (date(2026, 1, 1), Decimal("0"))]) is None


def test_cagr_is_none_over_a_zero_length_span():
    same_day = [(START, Decimal("100")), (START, Decimal("110"))]
    assert cagr_pct(same_day) is None


# ---- the assembled report ---------------------------------------------------------------------------


def test_compute_metrics_assembles_trade_statistics():
    metrics = compute_metrics(
        label="test",
        equity_curve=curve("10000", "10100", "10250"),
        closed_positions=[closed_position("100"), closed_position("200"), closed_position("-50")],
        starting_equity=Decimal("10000"),
        total_commission=Decimal("7.80"),
    )
    assert metrics.trade_count == 3
    assert metrics.win_count == 2 and metrics.loss_count == 1
    assert metrics.win_rate_pct is not None and abs(metrics.win_rate_pct - 66.67) < 0.01
    assert metrics.avg_win == Decimal("150")
    assert metrics.avg_loss == Decimal("-50")
    assert metrics.expectancy_per_trade == Decimal("250") / 3
    assert metrics.profit_factor == 6.0  # 300 gross profit / 50 gross loss
    assert metrics.total_commission == Decimal("7.80")


def test_profit_factor_is_none_with_no_losses():
    metrics = compute_metrics(
        label="test",
        equity_curve=curve("10000", "10100"),
        closed_positions=[closed_position("100")],
        starting_equity=Decimal("10000"),
    )
    assert metrics.profit_factor is None


def test_metrics_with_no_trades_reports_none_not_zero():
    metrics = compute_metrics(
        label="test", equity_curve=curve("10000", "10000"), closed_positions=[], starting_equity=Decimal("10000")
    )
    assert metrics.trade_count == 0
    assert metrics.win_rate_pct is None
    assert metrics.expectancy_per_trade is None
    assert metrics.avg_win is None and metrics.avg_loss is None
    assert metrics.is_profitable is False


def test_metrics_with_an_empty_curve_falls_back_to_starting_equity():
    metrics = compute_metrics(
        label="test", equity_curve=[], closed_positions=[], starting_equity=Decimal("10000")
    )
    assert metrics.ending_equity == Decimal("10000")
    assert metrics.start is None and metrics.end is None


def test_positions_without_a_realized_pnl_are_not_counted_as_break_even_trades():
    """A half-populated Position must not silently score as a zero-P&L trade and drag
    expectancy toward zero."""
    unrealized = closed_position("100")
    unrealized.realized_pnl = None
    metrics = compute_metrics(
        label="test",
        equity_curve=curve("10000", "10100"),
        closed_positions=[closed_position("100"), unrealized],
        starting_equity=Decimal("10000"),
    )
    assert metrics.trade_count == 1


def test_is_profitable_requires_both_positive_expectancy_and_a_gain():
    """One lucky trade against a losing average has not earned the right to be trusted."""
    lucky = compute_metrics(
        label="test",
        equity_curve=curve("10000", "10050"),
        closed_positions=[closed_position("500"), closed_position("-150"), closed_position("-150"),
                          closed_position("-150"), closed_position("-150")],
        starting_equity=Decimal("10000"),
    )
    assert lucky.expectancy_per_trade < 0
    assert lucky.is_profitable is False

    genuine = compute_metrics(
        label="test",
        equity_curve=curve("10000", "10300"),
        closed_positions=[closed_position("100"), closed_position("100"), closed_position("100")],
        starting_equity=Decimal("10000"),
    )
    assert genuine.is_profitable is True


def test_render_produces_a_readable_report_without_crashing_on_none_fields():
    metrics = compute_metrics(
        label="empty run", equity_curve=[], closed_positions=[], starting_equity=Decimal("10000")
    )
    rendered = metrics.render()
    assert "empty run" in rendered
    assert "n/a" in rendered
    assert "NOT PROFITABLE" in rendered
