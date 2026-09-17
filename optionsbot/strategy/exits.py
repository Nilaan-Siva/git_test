"""When to close an open position.

Exit rules are shared across every premium-selling strategy and are driven entirely by
risk.yaml (`profit_target_pct`, `stop_loss_multiple`, `time_stop_dte`), not by strategies.yaml.
That placement is deliberate: taking profits at 50% and refusing to hold a short spread through
the last three weeks of gamma are risk decisions, and the Phase 7 learning loop -- which may
tune strategies.yaml but never risk.yaml -- must not be able to widen a stop or push a time
stop later to make a losing strategy's numbers look better.

The reference amount all three rules measure against is the credit received (for a credit
spread) or the debit paid (for a debit spread), per share.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from optionsbot.config.schema import RiskConfig
from optionsbot.core.models import Position


@dataclass(frozen=True)
class ExitSignal:
    """A decision to close, with the reason recorded for the journal and the evening report."""

    reason: str


def reference_premium_per_share(position: Position) -> Decimal:
    """The credit received or debit paid at entry, per share, as a positive number."""
    return abs(position.entry_price_per_share)


def evaluate_exit(
    position: Position,
    *,
    close_price_per_share: Decimal,
    as_of: date,
    risk: RiskConfig,
) -> Optional[ExitSignal]:
    """Whether `position` should be closed today, and why. None means hold.

    `close_price_per_share` is what it would cost to close *now*, in core/models.py's
    cash-flow-to-trader terms (so closing a short put spread is a positive number: you pay to
    buy it back).

    Checks run worst-case first -- expiration, then stop loss, then profit target, then time
    stop. Daily-bar data can't tell us whether the stop or the target was touched first on a day
    that saw both, so the backtest assumes the stop. That biases results downward, which is the
    correct direction for a bias you can't eliminate.
    """
    dte = position.dte(as_of)
    if dte <= 0:
        return ExitSignal(reason="expiration")

    premium = reference_premium_per_share(position)
    profit_per_share = -(position.entry_price_per_share + close_price_per_share)

    if premium > 0:
        loss_per_share = -profit_per_share
        # A debit spread cannot lose more than the debit paid, so its stop is capped there
        # regardless of the configured multiple -- otherwise the rule is simply unreachable.
        max_stop = premium * risk.stop_loss_multiple
        stop_threshold = max_stop if position.entry_price_per_share < 0 else min(max_stop, premium)
        if loss_per_share >= stop_threshold:
            return ExitSignal(reason="stop_loss")

        if profit_per_share >= premium * risk.profit_target_pct:
            return ExitSignal(reason="profit_target")

    if dte <= risk.time_stop_dte:
        return ExitSignal(reason="time_stop")

    return None
