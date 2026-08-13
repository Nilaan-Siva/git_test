"""Core domain models: the shared vocabulary between strategy, risk, execution, and portfolio.

SIGN CONVENTION (read this before touching any price field in this file):

Every `*_price_per_share` field in this module is a signed **cash-flow-to-trader** amount,
matching how Interactive Brokers quotes a combo limit price:

    positive = the trader PAYS this many dollars per share (a debit)
    negative = the trader RECEIVES this many dollars per share (a credit)

This is the SAME convention whether the transaction is opening or closing a position. It is
deliberately NOT "positive = credit" (the common English phrasing for "credit spread") because
that convention flips sign between opening and closing and is a classic source of P&L bugs.

Consequences of this single convention:
  - Opening a credit spread (e.g. sell a put spread for $1.50 credit) -> entry_price = -1.50
  - Opening a debit spread (e.g. buy a call spread for $2.00 debit)   -> entry_price = +2.00
  - Profit per share, for ANY position, closed at `close_price_per_share`, is simply:

        profit_per_share = -(entry_price_per_share + close_price_per_share)

    because total cash flow to the trader is (entry_price + close_price); profit is the
    negative of a net cash outflow. See Position.unrealized_pnl / Position.close.

All strike/price fields use Decimal, never float, to avoid binary floating-point error in
money math. All `*_price_per_share` fields are PER SHARE (i.e. as options are quoted); multiply
by `multiplier` (contract size, almost always 100) to get per-contract dollars.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from optionsbot.core.enums import Action, OrderState, PositionStatus, Right, StrategyName


def _new_id() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OptionContract(BaseModel):
    """Identity of a single option contract. Immutable -- this describes *what*, never a
    position's state (quantity, side, P&L live on Leg/Position instead)."""

    model_config = {"frozen": True}

    underlying: str
    expiration: date
    strike: Decimal
    right: Right
    multiplier: int = 100
    occ_symbol: Optional[str] = None  # filled in by broker/data adapters when available

    @field_validator("underlying")
    @classmethod
    def _normalize_underlying(cls, v: str) -> str:
        v = v.upper().strip()
        if not v:
            raise ValueError("underlying must not be empty")
        return v

    @field_validator("strike")
    @classmethod
    def _positive_strike(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("strike must be positive")
        return v

    @field_validator("multiplier")
    @classmethod
    def _positive_multiplier(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("multiplier must be positive")
        return v

    def dte(self, as_of: date) -> int:
        """Calendar days to expiration (the standard options DTE convention)."""
        return (self.expiration - as_of).days

    def __str__(self) -> str:  # human-readable, e.g. "SPY 2026-09-18 450P"
        return f"{self.underlying} {self.expiration.isoformat()} {self.strike}{self.right.value[0]}"


class Leg(BaseModel):
    """One leg of a (possibly multi-leg) order or position."""

    contract: OptionContract
    action: Action
    ratio: int = Field(default=1, gt=0)  # relative quantity within the spread, e.g. a 1x2 ratio

    @property
    def is_short(self) -> bool:
        return self.action.is_short


class Spread(BaseModel):
    """A structure: one or more legs opened and closed together as a unit."""

    strategy: StrategyName
    underlying: str
    legs: list[Leg]

    @model_validator(mode="after")
    def _validate_legs(self) -> "Spread":
        if not self.legs:
            raise ValueError("a spread must have at least one leg")
        bad = [leg for leg in self.legs if leg.contract.underlying != self.underlying.upper()]
        if bad:
            raise ValueError(
                f"all legs must match spread.underlying={self.underlying!r}, "
                f"found mismatched leg(s): {[str(leg.contract) for leg in bad]}"
            )
        return self

    @property
    def width(self) -> Optional[Decimal]:
        """Strike width, per share, for a simple 2-leg vertical spread. None otherwise."""
        if len(self.legs) != 2:
            return None
        strikes = sorted(leg.contract.strike for leg in self.legs)
        return strikes[1] - strikes[0]

    def defined_risk(self, entry_price_per_share: Decimal) -> tuple[Decimal, Decimal]:
        """(max_loss, max_profit) per contract, in dollars, for a 2-leg vertical spread.

        `entry_price_per_share` follows this module's cash-flow-to-trader convention
        (negative = credit received, positive = debit paid). Compound structures such as
        iron condors are not a single Spread here -- build them as two verticals and sum
        each side's defined_risk (see strategy/iron_condor.py).
        """
        width = self.width
        if width is None:
            raise NotImplementedError(
                "defined_risk is only implemented for 2-leg verticals; compound structures "
                "(e.g. iron condors) should sum the defined_risk of their component verticals"
            )
        mult = Decimal(self.legs[0].contract.multiplier)
        if entry_price_per_share <= 0:  # opened for a net credit
            credit = -entry_price_per_share
            if credit > width:
                raise ValueError("credit received cannot exceed spread width")
            max_profit = credit * mult
            max_loss = (width - credit) * mult
        else:  # opened for a net debit
            debit = entry_price_per_share
            if debit > width:
                raise ValueError("debit paid cannot exceed spread width")
            max_loss = debit * mult
            max_profit = (width - debit) * mult
        return max_loss, max_profit

    def closing_legs(self) -> list[Leg]:
        """The mirrored legs that would close every leg of this spread."""
        return [
            Leg(contract=leg.contract, action=leg.action.closing_counterpart, ratio=leg.ratio)
            for leg in self.legs
        ]


class OrderIntent(BaseModel):
    """A proposed trade from a Strategy, not yet sized or risk-checked. The risk manager
    consumes this and either rejects it or turns it into a sized Order."""

    id: str = Field(default_factory=_new_id)
    strategy: StrategyName
    spread: Spread
    limit_price_per_share: Decimal  # opening price; see module docstring for sign convention
    max_loss_per_contract: Decimal
    max_profit_per_contract: Decimal
    rationale: str = ""
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("max_loss_per_contract")
    @classmethod
    def _non_negative_loss(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("max_loss_per_contract must be >= 0")
        return v


class Order(BaseModel):
    """A sized, risk-approved order submitted (or about to be submitted) to a broker."""

    id: str = Field(default_factory=_new_id)
    intent_id: str
    spread: Spread
    quantity: int = Field(gt=0)  # number of contracts (spreads), always positive
    limit_price_per_share: Decimal
    state: OrderState = OrderState.PENDING
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    avg_fill_price_per_share: Optional[Decimal] = None
    commission: Decimal = Decimal("0")
    broker_order_id: Optional[str] = None


class Fill(BaseModel):
    """A single fill event against a leg of an order. An order may accumulate several fills."""

    order_id: str
    leg: Leg
    quantity: int = Field(gt=0)
    price_per_share: Decimal
    timestamp: datetime
    commission: Decimal = Decimal("0")


class Position(BaseModel):
    """An open (or closed) holding: a Spread, a size, and its entry/exit economics."""

    id: str = Field(default_factory=_new_id)
    strategy: StrategyName
    spread: Spread
    quantity: int = Field(gt=0)
    entry_price_per_share: Decimal
    entry_date: date
    max_loss_per_contract: Decimal
    max_profit_per_contract: Decimal
    status: PositionStatus = PositionStatus.OPEN
    exit_price_per_share: Optional[Decimal] = None
    exit_date: Optional[date] = None
    close_reason: Optional[str] = None
    realized_pnl: Optional[Decimal] = None

    @property
    def total_max_loss(self) -> Decimal:
        return self.max_loss_per_contract * self.quantity

    @property
    def multiplier(self) -> int:
        return self.spread.legs[0].contract.multiplier

    def dte(self, as_of: date) -> int:
        """DTE of the nearest-expiring leg (relevant for the 21-DTE time stop)."""
        return min(leg.contract.dte(as_of) for leg in self.spread.legs)

    def unrealized_pnl(self, close_price_per_share: Decimal) -> Decimal:
        """P&L in dollars if closed now at `close_price_per_share` (same sign convention
        as entry_price_per_share -- see module docstring for the derivation)."""
        profit_per_share = -(self.entry_price_per_share + close_price_per_share)
        return profit_per_share * self.multiplier * self.quantity

    def close(self, exit_price_per_share: Decimal, exit_date: date, reason: str) -> "Position":
        self.exit_price_per_share = exit_price_per_share
        self.exit_date = exit_date
        self.close_reason = reason
        self.status = PositionStatus.CLOSED
        self.realized_pnl = self.unrealized_pnl(exit_price_per_share)
        return self


class AccountSnapshot(BaseModel):
    """A point-in-time account state, used by the risk manager and portfolio tracker."""

    timestamp: datetime
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
