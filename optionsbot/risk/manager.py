"""The risk manager: THE single veto gate every OrderIntent must pass through before it can
become an Order.

`approve()` is a pure function of (intent, portfolio, config, now) -> Decision: no I/O, no
hidden state, fully unit-testable. It can only ever reject a trade a strategy proposes or size
it down; it never loosens a limit and never invents a trade a strategy didn't ask for.

IMPORTANT -- statelessness and sequential calls: this module has no memory of its own between
calls. If you call approve() twice in a row with the SAME PortfolioState (e.g. evaluating two
intents from the same strategy cycle before either has actually been filled), both calls will
see the same open_positions and the same heat room, and could both approve trades that together
exceed a limit neither call could see on its own. The caller (execution/router.py, Phase 5) is
responsible for either processing intents strictly one at a time -- refreshing PortfolioState
(or provisionally reserving heat) after each approval and before evaluating the next -- or
building a reservation layer on top of this if genuinely concurrent evaluation is ever needed.
This module deliberately doesn't do that itself, to keep it a pure, trivially-testable function.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from optionsbot.config.schema import RiskConfig
from optionsbot.core.enums import Action, OrderState
from optionsbot.core.models import Order, OrderIntent, PortfolioState
from optionsbot.risk import limits
from optionsbot.risk.limits import heat_room_remaining
from optionsbot.risk.sizing import clamp_to_heat_room, size_by_risk_pct


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    order: Optional[Order] = None


# Kill switches guard the whole account, not just this one trade -- they run first, in this
# order, and short-circuit on the first hit.
_KILL_SWITCHES = (
    limits.check_drawdown_halt,
    limits.check_consecutive_loss_halt,
    limits.check_daily_loss_halt,
    limits.check_weekly_loss_halt,
)


def _has_naked_short(intent: OrderIntent) -> bool:
    """True if any SELL_TO_OPEN leg lacks a same-right BUY_TO_OPEN leg protecting it.

    This bot is defined-risk-only by design (see risk.yaml's "defined risk only" rule); this is
    the last line of defense against a strategy bug proposing an unprotected short, not the
    primary mechanism (strategy modules should never construct such a spread in the first
    place).
    """
    short_rights = {leg.contract.right for leg in intent.spread.legs if leg.action == Action.SELL_TO_OPEN}
    long_rights = {leg.contract.right for leg in intent.spread.legs if leg.action == Action.BUY_TO_OPEN}
    return not short_rights.issubset(long_rights)


def approve(intent: OrderIntent, portfolio: PortfolioState, config: RiskConfig, *, now: datetime) -> Decision:
    for check in _KILL_SWITCHES:
        reason = check(portfolio, config)
        if reason:
            return Decision(approved=False, reason=reason)

    reason = limits.check_data_staleness(portfolio, config, now)
    if reason:
        return Decision(approved=False, reason=reason)
    reason = limits.check_broker_disconnect(portfolio, config, now)
    if reason:
        return Decision(approved=False, reason=reason)

    if intent.max_loss_per_contract <= 0:
        return Decision(approved=False, reason="invalid_intent: max_loss_per_contract must be positive")

    if _has_naked_short(intent):
        return Decision(
            approved=False,
            reason="naked_short_not_allowed: every short leg needs a protective long leg of the same right",
        )

    reason = limits.check_per_underlying_limit(intent, portfolio, config)
    if reason:
        return Decision(approved=False, reason=reason)
    reason = limits.check_distinct_expirations(intent, portfolio, config)
    if reason:
        return Decision(approved=False, reason=reason)
    reason = limits.check_correlated_bucket_limit(intent, portfolio, config)
    if reason:
        return Decision(approved=False, reason=reason)

    quantity = size_by_risk_pct(portfolio.account.equity, config.max_risk_per_trade_pct, intent.max_loss_per_contract)
    if quantity < 1:
        risk_budget = portfolio.account.equity * config.max_risk_per_trade_pct
        return Decision(
            approved=False,
            reason=(
                f"position_size_zero: {config.max_risk_per_trade_pct * 100:.1f}% of equity "
                f"({risk_budget}) is less than one contract's max loss ({intent.max_loss_per_contract})"
            ),
        )

    room = heat_room_remaining(portfolio, config)
    heat_clamped_quantity = clamp_to_heat_room(quantity, intent.max_loss_per_contract, room)
    if heat_clamped_quantity < 1:
        return Decision(approved=False, reason=f"portfolio_heat_exhausted: {room} of heat room remaining")

    order = Order(
        intent_id=intent.id,
        spread=intent.spread,
        quantity=heat_clamped_quantity,
        limit_price_per_share=intent.limit_price_per_share,
        state=OrderState.PENDING,
    )
    return Decision(approved=True, reason="approved", order=order)
