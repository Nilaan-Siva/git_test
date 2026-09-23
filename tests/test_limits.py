"""Tests for risk/limits.py -- each individual check in isolation, including boundary conditions
(>=, <= exactly at the configured limit) since off-by-one errors here are exactly the kind of
bug that lets an account blow through a limit it thinks it has."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from optionsbot.config.schema import RiskConfig
from optionsbot.core.enums import Action, Right, StrategyName
from optionsbot.core.models import (
    AccountSnapshot,
    Leg,
    OptionContract,
    OrderIntent,
    PortfolioState,
    Position,
    Spread,
)
from optionsbot.risk import limits

NOW = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)


def make_account(equity: str = "10000", cash: str = "10000") -> AccountSnapshot:
    return AccountSnapshot(timestamp=NOW, equity=Decimal(equity), cash=Decimal(cash), buying_power=Decimal(cash))


def make_portfolio(**overrides) -> PortfolioState:
    defaults = dict(
        account=make_account(),
        open_positions=[],
        high_water_mark_equity=Decimal("10000"),
        realized_pnl_today=Decimal("0"),
        realized_pnl_this_week=Decimal("0"),
        last_data_update=NOW,
        last_broker_heartbeat=NOW,
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def make_position(underlying: str, max_loss: str = "350", quantity: int = 1) -> Position:
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


def make_intent(underlying: str = "SPY") -> OrderIntent:
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
    return OrderIntent(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=spread,
        limit_price_per_share=Decimal("-1.50"),
        max_loss_per_contract=Decimal("350"),
        max_profit_per_contract=Decimal("150"),
    )


# RiskConfig()'s own pydantic default for correlated_buckets is an empty dict -- only the
# shipped risk.yaml populates it (see test_config.py for that integration check). Tests here
# construct it explicitly so they're self-contained and don't silently depend on the yaml file's
# exact contents.
DEFAULT_CONFIG = RiskConfig(correlated_buckets={"us_broad_market": ["SPY", "XSP", "QQQ", "IWM"]})


# ---- drawdown -----------------------------------------------------------------------------


def test_drawdown_below_limit_passes():
    portfolio = make_portfolio(high_water_mark_equity=Decimal("10000"), account=make_account(equity="9000"))  # 10% dd
    assert limits.check_drawdown_halt(portfolio, DEFAULT_CONFIG) is None


def test_drawdown_at_exact_limit_halts():
    # default max_drawdown_halt_pct = 0.15 -> equity at exactly 85% of high-water-mark
    portfolio = make_portfolio(high_water_mark_equity=Decimal("10000"), account=make_account(equity="8500"))
    reason = limits.check_drawdown_halt(portfolio, DEFAULT_CONFIG)
    assert reason is not None
    assert "max_drawdown_halt" in reason


def test_drawdown_zero_high_water_mark_does_not_crash():
    portfolio = make_portfolio(high_water_mark_equity=Decimal("0"), account=make_account(equity="0"))
    assert limits.check_drawdown_halt(portfolio, DEFAULT_CONFIG) is None


# ---- consecutive losses --------------------------------------------------------------------


def test_consecutive_losses_below_limit_passes():
    portfolio = make_portfolio(consecutive_losses=4)  # default halt = 5
    assert limits.check_consecutive_loss_halt(portfolio, DEFAULT_CONFIG) is None


def test_consecutive_losses_at_exact_limit_halts():
    portfolio = make_portfolio(consecutive_losses=5)
    reason = limits.check_consecutive_loss_halt(portfolio, DEFAULT_CONFIG)
    assert reason is not None and "consecutive_loss_halt" in reason


# ---- daily / weekly loss --------------------------------------------------------------------


def test_daily_loss_below_limit_passes():
    portfolio = make_portfolio(realized_pnl_today=Decimal("-299"))  # 3% of 10000 = 300
    assert limits.check_daily_loss_halt(portfolio, DEFAULT_CONFIG) is None


def test_daily_loss_at_exact_limit_halts():
    portfolio = make_portfolio(realized_pnl_today=Decimal("-300"))
    reason = limits.check_daily_loss_halt(portfolio, DEFAULT_CONFIG)
    assert reason is not None and "daily_loss_halt" in reason


def test_daily_loss_positive_pnl_never_halts():
    portfolio = make_portfolio(realized_pnl_today=Decimal("500"))
    assert limits.check_daily_loss_halt(portfolio, DEFAULT_CONFIG) is None


def test_weekly_loss_at_exact_limit_halts():
    portfolio = make_portfolio(realized_pnl_this_week=Decimal("-600"))  # 6% of 10000
    reason = limits.check_weekly_loss_halt(portfolio, DEFAULT_CONFIG)
    assert reason is not None and "weekly_loss_halt" in reason


def test_loss_halts_do_not_crash_on_zero_equity():
    portfolio = make_portfolio(account=make_account(equity="0"), realized_pnl_today=Decimal("-1000"))
    assert limits.check_daily_loss_halt(portfolio, DEFAULT_CONFIG) is None
    assert limits.check_weekly_loss_halt(portfolio, DEFAULT_CONFIG) is None


# ---- data staleness / broker disconnect ------------------------------------------------------


def test_data_staleness_fresh_passes():
    portfolio = make_portfolio(last_data_update=NOW)
    assert limits.check_data_staleness(portfolio, DEFAULT_CONFIG, now=NOW) is None


def test_data_staleness_none_halts():
    portfolio = make_portfolio(last_data_update=None)
    reason = limits.check_data_staleness(portfolio, DEFAULT_CONFIG, now=NOW)
    assert reason is not None and "data_staleness_halt" in reason


def test_data_staleness_too_old_halts():
    old = NOW - timedelta(seconds=DEFAULT_CONFIG.data_staleness_halt_seconds + 1)
    portfolio = make_portfolio(last_data_update=old)
    reason = limits.check_data_staleness(portfolio, DEFAULT_CONFIG, now=NOW)
    assert reason is not None and "data_staleness_halt" in reason


def test_broker_disconnect_none_halts():
    portfolio = make_portfolio(last_broker_heartbeat=None)
    reason = limits.check_broker_disconnect(portfolio, DEFAULT_CONFIG, now=NOW)
    assert reason is not None and "broker_disconnect_halt" in reason


def test_broker_disconnect_too_old_halts():
    old = NOW - timedelta(seconds=DEFAULT_CONFIG.broker_disconnect_halt_seconds + 1)
    portfolio = make_portfolio(last_broker_heartbeat=old)
    reason = limits.check_broker_disconnect(portfolio, DEFAULT_CONFIG, now=NOW)
    assert reason is not None and "broker_disconnect_halt" in reason


def test_broker_disconnect_fresh_passes():
    portfolio = make_portfolio(last_broker_heartbeat=NOW)
    assert limits.check_broker_disconnect(portfolio, DEFAULT_CONFIG, now=NOW) is None


# ---- per-underlying / correlated bucket ------------------------------------------------------


def test_per_underlying_limit_passes_when_no_existing_position():
    portfolio = make_portfolio(open_positions=[])
    assert limits.check_per_underlying_limit(make_intent("SPY"), portfolio, DEFAULT_CONFIG) is None


def test_per_underlying_limit_blocks_second_position_in_same_underlying():
    portfolio = make_portfolio(open_positions=[make_position("SPY")])  # default limit = 1
    reason = limits.check_per_underlying_limit(make_intent("SPY"), portfolio, DEFAULT_CONFIG)
    assert reason is not None and "per_underlying_limit" in reason


def test_per_underlying_limit_allows_different_underlying():
    portfolio = make_portfolio(open_positions=[make_position("SPY")])
    assert limits.check_per_underlying_limit(make_intent("QQQ"), portfolio, DEFAULT_CONFIG) is None


def test_correlated_bucket_limit_blocks_at_cap():
    # default bucket us_broad_market = [SPY, XSP, QQQ, IWM], cap = 2
    portfolio = make_portfolio(open_positions=[make_position("SPY"), make_position("QQQ")])
    reason = limits.check_correlated_bucket_limit(make_intent("IWM"), portfolio, DEFAULT_CONFIG)
    assert reason is not None and "correlated_bucket_limit" in reason


def test_correlated_bucket_limit_allows_below_cap():
    portfolio = make_portfolio(open_positions=[make_position("SPY")])
    assert limits.check_correlated_bucket_limit(make_intent("QQQ"), portfolio, DEFAULT_CONFIG) is None


def test_correlated_bucket_limit_ignores_underlying_not_in_any_bucket():
    portfolio = make_portfolio(
        open_positions=[make_position("AAPL"), make_position("AAPL")]  # not in any configured bucket
    )
    assert limits.check_correlated_bucket_limit(make_intent("AAPL"), portfolio, DEFAULT_CONFIG) is None


# ---- heat cap / room -----------------------------------------------------------------------


def test_heat_cap_normal_vix():
    portfolio = make_portfolio(vix_level=Decimal("18"))
    assert limits.current_heat_cap_pct(portfolio, DEFAULT_CONFIG) == DEFAULT_CONFIG.max_portfolio_heat_pct


def test_heat_cap_tightens_above_high_vix_threshold():
    portfolio = make_portfolio(vix_level=Decimal("30"))
    assert limits.current_heat_cap_pct(portfolio, DEFAULT_CONFIG) == DEFAULT_CONFIG.max_portfolio_heat_pct_high_vix


def test_heat_cap_at_exact_threshold_does_not_tighten():
    # check uses strict > , so exactly at the threshold should NOT be treated as "high vix"
    portfolio = make_portfolio(vix_level=DEFAULT_CONFIG.high_vix_threshold)
    assert limits.current_heat_cap_pct(portfolio, DEFAULT_CONFIG) == DEFAULT_CONFIG.max_portfolio_heat_pct


def test_heat_cap_none_vix_uses_normal_cap():
    portfolio = make_portfolio(vix_level=None)
    assert limits.current_heat_cap_pct(portfolio, DEFAULT_CONFIG) == DEFAULT_CONFIG.max_portfolio_heat_pct


def test_heat_room_remaining_full_when_no_positions():
    portfolio = make_portfolio(account=make_account(equity="10000"), open_positions=[])
    assert limits.heat_room_remaining(portfolio, DEFAULT_CONFIG) == Decimal("600")  # 6% of 10000


def test_heat_room_remaining_reduced_by_existing_positions():
    portfolio = make_portfolio(account=make_account(equity="10000"), open_positions=[make_position("SPY", max_loss="400")])
    assert limits.heat_room_remaining(portfolio, DEFAULT_CONFIG) == Decimal("200")  # 600 - 400


def test_heat_room_remaining_never_negative_when_over_cap():
    portfolio = make_portfolio(account=make_account(equity="10000"), open_positions=[make_position("SPY", max_loss="900")])
    assert limits.heat_room_remaining(portfolio, DEFAULT_CONFIG) == Decimal("0")


def test_heat_room_remaining_zero_equity_returns_zero():
    portfolio = make_portfolio(account=make_account(equity="0"))
    assert limits.heat_room_remaining(portfolio, DEFAULT_CONFIG) == Decimal("0")
