"""Portfolio-level exposure aggregation: total risk-in-play ("heat") and position lookups by
underlying/bucket. Pure functions over a list of Positions -- no I/O, no hidden state."""
from __future__ import annotations

from decimal import Decimal

from optionsbot.core.models import Position


def total_heat(open_positions: list[Position]) -> Decimal:
    """Sum of max possible loss across all open positions, in dollars."""
    return sum((p.total_max_loss for p in open_positions), Decimal("0"))


def heat_pct_of_equity(open_positions: list[Position], equity: Decimal) -> Decimal:
    if equity <= 0:
        return Decimal("0")
    return total_heat(open_positions) / equity


def positions_for_underlying(open_positions: list[Position], underlying: str) -> list[Position]:
    return [p for p in open_positions if p.spread.underlying == underlying.upper()]


def positions_in_bucket(open_positions: list[Position], bucket_underlyings: list[str]) -> list[Position]:
    bucket_set = {u.upper() for u in bucket_underlyings}
    return [p for p in open_positions if p.spread.underlying in bucket_set]
