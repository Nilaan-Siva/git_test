"""What a given account size can actually trade.

This exists because the most consequential number in the whole project is not a strategy
parameter -- it is how much money is in the account. Everything else follows from it: how wide a
spread can be sized inside the 1% rule, how badly fixed commissions eat the credit, how many
positions can run at once, and therefore how often the bot trades at all.

The arithmetic is short and unforgiving:

    max loss per contract  ~=  (width x 100) - credit,   credit ~= 22% of width
    contracts affordable   =   floor(equity x risk_pct / max loss per contract)
    commission drag        =   $2.60 round trip / credit per contract

The trap it exists to prevent: narrow spreads *look* like the small-account option, because they
risk fewer dollars. They are the opposite. Commission is a flat fee, so halving the width halves
the credit while the fee stays put -- a 1-wide spread hands ~11% of every winner to the broker
before slippage, which is plausibly more than the strategy's entire edge. Trading smaller does
not make a small account viable; it makes the fees proportionally worse.

Credit is estimated at 22% of width, measured from live SPY chains on 2026-08-13 (a 3-wide
30-delta spread quoted $0.68 against $3.00 of width). It moves with volatility, so treat these
as the shape of the tradeoff rather than a quote.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

# Fraction of a spread's width typically received as credit at ~30 delta, 30-50 DTE.
CREDIT_FRACTION_OF_WIDTH = Decimal("0.22")
IBKR_COMMISSION_PER_CONTRACT = Decimal("0.65")
LEGS_PER_VERTICAL = 2


@dataclass(frozen=True)
class WidthOption:
    """What one spread width means for one account."""

    width: Decimal
    credit_per_contract: Decimal
    max_loss_per_contract: Decimal
    contracts_affordable: int
    commission_round_trip: Decimal
    commission_drag_pct: Decimal

    @property
    def tradable(self) -> bool:
        return self.contracts_affordable >= 1

    @property
    def verdict(self) -> str:
        if not self.tradable:
            return "cannot size a single contract"
        if self.commission_drag_pct > Decimal("10"):
            return "fees eat most of the edge"
        if self.commission_drag_pct > Decimal("5"):
            return "workable but fee-heavy"
        return "healthy"


def evaluate_width(equity: Decimal, risk_pct: Decimal, width: Decimal) -> WidthOption:
    credit = (width * CREDIT_FRACTION_OF_WIDTH * 100).quantize(Decimal("0.01"))
    max_loss = (width * 100 - credit).quantize(Decimal("0.01"))
    budget = equity * risk_pct
    contracts = int(budget / max_loss) if max_loss > 0 else 0
    commission = IBKR_COMMISSION_PER_CONTRACT * LEGS_PER_VERTICAL * 2  # open + close
    drag = (commission / credit * 100).quantize(Decimal("0.1")) if credit > 0 else Decimal("0")
    return WidthOption(
        width=width,
        credit_per_contract=credit,
        max_loss_per_contract=max_loss,
        contracts_affordable=contracts,
        commission_round_trip=commission,
        commission_drag_pct=drag,
    )


@dataclass(frozen=True)
class CapitalAssessment:
    equity: Decimal
    risk_pct: Decimal
    heat_pct: Decimal
    options: list[WidthOption]

    @property
    def concurrent_positions(self) -> int:
        """How many positions can be open at once before the portfolio heat cap binds."""
        if self.risk_pct <= 0:
            return 0
        return int(self.heat_pct / self.risk_pct)

    @property
    def best(self) -> Optional[WidthOption]:
        """The widest tradable spread -- widest is best, because commission drag falls as width
        rises and the risk is capped either way by the 1% rule."""
        tradable = [o for o in self.options if o.tradable]
        return max(tradable, key=lambda o: o.width) if tradable else None

    @property
    def can_trade(self) -> bool:
        return self.best is not None

    def estimated_trades_per_year(self, *, avg_hold_days: int = 15, trading_days: int = 252) -> int:
        """Slots divided by how long each is occupied. Signal availability is not the constraint
        once the universe has a dozen names -- position slots are."""
        if not self.can_trade or avg_hold_days <= 0:
            return 0
        return int(self.concurrent_positions * trading_days / avg_hold_days)


def assess(
    equity: Decimal,
    *,
    risk_pct: Decimal = Decimal("0.01"),
    heat_pct: Decimal = Decimal("0.06"),
    widths: Sequence[Decimal] = (Decimal("1"), Decimal("2"), Decimal("3"), Decimal("5"), Decimal("10")),
) -> CapitalAssessment:
    return CapitalAssessment(
        equity=equity,
        risk_pct=risk_pct,
        heat_pct=heat_pct,
        options=[evaluate_width(equity, risk_pct, w) for w in widths],
    )


def render(assessment: CapitalAssessment) -> str:
    """A plain-language answer to 'what can this account actually do?'"""
    eq = assessment.equity
    lines = [
        f"=== What ${eq:,.0f} can trade ===",
        f"Risk per trade: {assessment.risk_pct * 100:.1f}% = ${eq * assessment.risk_pct:,.2f}",
        f"Positions at once (before the {assessment.heat_pct * 100:.0f}% total-risk cap): "
        f"{assessment.concurrent_positions}",
        "",
        f"{'Width':>6} {'Credit':>9} {'At risk':>9} {'Contracts':>10} {'Fees':>7}  Verdict",
    ]
    for o in assessment.options:
        lines.append(
            f"{o.width:>5}pt {o.credit_per_contract:>8,.2f} {o.max_loss_per_contract:>9,.2f} "
            f"{o.contracts_affordable:>10} {o.commission_drag_pct:>6.1f}%  {o.verdict}"
        )
    lines.append("")

    best = assessment.best
    if best is None:
        cheapest = min(assessment.options, key=lambda o: o.max_loss_per_contract)
        needed = (cheapest.max_loss_per_contract / assessment.risk_pct).quantize(Decimal("1"))
        lines += [
            "VERDICT: this account cannot trade defined-risk spreads at all.",
            f"The narrowest spread available risks ${cheapest.max_loss_per_contract:,.2f} per contract, and "
            f"{assessment.risk_pct * 100:.1f}% of ${eq:,.0f} is only ${eq * assessment.risk_pct:,.2f}.",
            f"You would need about ${needed:,.0f} for one contract of the narrowest spread -- and that "
            f"width hands ~{cheapest.commission_drag_pct:.0f}% of every winner to commissions, so it is a floor, "
            "not a target.",
            "",
            "The bot will refuse every trade at this size. That is the correct behaviour, not a bug.",
        ]
    else:
        lines += [
            f"VERDICT: tradable. Best width is {best.width}pt -- ${best.max_loss_per_contract:,.2f} at risk, "
            f"${best.credit_per_contract:,.2f} credit, {best.commission_drag_pct:.1f}% to fees.",
            f"Roughly {assessment.estimated_trades_per_year()} trades a year with a full 12-ticker universe "
            f"(~{assessment.estimated_trades_per_year() / 52:.1f} per week).",
        ]
        if best.commission_drag_pct > Decimal("5"):
            lines.append(
                "Fees are heavy at this size. Every extra dollar of capital widens the spread you can "
                "afford and shrinks that percentage."
            )
    return "\n".join(lines)
