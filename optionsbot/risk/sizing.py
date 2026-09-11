"""Position sizing: there is exactly ONE sizing function in the codebase, called only from
risk/manager.py, so the account's per-trade risk rule can never be silently bypassed by a
strategy computing its own quantity."""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def size_by_risk_pct(equity: Decimal, risk_pct: Decimal, max_loss_per_contract: Decimal) -> int:
    """floor(equity * risk_pct / max_loss_per_contract).

    Returns 0 -- never negative, never fractional -- if the account can't afford even one
    contract at this risk level. The caller (risk/manager.py) must treat 0 as "reject the
    trade," never as "trade the smallest size available."
    """
    if max_loss_per_contract <= 0:
        raise ValueError("max_loss_per_contract must be positive")
    if equity <= 0 or risk_pct <= 0:
        return 0
    budget = equity * risk_pct
    quantity = (budget / max_loss_per_contract).to_integral_value(rounding=ROUND_DOWN)
    return int(quantity)


def clamp_to_heat_room(quantity: int, max_loss_per_contract: Decimal, heat_room_remaining: Decimal) -> int:
    """Clamp `quantity` down further so quantity * max_loss_per_contract never exceeds the
    portfolio's remaining heat room. Never returns more than the input quantity, and never a
    negative number."""
    if quantity <= 0 or heat_room_remaining <= 0 or max_loss_per_contract <= 0:
        return 0
    max_by_heat = (heat_room_remaining / max_loss_per_contract).to_integral_value(rounding=ROUND_DOWN)
    return max(0, min(quantity, int(max_by_heat)))
