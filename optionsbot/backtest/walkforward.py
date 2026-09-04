"""Walk-forward validation: the overfitting firewall.

A backtest tuned on the same data it is judged on proves nothing. Adjust enough knobs against
one stretch of history and any strategy looks brilliant on it, because the tuning is fitting
that specific past rather than anything that generalises. Walk-forward is the discipline that
catches it: split history into consecutive folds, choose parameters using only the *earlier*
part of each fold, then score those parameters on the *later* part, which the search never saw.

Three properties this implementation insists on, each because the usual shortcut lies:

**Parameters are chosen per fold, never once globally.** Picking one set of parameters across
all history and then reporting "out-of-sample" results is the most common way to fool yourself,
because the choice already used the future.

**An untuned baseline runs alongside every fold.** Tuning has to beat *not tuning* on unseen
data, or it is noise wearing a decimal point. A walk-forward run that reports only tuned results
cannot distinguish a real edge from a parameter search rediscovering randomness -- so every fold
also runs the shipped defaults on the same out-of-sample window, and the comparison is reported
whether or not it is flattering.

**Thin folds are excluded and counted, not averaged in.** At roughly fifty trades a year, a
three-month window holds about a dozen trades, and a "best" parameter set chosen from twelve
trades is a coin flip with extra steps. Folds below `min_trades` are reported as unjudgeable
rather than quietly diluting the average.

What may be tuned is strictly limited to strategies.yaml -- entry filters, strike selection,
DTE. Nothing here can touch risk.yaml. The 1% rule, the heat cap and the kill switches are not
parameters to be optimised; a system permitted to relax its own risk limits when results
disappoint will eventually relax all of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from itertools import product
from typing import Callable, Mapping, Optional, Sequence

from optionsbot.backtest.metrics import BacktestMetrics
from optionsbot.config.schema import StrategyParams

# Run one backtest for a parameter set over one window.
BacktestRunner = Callable[[StrategyParams, date, date], BacktestMetrics]

# Only these may vary. Anything absent is not tunable, and nothing in risk.yaml appears at all.
TUNABLE_FIELDS = frozenset(
    {
        "min_iv_rank",
        "short_delta_target",
        "short_delta_tolerance",
        "target_dte_min",
        "target_dte_max",
        "spread_width",
        "min_credit_pct_of_width",
        "trend_sma_period",
    }
)

DEFAULT_GRID: dict[str, tuple] = {
    "min_iv_rank": (Decimal("20"), Decimal("30"), Decimal("40")),
    "short_delta_target": (Decimal("0.20"), Decimal("0.30")),
}


@dataclass(frozen=True)
class Fold:
    index: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date

    def __str__(self) -> str:
        return f"fold {self.index}: tune {self.is_start}..{self.is_end}, test {self.oos_start}..{self.oos_end}"


def make_folds(
    start: date,
    end: date,
    *,
    in_sample_days: int = 180,
    out_of_sample_days: int = 90,
    anchored: bool = False,
) -> list[Fold]:
    """Consecutive tune/test windows across [start, end].

    Rolling by default: each fold tunes on a fixed-length recent window, which matches how the
    bot would actually be re-tuned in production. `anchored=True` grows the tuning window from
    `start` instead, using all history to date -- steadier parameter choices, but slower to
    notice that the market has changed character.

    Out-of-sample windows never overlap, so no test day is ever scored twice.
    """
    if end <= start:
        raise ValueError("end must be after start")
    if in_sample_days <= 0 or out_of_sample_days <= 0:
        raise ValueError("window lengths must be positive")

    folds: list[Fold] = []
    is_end = start + timedelta(days=in_sample_days)
    index = 1
    while is_end + timedelta(days=out_of_sample_days) <= end:
        oos_end = is_end + timedelta(days=out_of_sample_days)
        folds.append(
            Fold(
                index=index,
                is_start=start if anchored else is_end - timedelta(days=in_sample_days),
                is_end=is_end,
                oos_start=is_end,
                oos_end=oos_end,
            )
        )
        is_end = oos_end
        index += 1
    return folds


def expand_grid(base: StrategyParams, grid: Mapping[str, Sequence]) -> list[StrategyParams]:
    """Every combination of the grid, applied to `base`.

    Rejects any field outside TUNABLE_FIELDS. That guard is the point: it is the boundary
    between "the strategy adapts" and "the safety limits adapt", and it should fail loudly
    rather than silently permit a risk parameter into a search that optimises for return.
    """
    unknown = set(grid) - TUNABLE_FIELDS
    if unknown:
        raise ValueError(
            f"not tunable: {sorted(unknown)}. Only strategies.yaml fields may be searched; "
            "risk limits are never optimisation targets."
        )
    if not grid:
        return [base]

    names = sorted(grid)
    out = []
    for combo in product(*(grid[n] for n in names)):
        candidate = base.model_copy(update=dict(zip(names, combo)))
        if candidate.target_dte_min > candidate.target_dte_max:
            continue  # nonsensical combination; skip rather than let the engine find nothing
        out.append(candidate)
    return out


def describe(params: StrategyParams, base: StrategyParams) -> str:
    """Just the fields that differ from the shipped defaults."""
    diffs = [
        f"{name}={getattr(params, name)}"
        for name in sorted(TUNABLE_FIELDS)
        if getattr(params, name) != getattr(base, name)
    ]
    return ", ".join(diffs) if diffs else "defaults"


@dataclass(frozen=True)
class FoldResult:
    fold: Fold
    chosen: StrategyParams
    chosen_description: str
    in_sample: Optional[BacktestMetrics]
    out_of_sample: Optional[BacktestMetrics]
    baseline: Optional[BacktestMetrics]
    candidates_evaluated: int
    min_trades: int

    @property
    def judgeable(self) -> bool:
        """Enough out-of-sample trades for the result to carry information."""
        return self.out_of_sample is not None and self.out_of_sample.trade_count >= self.min_trades

    @property
    def used_defaults(self) -> bool:
        """True when the search settled on the shipped parameters, so there is nothing to
        compare a baseline against -- tuning cannot be judged worthless if it never happened."""
        return self.chosen_description == "defaults"

    @property
    def degradation_pct(self) -> Optional[float]:
        """How much worse out-of-sample expectancy is than in-sample, as a percentage.

        Positive means the strategy did worse on unseen data, which is normal and expected --
        some degradation always occurs. It is the *size* that matters. Above ~30% suggests the
        tuning was fitting noise. Negative means it did better out-of-sample, which is luck, not
        skill, and should not be read as a good sign.
        """
        if self.in_sample is None or self.out_of_sample is None:
            return None
        is_exp, oos_exp = self.in_sample.expectancy_per_trade, self.out_of_sample.expectancy_per_trade
        if is_exp is None or oos_exp is None or is_exp <= 0:
            return None
        return float((is_exp - oos_exp) / is_exp * 100)

    @property
    def tuning_gain(self) -> Optional[Decimal]:
        """Out-of-sample expectancy from tuning, minus the same from shipped defaults.

        The number that says whether the parameter search earned its keep. At or below zero,
        tuning is noise and the defaults should simply be used.
        """
        if self.out_of_sample is None or self.baseline is None:
            return None
        tuned, base = self.out_of_sample.expectancy_per_trade, self.baseline.expectancy_per_trade
        if tuned is None or base is None:
            return None
        return tuned - base


@dataclass(frozen=True)
class WalkForwardResult:
    folds: list[FoldResult]
    min_trades: int
    grid_size: int

    @property
    def judgeable(self) -> list[FoldResult]:
        return [f for f in self.folds if f.judgeable]

    @property
    def skipped(self) -> list[FoldResult]:
        return [f for f in self.folds if not f.judgeable]

    @property
    def tuning_attempted(self) -> bool:
        """Whether any judgeable fold actually departed from the defaults.

        Without this, a run with an empty grid compares the defaults against themselves, finds
        no improvement, and declares tuning worthless -- a confident verdict about something
        that never took place.
        """
        return any(not f.used_defaults for f in self.judgeable)

    @property
    def total_oos_trades(self) -> int:
        return sum(f.out_of_sample.trade_count for f in self.judgeable if f.out_of_sample)

    def _mean(self, values: list[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    @property
    def mean_degradation_pct(self) -> Optional[float]:
        return self._mean([d for f in self.judgeable if (d := f.degradation_pct) is not None])

    @property
    def mean_oos_expectancy(self) -> Optional[Decimal]:
        vals = [f.out_of_sample.expectancy_per_trade for f in self.judgeable
                if f.out_of_sample and f.out_of_sample.expectancy_per_trade is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def mean_baseline_expectancy(self) -> Optional[Decimal]:
        vals = [f.baseline.expectancy_per_trade for f in self.judgeable
                if f.baseline and f.baseline.expectancy_per_trade is not None]
        return sum(vals) / len(vals) if vals else None

    def verdict(self) -> tuple[str, str]:
        """(code, explanation). Ordered so the most fundamental objection wins."""
        if not self.judgeable:
            return (
                "INCONCLUSIVE",
                f"no fold reached {self.min_trades} out-of-sample trades. This says nothing about "
                "the strategy -- there is not enough data to say anything. Widen the window or "
                "the universe and rerun.",
            )
        oos = self.mean_oos_expectancy
        if oos is None:
            return "INCONCLUSIVE", "no out-of-sample expectancy could be computed."
        if oos <= 0:
            return (
                "FAIL",
                f"out-of-sample expectancy is {oos:+.2f} per trade. Tuned on past data, the "
                "strategy still loses money on data it has not seen.",
            )

        baseline = self.mean_baseline_expectancy
        if self.tuning_attempted and baseline is not None and oos <= baseline:
            return (
                "TUNING WORTHLESS",
                f"tuned parameters earn {oos:+.2f} per trade out-of-sample against {baseline:+.2f} "
                "for the untouched defaults. The search is fitting noise -- ship the defaults and "
                "drop the tuning step.",
            )

        degradation = self.mean_degradation_pct
        if degradation is not None and degradation > 30:
            return (
                "OVERFIT",
                f"out-of-sample expectancy is {degradation:.0f}% below in-sample. Beyond about "
                "30%, the tuning is fitting the past rather than finding something durable.",
            )
        return (
            "PASS",
            f"out-of-sample expectancy {oos:+.2f} per trade across {self.total_oos_trades} trades, "
            f"degradation {degradation:.0f}%. Holds up on data it was not tuned on."
            if degradation is not None
            else f"out-of-sample expectancy {oos:+.2f} per trade across {self.total_oos_trades} trades.",
        )

    def render(self) -> str:
        lines = [
            "=== Walk-forward validation ===",
            f"Folds: {len(self.folds)} ({len(self.judgeable)} judgeable, {len(self.skipped)} too thin)",
            f"Parameter combinations searched per fold: {self.grid_size}",
            f"Minimum trades for a fold to count: {self.min_trades}",
            "",
            f"{'Fold':>4} {'Test window':<25} {'Trades':>7} {'IS exp':>9} {'OOS exp':>9} {'Drop':>7} "
            f"{'vs base':>9}  Chosen",
        ]
        towards = ".."
        for f in self.folds:
            oos_trades = f.out_of_sample.trade_count if f.out_of_sample else 0
            is_exp = f.in_sample.expectancy_per_trade if f.in_sample else None
            oos_exp = f.out_of_sample.expectancy_per_trade if f.out_of_sample else None
            drop = f.degradation_pct
            gain = f.tuning_gain
            flag = "" if f.judgeable else "  (too thin to judge)"
            lines.append(
                # str() first: a width spec on a date object is read as a strftime pattern, so
                # f"{some_date:<12}" silently renders the literal text "<12".
                f"{f.fold.index:>4} {str(f.fold.oos_start) + towards + str(f.fold.oos_end):<25} {oos_trades:>7} "
                f"{('n/a' if is_exp is None else f'{is_exp:+.2f}'):>9} "
                f"{('n/a' if oos_exp is None else f'{oos_exp:+.2f}'):>9} "
                f"{('n/a' if drop is None else f'{drop:+.0f}%'):>7} "
                f"{('n/a' if gain is None else f'{gain:+.2f}'):>9}  {f.chosen_description}{flag}"
            )

        code, why = self.verdict()
        lines += ["", f"Mean out-of-sample expectancy: "
                      f"{'n/a' if self.mean_oos_expectancy is None else f'{self.mean_oos_expectancy:+.2f}'} per trade"]
        if self.mean_baseline_expectancy is not None:
            lines.append(f"Same, with defaults and no tuning:  {self.mean_baseline_expectancy:+.2f} per trade")
        lines += ["", f"VERDICT: {code}", f"  {why}"]
        if self.skipped:
            lines.append(
                f"  Note: {len(self.skipped)} fold(s) had too few trades to judge and were excluded "
                "rather than averaged in."
            )
        return "\n".join(lines)


def _score(metrics: Optional[BacktestMetrics], min_trades: int) -> Optional[Decimal]:
    """Selection score: expectancy per trade, but only if there were enough trades.

    Deliberately not total return -- that rewards a parameter set that got lucky on one big
    winner. Expectancy over a decent count is the closest available proxy for a repeatable edge.
    """
    if metrics is None or metrics.trade_count < min_trades or metrics.expectancy_per_trade is None:
        return None
    return metrics.expectancy_per_trade


def walk_forward(
    runner: BacktestRunner,
    *,
    folds: Sequence[Fold],
    base_params: StrategyParams,
    grid: Optional[Mapping[str, Sequence]] = None,
    min_trades: int = 10,
    min_is_trades: int = 10,
) -> WalkForwardResult:
    """Tune on each fold's in-sample window, score on its out-of-sample window.

    `runner` does the actual backtesting, injected so this logic is testable without market
    data and so the same code drives cached, synthetic, or future live-replay runs.
    """
    candidates = expand_grid(base_params, grid if grid is not None else DEFAULT_GRID)
    results: list[FoldResult] = []

    for fold in folds:
        best_params, best_score, best_is = base_params, None, None
        for candidate in candidates:
            metrics = runner(candidate, fold.is_start, fold.is_end)
            score = _score(metrics, min_is_trades)
            if score is not None and (best_score is None or score > best_score):
                best_params, best_score, best_is = candidate, score, metrics

        if best_is is None:
            # No candidate traded enough in-sample to justify a choice. Falling back to the
            # defaults is the honest move: pretending a 3-trade winner is "the best parameters"
            # is exactly the self-deception this module exists to prevent.
            best_is = runner(base_params, fold.is_start, fold.is_end)

        out_of_sample = runner(best_params, fold.oos_start, fold.oos_end)
        baseline = (
            out_of_sample
            if best_params == base_params
            else runner(base_params, fold.oos_start, fold.oos_end)
        )

        results.append(
            FoldResult(
                fold=fold,
                chosen=best_params,
                chosen_description=describe(best_params, base_params),
                in_sample=best_is,
                out_of_sample=out_of_sample,
                baseline=baseline,
                candidates_evaluated=len(candidates),
                min_trades=min_trades,
            )
        )

    return WalkForwardResult(folds=results, min_trades=min_trades, grid_size=len(candidates))
