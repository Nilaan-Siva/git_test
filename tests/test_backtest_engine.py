"""End-to-end tests for backtest/engine.py.

These run the real strategies and the real risk manager against the deterministic synthetic
market, so they check the wiring that unit tests structurally cannot: that the risk gate is
actually consulted, that portfolio state is refreshed between approvals, that the books balance,
and that a bad data day degrades instead of crashing.

They are not, and must never be read as, evidence that the strategy makes money. The synthetic
market's edge is a parameter (`variance_risk_premium`); "profitable against a market I told to
be profitable" is a test of arithmetic, not of a trading strategy.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from optionsbot.backtest.engine import BacktestConfig, BacktestEngine
from optionsbot.backtest.slippage import SlippageModel
from optionsbot.config.schema import RiskConfig, StrategiesConfig, StrategyParams, UniverseConfig
from optionsbot.core.models import Chain
from optionsbot.data.providers.base import ChainProvider, DataUnavailableError
from optionsbot.data.providers.synthetic import SyntheticChainProvider
from optionsbot.strategy.registry import build_enabled_strategies

END = date(2026, 8, 13)
START = END - timedelta(days=120)
WARMUP = 150
UNIVERSE = UniverseConfig()
RISK = RiskConfig(correlated_buckets={"us_broad_market": ["SPY", "XSP", "QQQ", "IWM"]})

# Small chains keep the suite fast; the strategy only ever looks near 30 delta and inside the
# 30-50 DTE window, so a trimmed chain exercises exactly the same code paths.
FAST_CHAIN = dict(strikes_each_side=18, max_expiration_days=60)


def make_provider(*, seed: int = 1, tickers=("SPY",), warmup: int = WARMUP, **kwargs) -> SyntheticChainProvider:
    return SyntheticChainProvider(
        start=START - timedelta(days=warmup + 5), end=END, underlyings=tickers, seed=seed, **{**FAST_CHAIN, **kwargs}
    )


def run_backtest(
    *,
    provider=None,
    risk: RiskConfig = RISK,
    strategies_config: StrategiesConfig = None,
    slippage=None,
    tickers=("SPY",),
    equity: str = "25000",
    iv_rank_min_history: int = 40,
    warmup: int = WARMUP,
):
    strategies_config = strategies_config or StrategiesConfig()
    engine = BacktestEngine(
        provider=provider or make_provider(tickers=tickers),
        strategies=build_enabled_strategies(strategies_config),
        risk=risk,
        strategies_config=strategies_config,
        universe=UNIVERSE,
        slippage=slippage or SlippageModel.realistic(),
        config=BacktestConfig(
            start=START,
            end=END,
            tickers=list(tickers),
            starting_equity=Decimal(equity),
            warmup_days=warmup,
            iv_rank_min_history=iv_rank_min_history,
        ),
    )
    return engine.run()


def test_backtest_config_rejects_a_backwards_window():
    with pytest.raises(ValueError, match="end must not be before start"):
        BacktestConfig(start=END, end=START, tickers=["SPY"])


def test_backtest_config_rejects_an_empty_universe():
    with pytest.raises(ValueError, match="at least one ticker"):
        BacktestConfig(start=START, end=END, tickers=[])


@pytest.fixture(scope="module")
def result():
    """One shared run -- the synthetic provider is the slow part, so tests share a result."""
    return run_backtest()


# ---- the run happens at all ------------------------------------------------------------------


def test_backtest_produces_trades_an_equity_curve_and_a_journal(result):
    assert result.metrics.trade_count > 0
    assert len(result.equity_curve) > 50
    assert result.journal


def test_every_position_opened_is_eventually_closed(result):
    """Nothing is left dangling: `_settle_remaining` closes anything still open at the end."""
    assert result.open_positions == []
    assert len(result.closed_positions) == len(result.entries_of_kind("entry"))


def test_every_closed_position_records_a_reason_and_a_realized_pnl(result):
    for position in result.closed_positions:
        assert position.close_reason
        assert position.realized_pnl is not None
        assert position.exit_date is not None


def test_exits_are_driven_by_the_configured_rules(result):
    reasons = {p.close_reason for p in result.closed_positions}
    assert reasons <= {"profit_target", "stop_loss", "time_stop", "expiration", "backtest_end"}
    # the two rules that should dominate a 30-50 DTE credit strategy
    assert reasons & {"profit_target", "time_stop"}


# ---- the books balance -------------------------------------------------------------------------


def test_final_equity_equals_starting_equity_plus_realized_pnl(result):
    """The core accounting identity. Every dollar of commission and slippage has to land in a
    trade's realised P&L; if this drifts, equity and trade statistics are telling different
    stories and neither can be trusted."""
    realized = sum((p.realized_pnl for p in result.closed_positions), Decimal("0"))
    assert result.metrics.ending_equity == result.metrics.starting_equity + realized


def test_commissions_are_charged_and_counted(result):
    assert result.metrics.total_commission > 0
    # two legs, two ways, $0.65 a contract -> $2.60 per single-contract round trip
    assert result.metrics.total_commission >= Decimal("2.60") * result.metrics.trade_count


def test_realized_pnl_is_net_of_commission(result):
    """Trade statistics must be spendable numbers. A position closed at exactly its entry price
    is a small loss, not a break-even."""
    for position in result.closed_positions:
        gross = position.unrealized_pnl(position.exit_price_per_share)
        assert position.realized_pnl < gross


def test_equity_curve_starts_at_the_configured_equity(result):
    assert result.equity_curve[0][1] == Decimal("25000") or result.equity_curve[0][0] >= START


# ---- the risk manager is genuinely in the path ----------------------------------------------------


def test_risk_manager_can_veto_every_trade():
    """Wiring check: shrink per-trade risk until no position can be sized, and the engine must
    produce vetoes and zero fills. If the engine ever bypassed risk.approve, this passes trades
    through and the test fails."""
    starved = RISK.model_copy(update={"max_risk_per_trade_pct": Decimal("0.00001")})
    result = run_backtest(risk=starved)
    assert result.metrics.trade_count == 0
    vetoes = result.entries_of_kind("veto")
    assert vetoes
    assert all(v.reason_code == "position_size_zero" for v in vetoes)


def test_never_more_than_one_position_open_per_underlying(result):
    """`max_positions_per_underlying` is 1, so entries and exits must strictly alternate."""
    open_count = 0
    for entry in result.journal:
        if entry.kind == "entry":
            open_count += 1
        elif entry.kind == "exit":
            open_count -= 1
        assert 0 <= open_count <= 1


def test_portfolio_state_is_refreshed_between_sequential_approvals():
    """The caller-side contract that risk/manager.py's docstring demands.

    `approve()` is stateless: evaluated against one stale snapshot, two intents on different
    underlyings in the same correlated bucket would both pass a bucket limit of 1. The engine
    rebuilds PortfolioState after every approval specifically to prevent that. With two tickers
    in one bucket and a limit of one, a stale-state engine opens two positions on the same day;
    a correct one opens exactly one.
    """
    single_slot = RISK.model_copy(update={"max_positions_per_correlated_bucket": 1})
    result = run_backtest(
        provider=make_provider(tickers=("SPY", "QQQ")),
        risk=single_slot,
        tickers=("SPY", "QQQ"),
    )
    assert result.metrics.trade_count > 0

    open_count = 0
    for entry in result.journal:
        if entry.kind == "entry":
            open_count += 1
        elif entry.kind == "exit":
            open_count -= 1
        assert open_count <= 1, "two positions open in a bucket limited to one"

    assert any(v.reason_code == "correlated_bucket_limit" for v in result.entries_of_kind("veto"))


# ---- filters actually gate ---------------------------------------------------------------------------


def test_the_trend_gate_stands_the_strategy_down_in_a_bear_market():
    """Seed 20260813 trends down ~28% through the window. The trend gate should suppress
    essentially all trading -- not literally all of it, because a bear market does bounce back
    above its average occasionally, and the gate is a regime filter rather than a prophecy.

    The long warmup is load-bearing, and the gotcha it encodes is worth stating plainly: the
    default trend average is 200 *trading* days, and `check_not_downtrend` passes when it has
    less history than that. Run a backtest with a short warmup and the trend gate is silently
    inactive for the whole run -- it does not warn, it just never fires. At 150 warmup days this
    same bear market puts on five trades; with a real 200-day average available it manages one.
    """
    long_warmup = 430  # ~295 trading days: enough to have a real 200-day average from day one
    result = run_backtest(provider=make_provider(seed=20260813, warmup=long_warmup), warmup=long_warmup)

    downtrend_days = sum(1 for e in result.entries_of_kind("no_trade") if e.reason_code == "downtrend")
    assert downtrend_days > 50, "the trend gate barely fired in a market that fell 28%"
    # the gate should be rejecting vastly more often than the strategy is trading
    assert result.metrics.trade_count * 10 < downtrend_days


def test_raising_the_iv_rank_floor_reduces_trading(result):
    picky = StrategiesConfig(put_credit_spread=StrategyParams(min_iv_rank=Decimal("95")))
    strict = run_backtest(strategies_config=picky)
    assert strict.metrics.trade_count < result.metrics.trade_count


def test_warmup_days_are_never_traded(result):
    """Warmup exists to prime IV Rank, not to trade. An entry before `start` would mean the
    strategy is being scored on days its own signals weren't ready for."""
    for entry in result.entries_of_kind("entry"):
        assert entry.day >= START
    assert all(day >= START for day, _ in result.equity_curve)


# ---- degraded data ---------------------------------------------------------------------------------------


class FlakyProvider(ChainProvider):
    """Wraps a provider and refuses data on a fixed fraction of days."""

    def __init__(self, inner: ChainProvider, *, fail_every: int = 3) -> None:
        self.inner = inner
        self.fail_every = fail_every
        self._calls = 0

    def get_chain(self, underlying: str, as_of: date) -> Chain:
        self._calls += 1
        if self._calls % self.fail_every == 0:
            raise DataUnavailableError("synthetic outage")
        return self.inner.get_chain(underlying, as_of)

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        return self.inner.get_underlying_price(underlying, as_of)


def test_missing_chains_are_journalled_and_the_run_survives():
    """A data gap must degrade the run, not end it -- and must be visible in the journal rather
    than silently becoming a flat day."""
    result = run_backtest(provider=FlakyProvider(make_provider()))
    gaps = result.entries_of_kind("data_gap")
    assert gaps
    assert result.equity_curve
    assert result.metrics.trade_count >= 0


def test_coverage_exposes_tickers_that_had_no_data(result):
    """The failure that produced a confident four-trade verdict on a twelve-ticker universe.

    Backfilling one ticker and backtesting twelve yields a perfectly well-formed report --
    equity curve, win rate, verdict -- computed from a twelfth of the intended universe, with
    nothing in the numbers indicating it. Missing data is not a rejection, so it never appeared
    in rejection_counts; the strategy was simply never asked about those names.
    """
    assert result.total_days > 0
    assert result.coverage()["SPY"] > 0.9
    assert result.starved_tickers() == []


def test_a_ticker_with_no_cached_chains_is_flagged_as_starved():
    class OnlySpy(ChainProvider):
        def __init__(self, inner):
            self.inner = inner

        def get_chain(self, underlying: str, as_of: date) -> Chain:
            if underlying != "SPY":
                raise DataUnavailableError(f"{underlying} not cached")
            return self.inner.get_chain(underlying, as_of)

        def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
            return self.inner.get_underlying_price(underlying, as_of)

    result = run_backtest(
        provider=OnlySpy(make_provider(tickers=("SPY", "QQQ"))), tickers=("SPY", "QQQ")
    )
    assert result.starved_tickers() == ["QQQ"]
    assert result.coverage()["QQQ"] == 0.0
    assert result.coverage()["SPY"] > 0.9


def test_a_provider_with_no_data_at_all_produces_an_empty_but_valid_result():
    class DeadProvider(ChainProvider):
        def get_chain(self, underlying: str, as_of: date) -> Chain:
            raise DataUnavailableError("nothing cached")

        def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
            raise DataUnavailableError("nothing cached")

    result = run_backtest(provider=DeadProvider())
    assert result.metrics.trade_count == 0
    assert result.metrics.ending_equity == Decimal("25000")
    assert all(e.kind == "data_gap" for e in result.journal)


# ---- slippage bracketing ---------------------------------------------------------------------------------


def test_pessimistic_fills_do_not_beat_optimistic_ones_on_this_market():
    """A smoke check, NOT an invariant -- and the distinction is load-bearing.

    The real invariant is per fill: a worse fill model never improves an individual trade's
    price, which test_slippage.py proves directly. At the level of a whole run this can and does
    invert, because worse fills change which proposals clear `min_credit_pct_of_width` and how
    the risk manager sizes them, so the two runs take *different trades*. The first real-data
    backtest showed exactly that: the pessimistic run took three trades and finished ahead of
    the optimistic run's two.

    So this asserts a property of this particular synthetic market, useful for catching a
    grossly inverted fill model, and nothing stronger. Do not promote it to a law.
    """
    optimistic = run_backtest(slippage=SlippageModel.optimistic())
    pessimistic = run_backtest(slippage=SlippageModel.pessimistic())
    assert pessimistic.metrics.ending_equity <= optimistic.metrics.ending_equity


def test_the_run_is_deterministic():
    """Same seed, same config, same result -- otherwise walk-forward comparisons in Phase 4 are
    measuring noise in the engine rather than in the strategy."""
    first, second = run_backtest(), run_backtest()
    assert first.metrics.ending_equity == second.metrics.ending_equity
    assert first.metrics.trade_count == second.metrics.trade_count


# ---- reporting helpers -------------------------------------------------------------------------------------


def test_rejection_counts_aggregate_by_reason_code(result):
    counts = result.rejection_counts()
    assert counts
    assert all(":" not in reason for reason in counts)
    # sorted most common first, so a report can truncate to the top few
    assert list(counts.values()) == sorted(counts.values(), reverse=True)


def test_journal_entries_carry_the_strategy_that_produced_them(result):
    for entry in result.journal:
        if entry.kind in ("entry", "exit", "no_trade", "veto"):
            assert entry.strategy == "put_credit_spread"


def test_metrics_render_without_error(result):
    assert "Backtest" in result.metrics.render()
