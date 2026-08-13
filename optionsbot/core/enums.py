"""Shared enums for the trading domain. String-valued so they serialize cleanly to JSON/SQLite."""
from __future__ import annotations

from enum import Enum


class Right(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class Action(str, Enum):
    """Standard OCC order actions. Open actions establish a leg; close actions unwind it."""

    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_OPEN = "SELL_TO_OPEN"
    BUY_TO_CLOSE = "BUY_TO_CLOSE"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"

    @property
    def is_opening(self) -> bool:
        return self in (Action.BUY_TO_OPEN, Action.SELL_TO_OPEN)

    @property
    def is_short(self) -> bool:
        return self in (Action.SELL_TO_OPEN, Action.BUY_TO_CLOSE)

    @property
    def closing_counterpart(self) -> "Action":
        """The action that unwinds this leg (e.g. SELL_TO_OPEN -> BUY_TO_CLOSE)."""
        return {
            Action.BUY_TO_OPEN: Action.SELL_TO_CLOSE,
            Action.SELL_TO_OPEN: Action.BUY_TO_CLOSE,
        }[self]


class OrderState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ASSIGNED = "ASSIGNED"
    EXPIRED = "EXPIRED"


class Regime(str, Enum):
    """Coarse volatility regime, driven by VIX level/trend (see learning/regime.py)."""

    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    ELEVATED_VOL = "ELEVATED_VOL"
    CRISIS = "CRISIS"


class StrategyName(str, Enum):
    PUT_CREDIT_SPREAD = "put_credit_spread"
    IRON_CONDOR = "iron_condor"
    WHEEL = "wheel"
    COVERED_CALL = "covered_call"
