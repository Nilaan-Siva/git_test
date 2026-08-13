"""Tests for risk/manager.py -- the veto gate itself. Exercises every rejection branch plus the
full happy path, and documents (via a characterization test) the stateless-sequential-calls
behavior the module's docstring warns about."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from optionsbot.config.schema import RiskConfig
from optionsbot.core.enums import Action, OrderState, Right, StrategyName
from optionsbot.core.models import (
    AccountSnapshot,
    Leg,
    OptionContract,
    OrderIntent,
    PortfolioState,
    Position,
    Spread,
)
from optionsbot.risk.manager import approve

NOW = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)
# RiskConfig()'s own pydantic default for correlated_buckets is an empty dict -- only the
# shipped risk.yaml populates it. Construct it explicitly so these tests are self-contained.
DEFAULT_CONFIG = RiskConfig(correlated_buckets={"us_broad_market": ["SPY", "XSP", "QQQ", "IWM"]})


def make_account(equity: str = "10000") -> AccountSnapshot:
    return AccountSnapshot(timestamp=NOW, equity=Decimal(equity), cash=Decimal(equity), buying_power=Decimal(equity))


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


def make_vertical_spread(underlying: str = "SPY", short_strike: str = "450", long_strike: str = "445") -> Spread:
    return Spread(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        underlying=underlying,
        legs=[
            Leg(
                contract=OptionContract(underlying=underlying, expiration=date(2026, 9, 18), strike=Decimal(short_strike), right=Right.PUT),
                action=Action.SELL_TO_OPEN,
            ),
            Leg(
                contract=OptionContract(underlying=underlying, expiration=date(2026, 9, 18), strike=Decimal(long_strike), right=Right.PUT),
                action=Action.BUY_TO_OPEN,
            ),
        ],
    )


def make_intent(underlying: str = "SPY", max_loss: str = "350") -> OrderIntent:
    return OrderIntent(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=make_vertical_spread(underlying),
        limit_price_per_share=Decimal("-1.50"),
        max_loss_per_contract=Decimal(max_loss),
        max_profit_per_contract=Decimal("150"),
    )


def make_position(underlying: str, max_loss: str = "350") -> Position:
    return Position(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=make_vertical_spread(underlying),
        quantity=1,
        entry_price_per_share=Decimal("-1.50"),
        entry_date=date(2026, 8, 1),
        max_loss_per_contract=Decimal(max_loss),
        max_profit_per_contract=Decimal("150"),
    )


# ---- happy path -----------------------------------------------------------------------------


def test_approve_happy_path():
    portfolio = make_portfolio(account=make_account(equity="100000"))  # plenty of room
    intent = make_intent(max_loss="350")
    decision = approve(intent, portfolio, DEFAULT_CONFIG, now=NOW)

    assert decision.approved is True
    assert decision.order is not None
    assert decision.order.intent_id == intent.id
    assert decision.order.quantity >= 1
    assert decision.order.state == OrderState.PENDING
    # 1% of 100,000 = 1,000; 1,000 // 350 = 2 contracts, within the 6% heat cap ($6,000)
    assert decision.order.quantity == 2


# ---- kill switches take priority and short-circuit ------------------------------------------


def test_approve_rejects_on_drawdown_before_anything_else():
    portfolio = make_portfolio(high_water_mark_equity=Decimal("100000"), account=make_account(equity="80000"))
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False
    assert "max_drawdown_halt" in decision.reason
    assert decision.order is None


def test_approve_rejects_on_consecutive_losses():
    portfolio = make_portfolio(consecutive_losses=DEFAULT_CONFIG.consecutive_loss_halt)
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "consecutive_loss_halt" in decision.reason


def test_approve_rejects_on_daily_loss():
    portfolio = make_portfolio(realized_pnl_today=Decimal("-1000"))  # > 3% of 10000
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "daily_loss_halt" in decision.reason


def test_approve_rejects_on_weekly_loss():
    portfolio = make_portfolio(realized_pnl_this_week=Decimal("-1000"))  # > 6% of 10000
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "weekly_loss_halt" in decision.reason


def test_approve_rejects_on_stale_data():
    portfolio = make_portfolio(last_data_update=NOW - timedelta(seconds=10_000))
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "data_staleness_halt" in decision.reason


def test_approve_rejects_on_broker_disconnect():
    portfolio = make_portfolio(last_broker_heartbeat=NOW - timedelta(seconds=10_000))
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "broker_disconnect_halt" in decision.reason


# ---- structural / defined-risk checks --------------------------------------------------------


def test_approve_rejects_zero_max_loss_intent():
    intent = OrderIntent(
        strategy=StrategyName.PUT_CREDIT_SPREAD,
        spread=make_vertical_spread(),
        limit_price_per_share=Decimal("0"),
        max_loss_per_contract=Decimal("0"),
        max_profit_per_contract=Decimal("0"),
    )
    portfolio = make_portfolio()
    decision = approve(intent, portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "invalid_intent" in decision.reason


def test_approve_rejects_naked_short_with_no_protective_leg():
    naked_spread = Spread(
        strategy=StrategyName.WHEEL,
        underlying="SPY",
        legs=[
            Leg(
                contract=OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("450"), right=Right.PUT),
                action=Action.SELL_TO_OPEN,
            )
        ],
    )
    intent = OrderIntent(
        strategy=StrategyName.WHEEL,
        spread=naked_spread,
        limit_price_per_share=Decimal("-1.50"),
        max_loss_per_contract=Decimal("44850"),  # theoretical max loss to strike zero
        max_profit_per_contract=Decimal("150"),
    )
    portfolio = make_portfolio(account=make_account(equity="1000000"))  # rule out sizing/heat as the cause
    decision = approve(intent, portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "naked_short_not_allowed" in decision.reason


def test_approve_allows_short_with_protective_long_of_same_right():
    # sanity check: the standard vertical spread (short put + long put) is NOT flagged as naked
    portfolio = make_portfolio(account=make_account(equity="100000"))
    decision = approve(make_intent(), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is True


# ---- per-underlying / correlated bucket ------------------------------------------------------


def test_approve_rejects_second_position_same_underlying():
    portfolio = make_portfolio(account=make_account(equity="100000"), open_positions=[make_position("SPY")])
    decision = approve(make_intent("SPY"), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "per_underlying_limit" in decision.reason


def test_approve_rejects_at_correlated_bucket_cap():
    portfolio = make_portfolio(
        account=make_account(equity="100000"),
        open_positions=[make_position("SPY"), make_position("QQQ")],
    )
    decision = approve(make_intent("IWM"), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "correlated_bucket_limit" in decision.reason


# ---- sizing / heat ---------------------------------------------------------------------------


def test_approve_rejects_when_account_too_small_for_one_contract():
    # high_water_mark must match equity here, or the drawdown kill switch fires first and
    # masks the sizing behavior this test is actually exercising.
    portfolio = make_portfolio(account=make_account(equity="2000"), high_water_mark_equity=Decimal("2000"))
    decision = approve(make_intent(max_loss="350"), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "position_size_zero" in decision.reason


def test_approve_clamps_quantity_down_to_available_heat_room():
    # equity=100,000 -> 1% risk budget = $1,000 -> wants floor(1000/300)=3 contracts ($900 heat)
    # but an existing position already uses $5,800 of the $6,000 (6%) heat cap, leaving $200 --
    # enough for exactly 0 more at $300/contract... use $100/contract existing position sizing
    # so the remaining room clamps to fewer than the risk-sized quantity but still >= 1.
    existing = make_position("QQQ", max_loss="5700")  # leaves $300 of the $6,000 cap
    portfolio = make_portfolio(account=make_account(equity="100000"), open_positions=[existing])
    intent = make_intent("SPY", max_loss="200")  # risk-sized quantity would be floor(1000/200)=5
    decision = approve(intent, portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is True
    assert decision.order.quantity == 1  # clamped from 5 down to what $300 of room allows (1)


def test_approve_rejects_when_heat_fully_exhausted():
    existing = make_position("QQQ", max_loss="6000")  # exactly at the 6% cap already
    portfolio = make_portfolio(account=make_account(equity="100000"), open_positions=[existing])
    decision = approve(make_intent("SPY", max_loss="200"), portfolio, DEFAULT_CONFIG, now=NOW)
    assert decision.approved is False and "portfolio_heat_exhausted" in decision.reason


# ---- documented stateless-sequential-calls characterization -----------------------------------


def test_sequential_calls_with_stale_portfolio_do_not_see_each_others_heat_usage():
    """Characterizes the behavior documented in risk/manager.py's module docstring: approve()
    has no memory between calls, so two calls against the SAME (unrefreshed) PortfolioState can
    both approve trades that, combined, exceed the heat cap. This is the caller's
    responsibility to avoid (refresh PortfolioState between approvals) -- not a bug in
    approve() itself, but exactly the kind of thing that must be true for the module to stay a
    pure, trivially-testable function, and exactly the kind of thing a smaller "just fix it
    here" patch would quietly hide.

    Note: the default config's 1%-per-trade / 6%-heat ratio gives 6x headroom, so it takes a
    tighter heat cap than the shipped default to demonstrate a 2-call collision -- with the
    default ratio you'd need 7 unrefreshed concurrent approvals to see this, which is a real
    argument for why that ratio is a sane default, not just an artifact of this test.
    """
    tight_config = DEFAULT_CONFIG.model_copy(update={"max_portfolio_heat_pct": Decimal("0.015")})
    portfolio = make_portfolio(account=make_account(equity="100000"))  # heat cap = 1.5% = $1,500
    intent_a = make_intent("SPY", max_loss="800")
    intent_b = make_intent("QQQ", max_loss="800")

    first = approve(intent_a, portfolio, tight_config, now=NOW)
    second = approve(intent_b, portfolio, tight_config, now=NOW)

    assert first.approved is True
    assert second.approved is True  # both approved against the SAME stale snapshot
    combined_heat = first.order.quantity * Decimal("800") + second.order.quantity * Decimal("800")
    assert combined_heat > Decimal("1500")  # together they exceed the cap neither call alone saw
