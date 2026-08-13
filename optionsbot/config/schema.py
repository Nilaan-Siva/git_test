"""Pydantic schemas for the committed, human-edited YAML config files.

These are the shapes that risk.yaml, strategies.yaml, and universe.yaml must validate against.
Validating on load (see loader.py) means a typo'd key or an out-of-range value fails at startup
with a clear error, instead of silently taking a default and disabling a risk control.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    """Hard risk limits. The risk manager (optionsbot/risk/) reads this and can only ever
    enforce it more strictly than a strategy would like -- it never loosens on its own."""

    max_risk_per_trade_pct: Decimal = Decimal("0.01")
    max_portfolio_heat_pct: Decimal = Decimal("0.06")
    max_portfolio_heat_pct_high_vix: Decimal = Decimal("0.04")
    high_vix_threshold: Decimal = Decimal("25")

    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_weekly_loss_pct: Decimal = Decimal("0.06")

    max_positions_per_underlying: int = 1
    max_positions_per_correlated_bucket: int = 2
    correlated_buckets: dict[str, list[str]] = Field(default_factory=dict)
    # When more than one position per underlying is allowed, they must sit on DIFFERENT
    # expirations. Otherwise "laddering" is just doubling the same bet: two spreads on the same
    # expiry share one price path and lose together, which is concentration wearing the costume
    # of diversification.
    require_distinct_expirations: bool = True

    earnings_blackout_days: int = 7

    profit_target_pct: Decimal = Decimal("0.50")
    stop_loss_multiple: Decimal = Decimal("2.0")
    time_stop_dte: int = 21

    consecutive_loss_halt: int = 5
    max_drawdown_halt_pct: Decimal = Decimal("0.15")
    broker_disconnect_halt_seconds: int = 60
    data_staleness_halt_seconds: int = 300


class StrategyParams(BaseModel):
    """Per-strategy tunables. This IS the surface the Phase 7 learning loop may propose changes
    to (always clamped within risk.yaml, always requiring your approval) -- risk.yaml itself is
    never touched by the learning loop."""

    enabled: bool = True
    target_dte_min: int = 30
    target_dte_max: int = 50
    short_delta_target: Decimal = Decimal("0.30")
    short_delta_tolerance: Decimal = Decimal("0.05")
    min_iv_rank: Decimal = Decimal("30")
    spread_width: Decimal = Decimal("1")
    # A credit too small relative to the width isn't worth the risk or the four commission
    # charges it takes to open and close: on a 1-wide spread, $0.15 is ~$15 of max profit
    # against ~$2.60 of round-trip commission. Below this floor the strategy stands down.
    min_credit_pct_of_width: Decimal = Decimal("0.15")
    # Trend filter lookback. Deliberately long (200 days, not 50): IV Rank rises precisely when
    # price falls, so a short-term trend gate is almost perfectly anticorrelated with the IVR
    # gate and the two together can veto every single day. A long-term gate instead expresses
    # the setup this strategy actually wants -- sell premium into a pullback *within* an
    # ongoing uptrend -- and stands down only when the longer trend has genuinely broken.
    trend_sma_period: int = 200


class StrategiesConfig(BaseModel):
    put_credit_spread: StrategyParams = Field(default_factory=StrategyParams)
    iron_condor: StrategyParams = Field(
        default_factory=lambda: StrategyParams(enabled=False, short_delta_target=Decimal("0.16"), min_iv_rank=Decimal("50"))
    )
    wheel: StrategyParams = Field(default_factory=lambda: StrategyParams(enabled=False, min_iv_rank=Decimal("0")))


class UniverseConfig(BaseModel):
    """Tradable underlyings and liquidity floors. Both the backtest engine and the risk manager
    reject any contract failing these floors regardless of strategy signal."""

    tickers: list[str] = Field(default_factory=lambda: ["SPY", "XSP"])
    min_option_open_interest: int = 100
    # Fallback liquidity floor for providers that don't report open interest at all (Polygon's
    # free tier is one). Daily contract volume is a weaker signal than OI -- it says the
    # contract traded today, not that a position can be exited -- so this gate is a smoke test
    # for "is anyone trading this at all", not a substitute for the OI floor.
    min_option_volume_when_oi_unknown: int = 10
    max_bid_ask_spread_pct_of_mid: Decimal = Decimal("0.10")
