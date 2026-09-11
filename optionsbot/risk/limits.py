"""Individual risk-limit checks. Each function returns None if the check passes, or a short
human-readable rejection reason string if it fails. risk/manager.py runs these as a checklist
and stops at the first failure -- keeping each rule isolated and independently testable is the
whole point of this module's (and its adversarial test suite's) existence.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from optionsbot.config.schema import RiskConfig
from optionsbot.core.models import OrderIntent, PortfolioState
from optionsbot.risk.exposure import heat_pct_of_equity, positions_for_underlying, positions_in_bucket

# ---- account-wide kill switches -------------------------------------------------------------


def check_drawdown_halt(portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    if portfolio.high_water_mark_equity <= 0:
        return None
    drawdown = (portfolio.high_water_mark_equity - portfolio.account.equity) / portfolio.high_water_mark_equity
    if drawdown >= config.max_drawdown_halt_pct:
        return f"max_drawdown_halt: {drawdown * 100:.1f}% drawdown >= {config.max_drawdown_halt_pct * 100:.1f}% limit"
    return None


def check_consecutive_loss_halt(portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    if portfolio.consecutive_losses >= config.consecutive_loss_halt:
        return (
            f"consecutive_loss_halt: {portfolio.consecutive_losses} losses in a row "
            f">= {config.consecutive_loss_halt}"
        )
    return None


def check_daily_loss_halt(portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    equity = portfolio.account.equity
    if equity <= 0:
        return None
    loss_limit = -(config.max_daily_loss_pct * equity)
    if portfolio.realized_pnl_today <= loss_limit:
        return (
            f"daily_loss_halt: today's realized P&L {portfolio.realized_pnl_today} <= "
            f"-{config.max_daily_loss_pct * 100:.1f}% of equity ({loss_limit})"
        )
    return None


def check_weekly_loss_halt(portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    equity = portfolio.account.equity
    if equity <= 0:
        return None
    loss_limit = -(config.max_weekly_loss_pct * equity)
    if portfolio.realized_pnl_this_week <= loss_limit:
        return (
            f"weekly_loss_halt: this week's realized P&L {portfolio.realized_pnl_this_week} <= "
            f"-{config.max_weekly_loss_pct * 100:.1f}% of equity ({loss_limit})"
        )
    return None


def check_data_staleness(portfolio: PortfolioState, config: RiskConfig, now: datetime) -> Optional[str]:
    if portfolio.last_data_update is None:
        return "data_staleness_halt: no market data update has ever been recorded"
    age = (now - portfolio.last_data_update).total_seconds()
    if age > config.data_staleness_halt_seconds:
        return f"data_staleness_halt: market data is {age:.0f}s old (limit {config.data_staleness_halt_seconds}s)"
    return None


def check_broker_disconnect(portfolio: PortfolioState, config: RiskConfig, now: datetime) -> Optional[str]:
    if portfolio.last_broker_heartbeat is None:
        return "broker_disconnect_halt: no broker heartbeat has ever been recorded"
    age = (now - portfolio.last_broker_heartbeat).total_seconds()
    if age > config.broker_disconnect_halt_seconds:
        return (
            f"broker_disconnect_halt: broker heartbeat is {age:.0f}s old "
            f"(limit {config.broker_disconnect_halt_seconds}s)"
        )
    return None


# ---- per-trade structural limits ------------------------------------------------------------


def check_per_underlying_limit(intent: OrderIntent, portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    existing = positions_for_underlying(portfolio.open_positions, intent.spread.underlying)
    if len(existing) >= config.max_positions_per_underlying:
        return (
            f"per_underlying_limit: {len(existing)} open position(s) in {intent.spread.underlying} "
            f">= limit {config.max_positions_per_underlying}"
        )
    return None


def check_distinct_expirations(intent: OrderIntent, portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    """Reject a second position on an expiration this underlying already holds.

    This is what makes `max_positions_per_underlying > 1` a ladder rather than a doubled bet.
    Two spreads on the same underlying AND the same expiration share a single price path: they
    win together and, more to the point, they lose together, so the account carries twice the
    risk of one position while the per-trade limit reports that each is within bounds.
    Staggering expirations is the entire reason to allow more than one position at all.
    """
    if not config.require_distinct_expirations:
        return None
    proposed = {leg.contract.expiration for leg in intent.spread.legs}
    for position in positions_for_underlying(portfolio.open_positions, intent.spread.underlying):
        held = {leg.contract.expiration for leg in position.spread.legs}
        clash = held & proposed
        if clash:
            return (
                f"duplicate_expiration: already hold {intent.spread.underlying} expiring "
                f"{sorted(clash)[0].isoformat()}"
            )
    return None


def check_correlated_bucket_limit(intent: OrderIntent, portfolio: PortfolioState, config: RiskConfig) -> Optional[str]:
    underlying = intent.spread.underlying.upper()
    for bucket_name, members in config.correlated_buckets.items():
        if underlying in {m.upper() for m in members}:
            existing = positions_in_bucket(portfolio.open_positions, members)
            if len(existing) >= config.max_positions_per_correlated_bucket:
                return (
                    f"correlated_bucket_limit: {len(existing)} open position(s) in bucket "
                    f"'{bucket_name}' >= limit {config.max_positions_per_correlated_bucket}"
                )
    return None


# ---- portfolio heat ---------------------------------------------------------------------------


def current_heat_cap_pct(portfolio: PortfolioState, config: RiskConfig) -> Decimal:
    """The applicable portfolio-heat ceiling, tightened automatically when VIX is elevated."""
    if portfolio.vix_level is not None and portfolio.vix_level > config.high_vix_threshold:
        return config.max_portfolio_heat_pct_high_vix
    return config.max_portfolio_heat_pct


def heat_room_remaining(portfolio: PortfolioState, config: RiskConfig) -> Decimal:
    """Dollars of additional max-loss the portfolio can still take on before hitting its heat
    cap. Never negative -- returns 0 if already at or past the cap."""
    equity = portfolio.account.equity
    if equity <= 0:
        return Decimal("0")
    cap_dollars = current_heat_cap_pct(portfolio, config) * equity
    used_dollars = heat_pct_of_equity(portfolio.open_positions, equity) * equity
    return max(Decimal("0"), cap_dollars - used_dollars)
