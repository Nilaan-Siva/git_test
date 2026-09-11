"""Performance metrics for a completed backtest (and, later, for live results).

Pure functions over an equity curve and a list of closed positions, so the same code scores a
backtest, a walk-forward fold, and a month of paper trading -- which is the only way the Phase 7
"does live match the backtest?" comparison means anything.

Every metric returns `None` when it is genuinely undefined rather than a placeholder zero: a
Sharpe ratio needs at least two returns and a non-zero standard deviation, a profit factor needs
at least one loss, and a CAGR needs positive equity at both ends. Zero would read as a real,
poor result; None reads as "not enough data", which is the truth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from optionsbot.core.models import Position

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class BacktestMetrics:
    label: str
    start: Optional[date]
    end: Optional[date]
    starting_equity: Decimal
    ending_equity: Decimal
    total_return_pct: Optional[Decimal]
    cagr_pct: Optional[float]
    max_drawdown_pct: Optional[Decimal]
    sharpe: Optional[float]
    sortino: Optional[float]
    trade_count: int
    win_count: int
    loss_count: int
    win_rate_pct: Optional[float]
    avg_win: Optional[Decimal]
    avg_loss: Optional[Decimal]
    expectancy_per_trade: Optional[Decimal]
    profit_factor: Optional[float]
    total_commission: Decimal

    @property
    def is_profitable(self) -> bool:
        """Positive expectancy AND positive total P&L. A strategy that made money on one lucky
        trade while losing on average has not earned the right to be trusted."""
        return (
            self.expectancy_per_trade is not None
            and self.expectancy_per_trade > 0
            and self.ending_equity > self.starting_equity
        )

    def render(self) -> str:
        """A plain-text report. Kept here rather than in reporting/ so that scripts can print a
        result without pulling in the Phase 6 reporting stack."""

        def fmt(value, suffix: str = "", places: int = 2) -> str:
            if value is None:
                return "n/a"
            if isinstance(value, Decimal):
                return f"{value:,.{places}f}{suffix}"
            return f"{value:,.{places}f}{suffix}"

        period = f"{self.start} to {self.end}" if self.start and self.end else "no data"
        return "\n".join(
            [
                f"=== Backtest: {self.label} ===",
                f"Period:            {period}",
                f"Equity:            {fmt(self.starting_equity)} -> {fmt(self.ending_equity)}",
                f"Total return:      {fmt(self.total_return_pct, '%')}",
                f"CAGR:              {fmt(self.cagr_pct, '%')}",
                f"Max drawdown:      {fmt(self.max_drawdown_pct, '%')}",
                f"Sharpe / Sortino:  {fmt(self.sharpe)} / {fmt(self.sortino)}",
                f"Trades:            {self.trade_count} ({self.win_count}W / {self.loss_count}L)",
                f"Win rate:          {fmt(self.win_rate_pct, '%')}",
                f"Avg win / loss:    {fmt(self.avg_win)} / {fmt(self.avg_loss)}",
                f"Expectancy/trade:  {fmt(self.expectancy_per_trade)}",
                f"Profit factor:     {fmt(self.profit_factor)}",
                f"Commissions paid:  {fmt(self.total_commission)}",
                f"Verdict:           {'POSITIVE EXPECTANCY' if self.is_profitable else 'NOT PROFITABLE'}",
            ]
        )


def daily_returns(equity_curve: Sequence[tuple[date, Decimal]]) -> list[float]:
    """Simple day-over-day returns. Days where the prior equity is zero are skipped rather than
    producing an infinite return."""
    returns = []
    for (_, prev), (_, curr) in zip(equity_curve, equity_curve[1:]):
        if prev == 0:
            continue
        returns.append(float((curr - prev) / prev))
    return returns


def max_drawdown_pct(equity_curve: Sequence[tuple[date, Decimal]]) -> Optional[Decimal]:
    """Largest peak-to-trough decline, as a positive percentage."""
    if not equity_curve:
        return None
    peak = equity_curve[0][1]
    worst = Decimal("0")
    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak * 100
            worst = max(worst, drawdown)
    return worst


def sharpe_ratio(returns: Sequence[float], *, risk_free_rate: float = 0.0) -> Optional[float]:
    """Annualised Sharpe from daily returns. None if there's no variance to divide by."""
    if len(returns) < 2:
        return None
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    if variance <= 0:
        return None
    return mean / math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def sortino_ratio(returns: Sequence[float], *, risk_free_rate: float = 0.0) -> Optional[float]:
    """Annualised Sortino: like Sharpe, but only downside deviation counts as risk.

    The more honest measure for premium selling, whose return distribution is deliberately
    asymmetric -- many small wins and occasional large losses. Sharpe penalises the upside
    variance that a credit strategy structurally cannot have.
    """
    if len(returns) < 2:
        return None
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return None
    downside_variance = sum(r * r for r in downside) / len(excess)
    if downside_variance <= 0:
        return None
    return mean / math.sqrt(downside_variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def cagr_pct(equity_curve: Sequence[tuple[date, Decimal]]) -> Optional[float]:
    """Compound annual growth rate over the curve's calendar span."""
    if len(equity_curve) < 2:
        return None
    (start_date, start_equity), (end_date, end_equity) = equity_curve[0], equity_curve[-1]
    if start_equity <= 0 or end_equity <= 0:
        return None
    years = (end_date - start_date).days / 365.25
    if years <= 0:
        return None
    return ((float(end_equity) / float(start_equity)) ** (1 / years) - 1) * 100


def compute_metrics(
    *,
    label: str,
    equity_curve: Sequence[tuple[date, Decimal]],
    closed_positions: Sequence[Position],
    starting_equity: Decimal,
    total_commission: Decimal = Decimal("0"),
) -> BacktestMetrics:
    """Score a completed run.

    Only positions with a realised P&L count as trades -- a position closed by the engine always
    has one, but this keeps a half-populated Position from silently scoring as a break-even.
    """
    pnls = [p.realized_pnl for p in closed_positions if p.realized_pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    ending_equity = equity_curve[-1][1] if equity_curve else starting_equity
    total_return = (
        (ending_equity - starting_equity) / starting_equity * 100 if starting_equity > 0 else None
    )

    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))

    return BacktestMetrics(
        label=label,
        start=equity_curve[0][0] if equity_curve else None,
        end=equity_curve[-1][0] if equity_curve else None,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        total_return_pct=total_return,
        cagr_pct=cagr_pct(equity_curve),
        max_drawdown_pct=max_drawdown_pct(equity_curve),
        sharpe=sharpe_ratio(daily_returns(equity_curve)),
        sortino=sortino_ratio(daily_returns(equity_curve)),
        trade_count=len(pnls),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate_pct=(len(wins) / len(pnls) * 100) if pnls else None,
        avg_win=(gross_profit / len(wins)) if wins else None,
        avg_loss=(-gross_loss / len(losses)) if losses else None,
        expectancy_per_trade=(sum(pnls, Decimal("0")) / len(pnls)) if pnls else None,
        profit_factor=(float(gross_profit / gross_loss) if gross_loss > 0 else None),
        total_commission=total_commission,
    )
