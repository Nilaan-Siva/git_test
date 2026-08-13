"""The backtest event loop.

The design constraint that matters more than anything else in this file: **the backtest runs the
real strategy objects and the real risk manager.** It does not reimplement entry logic, it does
not have its own copy of the 1% rule, and it does not know what a put credit spread is. It
supplies chains and a fill model; everything else is the same code that will run against IBKR in
Phase 5. If backtest and paper results diverge later, the cause is data or fills -- never two
different bots.

Day loop, in order:

  1. Fetch each underlying's chain. A missing chain is journalled and that ticker sits the day
     out; it never silently becomes a zero.
  2. Update the IV and price history that drives IV Rank and the trend filter.
  3. Mark open positions to their modelled *liquidation* price -- what it would cost to close
     right now, slippage included -- not to mid. Marking to mid flatters equity and understates
     drawdown.
  4. Manage exits (strategy/exits.py) and settle anything at expiration.
  5. Propose new entries, run each through risk.approve, and fill the approved ones.
  6. Record equity.

Two deliberate choices worth knowing about:

**Warmup.** IV Rank is meaningless without IV history, so the engine fetches chains for
`warmup_days` before `start` and trades none of them. Without this, the first months of every
backtest would either be tradeless or trade on a two-week IV sample pretending to be a rank.

**PortfolioState is rebuilt after every single approval.** risk/manager.approve is a pure
function with no memory (see its module docstring): two intents evaluated against the same stale
snapshot can both pass a limit that, combined, they breach. This engine is the reference
implementation of the caller-side contract that prevents that, and Phase 5's live router must do
the same thing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from optionsbot.backtest.metrics import BacktestMetrics, compute_metrics
from optionsbot.backtest.slippage import SlippageModel
from optionsbot.config.schema import RiskConfig, StrategiesConfig, UniverseConfig
from optionsbot.core.clock import session_bounds, trading_days_between
from optionsbot.core.models import AccountSnapshot, Chain, OptionQuote, Order, OrderIntent, PortfolioState, Position
from optionsbot.data.indicators import iv_rank
from optionsbot.data.providers.base import ChainProvider, DataUnavailableError
from optionsbot.risk.manager import approve
from optionsbot.strategy.base import Strategy, StrategyContext
from optionsbot.strategy.exits import evaluate_exit
from optionsbot.strategy.quoting import (
    ContractKey,
    atm_implied_volatility,
    combo_intrinsic_price,
    quote_index,
)


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    tickers: Sequence[str]
    starting_equity: Decimal = Decimal("10000")
    # Calendar days of chain data loaded before `start` to prime IV Rank and the trend filter.
    # These days are marked and managed but never traded. A year is the working default because
    # that is what the two slowest signals need: a 252-day IV Rank window and a 200-day trend
    # average. Shorten it and those filters spend the early part of the run either unavailable
    # or ranking against a sample too small to mean anything. Polygon's free tier carries two
    # years of history, so a year of warmup plus six months of backtest fits comfortably.
    warmup_days: int = 365
    iv_rank_lookback: int = 252
    iv_rank_min_history: int = 40
    earnings_calendar: Mapping[str, Sequence[date]] = field(default_factory=dict)
    vix_series: Mapping[date, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("backtest end must not be before start")
        if not self.tickers:
            raise ValueError("backtest needs at least one ticker")


@dataclass(frozen=True)
class JournalEntry:
    """One thing that happened, or was prevented from happening, on one day.

    Vetoes and filter rejections are recorded as carefully as fills. A backtest that reports
    "12 trades" without saying that 300 proposals were rejected for low IV Rank is hiding the
    part of the strategy that actually does the work.
    """

    day: date
    kind: str  # data_gap | no_trade | veto | entry | exit
    underlying: str
    detail: str
    strategy: Optional[str] = None

    @property
    def reason_code(self) -> str:
        """The leading `snake_case_code:` of `detail`, for aggregation."""
        return self.detail.split(":", 1)[0]


@dataclass
class BacktestResult:
    metrics: BacktestMetrics
    equity_curve: list[tuple[date, Decimal]]
    closed_positions: list[Position]
    open_positions: list[Position]
    journal: list[JournalEntry]
    slippage_label: str

    def entries_of_kind(self, kind: str) -> list[JournalEntry]:
        return [e for e in self.journal if e.kind == kind]

    def rejection_counts(self) -> dict[str, int]:
        """How often each distinct rejection reason fired, most common first.

        Reasons are truncated at the first colon, so "iv_rank_too_low: 18.2 < 30" and
        "iv_rank_too_low: 22.9 < 30" aggregate into one bucket.
        """
        counts: dict[str, int] = {}
        for entry in self.journal:
            if entry.kind not in ("no_trade", "veto"):
                continue
            counts[entry.reason_code] = counts.get(entry.reason_code, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


class BacktestEngine:
    def __init__(
        self,
        *,
        provider: ChainProvider,
        strategies: Sequence[Strategy],
        risk: RiskConfig,
        strategies_config: StrategiesConfig,
        universe: UniverseConfig,
        slippage: SlippageModel,
        config: BacktestConfig,
    ) -> None:
        self.provider = provider
        self.strategies = list(strategies)
        self.risk = risk
        self.strategies_config = strategies_config
        self.universe = universe
        self.slippage = slippage
        self.config = config

        self._cash = config.starting_equity
        self._high_water_mark = config.starting_equity
        self._consecutive_losses = 0
        self._open: list[Position] = []
        self._closed: list[Position] = []
        self._journal: list[JournalEntry] = []
        self._equity_curve: list[tuple[date, Decimal]] = []
        self._total_commission = Decimal("0")

        self._open_commission: dict[str, Decimal] = {}
        self._marks: dict[str, Decimal] = {}  # position id -> latest modelled close price
        self._closes: dict[str, list[float]] = {t: [] for t in config.tickers}
        self._iv_history: dict[str, list[float]] = {t: [] for t in config.tickers}
        self._realized_by_day: dict[date, Decimal] = {}
        self._realized_by_week: dict[tuple[int, int], Decimal] = {}

    # ---- public API ---------------------------------------------------------------------

    def run(self) -> BacktestResult:
        warmup_start = self.config.start - timedelta(days=self.config.warmup_days)
        for day in trading_days_between(warmup_start, self.config.end):
            chains = self._load_chains(day)
            self._update_history(day, chains)
            self._manage_open_positions(day, chains)
            if day >= self.config.start:
                self._propose_and_fill(day, chains)
                self._equity_curve.append((day, self._equity()))
                self._high_water_mark = max(self._high_water_mark, self._equity())

        self._settle_remaining(self.config.end)
        if self._equity_curve:
            self._equity_curve[-1] = (self._equity_curve[-1][0], self._equity())

        return BacktestResult(
            metrics=compute_metrics(
                label=f"{'/'.join(self.config.tickers)} {self.slippage.label}",
                equity_curve=self._equity_curve,
                closed_positions=self._closed,
                starting_equity=self.config.starting_equity,
                total_commission=self._total_commission,
            ),
            equity_curve=list(self._equity_curve),
            closed_positions=list(self._closed),
            open_positions=list(self._open),
            journal=list(self._journal),
            slippage_label=self.slippage.label,
        )

    # ---- day steps ----------------------------------------------------------------------

    def _load_chains(self, day: date) -> dict[str, Chain]:
        chains: dict[str, Chain] = {}
        for ticker in self.config.tickers:
            try:
                chains[ticker] = self.provider.get_chain(ticker, day)
            except DataUnavailableError as exc:
                self._log(day, "data_gap", ticker, f"no chain available: {exc}")
        return chains

    def _update_history(self, day: date, chains: Mapping[str, Chain]) -> None:
        for ticker, chain in chains.items():
            self._closes[ticker].append(float(chain.underlying_price))
            current_iv = atm_implied_volatility(chain)
            if current_iv is not None:
                history = self._iv_history[ticker]
                history.append(float(current_iv))
                # Keep the trailing window bounded so IV Rank measures the recent regime rather
                # than drifting toward "rank within all history ever seen".
                if len(history) > self.config.iv_rank_lookback:
                    del history[: len(history) - self.config.iv_rank_lookback]

    def _manage_open_positions(self, day: date, chains: Mapping[str, Chain]) -> None:
        for position in list(self._open):
            chain = chains.get(position.spread.underlying)
            if chain is None:
                continue  # no data today; the position rides, and is journalled as a data gap
            quotes = quote_index(chain)
            closing_legs = position.spread.closing_legs()

            if position.dte(day) <= 0:
                settlement = combo_intrinsic_price(closing_legs, chain.underlying_price)
                self._close_position(position, settlement, day, "expiration", charge_commission=settlement != 0)
                continue

            close_price = self.slippage.fill_price_per_share(closing_legs, quotes)
            if close_price is None:
                self._log(day, "data_gap", position.spread.underlying, f"position {position.id} unpriceable today")
                continue
            self._marks[position.id] = close_price

            signal = evaluate_exit(position, close_price_per_share=close_price, as_of=day, risk=self.risk)
            if signal is not None:
                self._close_position(position, close_price, day, signal.reason, charge_commission=True)

    def _propose_and_fill(self, day: date, chains: Mapping[str, Chain]) -> None:
        now = session_bounds(day)[1]
        for ticker, chain in chains.items():
            quotes = quote_index(chain)
            for strategy in self.strategies:
                ctx = self._context(chain, quotes, strategy)
                intents = strategy.propose(ctx)
                for note in ctx.notes:
                    self._log(day, "no_trade", ticker, note, strategy=strategy.name.value)

                for intent in intents:
                    # Rebuilt every iteration on purpose -- see this module's docstring.
                    decision = approve(intent, self._portfolio_state(now, day), self.risk, now=now)
                    if not decision.approved or decision.order is None:
                        self._log(day, "veto", ticker, decision.reason, strategy=strategy.name.value)
                        continue
                    self._open_position(decision.order, intent, day)

    # ---- position bookkeeping -------------------------------------------------------------

    def _open_position(self, order: Order, intent: OrderIntent, day: date) -> None:
        commission = self.slippage.commission(order.spread.legs, order.quantity)
        position = Position(
            strategy=intent.strategy,
            spread=order.spread,
            quantity=order.quantity,
            entry_price_per_share=order.limit_price_per_share,
            entry_date=day,
            max_loss_per_contract=intent.max_loss_per_contract,
            max_profit_per_contract=intent.max_profit_per_contract,
        )
        self._cash -= commission
        self._total_commission += commission
        self._open_commission[position.id] = commission
        self._marks[position.id] = order.limit_price_per_share
        self._open.append(position)
        self._log(
            day,
            "entry",
            position.spread.underlying,
            f"opened x{order.quantity} @ {order.limit_price_per_share}: {intent.rationale}",
            strategy=intent.strategy.value,
        )

    def _close_position(
        self, position: Position, close_price: Decimal, day: date, reason: str, *, charge_commission: bool
    ) -> None:
        gross = position.unrealized_pnl(close_price)
        commission = (
            self.slippage.commission(position.spread.legs, position.quantity) if charge_commission else Decimal("0")
        )
        position.close(close_price, day, reason)
        # Trade statistics must be net of costs, or expectancy is a number nobody can spend.
        position.realized_pnl = gross - self._open_commission.get(position.id, Decimal("0")) - commission

        self._cash += gross - commission
        self._total_commission += commission
        self._open.remove(position)
        self._closed.append(position)
        self._marks.pop(position.id, None)

        self._realized_by_day[day] = self._realized_by_day.get(day, Decimal("0")) + position.realized_pnl
        week = day.isocalendar()[:2]
        self._realized_by_week[week] = self._realized_by_week.get(week, Decimal("0")) + position.realized_pnl
        self._consecutive_losses = self._consecutive_losses + 1 if position.realized_pnl < 0 else 0

        self._log(
            day,
            "exit",
            position.spread.underlying,
            f"closed_{reason} @ {close_price} for {position.realized_pnl}",
            strategy=position.strategy.value,
        )

    def _settle_remaining(self, day: date) -> None:
        """Close anything still open when the backtest window ends.

        Marked to its last known liquidation price and labelled distinctly, so these positions
        can be excluded from expectancy if you'd rather score only trades the strategy actually
        finished on its own terms.
        """
        for position in list(self._open):
            mark = self._marks.get(position.id, position.entry_price_per_share)
            self._close_position(position, mark, day, "backtest_end", charge_commission=True)

    # ---- state -----------------------------------------------------------------------------

    def _unrealized(self) -> Decimal:
        total = Decimal("0")
        for position in self._open:
            mark = self._marks.get(position.id, position.entry_price_per_share)
            total += position.unrealized_pnl(mark)
        return total

    def _equity(self) -> Decimal:
        return self._cash + self._unrealized()

    def _portfolio_state(self, now: datetime, day: date) -> PortfolioState:
        equity = self._equity()
        return PortfolioState(
            account=AccountSnapshot(timestamp=now, equity=equity, cash=self._cash, buying_power=equity),
            open_positions=list(self._open),
            high_water_mark_equity=max(self._high_water_mark, equity),
            realized_pnl_today=self._realized_by_day.get(day, Decimal("0")),
            realized_pnl_this_week=self._realized_by_week.get(day.isocalendar()[:2], Decimal("0")),
            unrealized_pnl=self._unrealized(),
            consecutive_losses=self._consecutive_losses,
            # In a backtest the data is by definition current and the "broker" is always up.
            # Phase 5 supplies real timestamps here; the staleness and disconnect kill switches
            # are therefore exercised live rather than in backtest, which is the honest split.
            last_data_update=now,
            last_broker_heartbeat=now,
            vix_level=self.config.vix_series.get(day),
        )

    def _context(
        self, chain: Chain, quotes: Mapping[ContractKey, OptionQuote], strategy: Strategy
    ) -> StrategyContext:
        ticker = chain.underlying
        history = self._iv_history[ticker]
        current_iv = atm_implied_volatility(chain)
        ivr: Optional[Decimal] = None
        # `history` already includes today (appended in _update_history), so the trailing window
        # compared against excludes it -- IV Rank must not rank today's IV against itself.
        prior = history[:-1]
        if current_iv is not None and len(prior) >= self.config.iv_rank_min_history:
            rank = iv_rank(float(current_iv), prior)
            ivr = None if rank is None else Decimal(str(round(rank, 4)))

        return StrategyContext.build(
            chain=chain,
            params=strategy.params,
            universe=self.universe,
            price_combo=lambda legs: self.slippage.fill_price_per_share(legs, quotes),
            iv_rank=ivr,
            underlying_closes=self._closes[ticker],
            earnings_dates=self.config.earnings_calendar.get(ticker, ()),
            earnings_blackout_days=self.risk.earnings_blackout_days,
        )

    def _log(self, day: date, kind: str, underlying: str, detail: str, *, strategy: Optional[str] = None) -> None:
        self._journal.append(
            JournalEntry(day=day, kind=kind, underlying=underlying, detail=detail, strategy=strategy)
        )
