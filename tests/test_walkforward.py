"""Tests for backtest/walkforward.py -- the overfitting firewall.

The runner is injected, so these drive the logic with synthetic metrics rather than market data.
That is what makes it possible to test the cases that matter most and are hardest to produce on
demand: a strategy that only looks good in-sample, a parameter search that adds nothing over the
defaults, and folds too thin to mean anything.

The bar here is higher than "the code runs". This module's job is to *refuse* to be fooled, so
most of these tests construct a specific way of being fooled and check that it doesn't work.
"""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.backtest.metrics import BacktestMetrics
from optionsbot.backtest.walkforward import (
    DEFAULT_GRID,
    TUNABLE_FIELDS,
    describe,
    expand_grid,
    make_folds,
    walk_forward,
)
from optionsbot.config.schema import StrategyParams

BASE = StrategyParams()
START, END = date(2025, 1, 1), date(2026, 7, 1)


def metrics(*, trades: int, expectancy: str, equity: str = "25000") -> BacktestMetrics:
    return BacktestMetrics(
        label="t",
        start=START,
        end=END,
        starting_equity=Decimal("25000"),
        ending_equity=Decimal(equity),
        total_return_pct=None,
        cagr_pct=None,
        max_drawdown_pct=None,
        sharpe=None,
        sortino=None,
        trade_count=trades,
        win_count=trades,
        loss_count=0,
        win_rate_pct=100.0,
        avg_win=None,
        avg_loss=None,
        expectancy_per_trade=Decimal(expectancy),
        profit_factor=None,
        total_commission=Decimal("0"),
    )


# ---- fold construction -------------------------------------------------------------------


def test_folds_tile_the_window_without_overlapping_test_periods():
    """No test day may be scored twice, or the same luck gets counted more than once."""
    folds = make_folds(START, END, in_sample_days=180, out_of_sample_days=90)
    assert len(folds) >= 2
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.oos_end == later.oos_start


def test_test_window_always_follows_its_tuning_window():
    """The whole point: parameters may never be chosen using data from the scoring period."""
    for fold in make_folds(START, END, in_sample_days=180, out_of_sample_days=90):
        assert fold.is_end == fold.oos_start
        assert fold.is_start < fold.is_end < fold.oos_end


def test_rolling_windows_stay_fixed_length_and_anchored_ones_grow():
    rolling = make_folds(START, END, in_sample_days=180, out_of_sample_days=90)
    anchored = make_folds(START, END, in_sample_days=180, out_of_sample_days=90, anchored=True)
    assert all((f.is_end - f.is_start).days == 180 for f in rolling)
    assert all(f.is_start == START for f in anchored)
    assert (anchored[-1].is_end - anchored[-1].is_start).days > 180


def test_no_folds_when_the_window_is_too_short():
    assert make_folds(START, date(2025, 3, 1), in_sample_days=180, out_of_sample_days=90) == []


def test_folds_reject_nonsense_windows():
    with pytest.raises(ValueError):
        make_folds(END, START, in_sample_days=180, out_of_sample_days=90)
    with pytest.raises(ValueError):
        make_folds(START, END, in_sample_days=0, out_of_sample_days=90)


# ---- the guardrail on what may be tuned ------------------------------------------------------


def test_risk_settings_can_never_enter_the_parameter_search():
    """The boundary between "the strategy adapts" and "the safety limits adapt".

    A search optimising for return will happily discover that risking more makes the backtest
    look better. Risk limits are not optimisation targets, and attempting to make them ones must
    fail loudly rather than quietly succeed.
    """
    for forbidden in ("max_risk_per_trade_pct", "max_portfolio_heat_pct", "stop_loss_multiple", "time_stop_dte"):
        with pytest.raises(ValueError, match="not tunable"):
            expand_grid(BASE, {forbidden: (Decimal("0.05"),)})


def test_the_default_grid_only_touches_tunable_fields():
    assert set(DEFAULT_GRID) <= TUNABLE_FIELDS


def test_expand_grid_produces_every_combination():
    grid = {"min_iv_rank": (Decimal("20"), Decimal("30")), "short_delta_target": (Decimal("0.2"), Decimal("0.3"))}
    assert len(expand_grid(BASE, grid)) == 4


def test_expand_grid_skips_impossible_dte_combinations():
    """A minimum above the maximum yields a window nothing can fall into; the engine would
    silently find no expirations and the fold would look like a strategy failure."""
    grid = {"target_dte_min": (30, 60), "target_dte_max": (45,)}
    widths = [(p.target_dte_min, p.target_dte_max) for p in expand_grid(BASE, grid)]
    assert (60, 45) not in widths
    assert (30, 45) in widths


def test_empty_grid_means_just_the_defaults():
    assert expand_grid(BASE, {}) == [BASE]


def test_describe_names_only_what_changed():
    tuned = BASE.model_copy(update={"min_iv_rank": Decimal("40")})
    assert describe(tuned, BASE) == "min_iv_rank=40"
    assert describe(BASE, BASE) == "defaults"


# ---- selection behaviour -----------------------------------------------------------------------


def test_parameters_are_chosen_per_fold_from_in_sample_data_only():
    """Choosing once across all history and calling the result out-of-sample is the classic
    self-deception: the choice already used the future."""
    seen: list[tuple[date, date]] = []

    def runner(params, start, end):
        seen.append((start, end))
        return metrics(trades=20, expectancy="5")

    folds = make_folds(START, END, in_sample_days=180, out_of_sample_days=90)
    walk_forward(runner, folds=folds, base_params=BASE, grid={"min_iv_rank": (Decimal("20"), Decimal("30"))})

    for fold in folds:
        tuning_calls = [w for w in seen if w == (fold.is_start, fold.is_end)]
        assert len(tuning_calls) == 2, "each candidate should be evaluated on this fold's tuning window"


def test_the_best_in_sample_candidate_is_the_one_scored_out_of_sample():
    def runner(params, start, end):
        # candidate with min_iv_rank 40 is best in-sample, mediocre out-of-sample
        if start == folds[0].is_start:
            return metrics(trades=20, expectancy="9" if params.min_iv_rank == Decimal("40") else "3")
        return metrics(trades=20, expectancy="1")

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(
        runner, folds=folds, base_params=BASE, grid={"min_iv_rank": (Decimal("20"), Decimal("40"))}
    )
    assert result.folds[0].chosen.min_iv_rank == Decimal("40")


def test_a_candidate_with_too_few_in_sample_trades_is_not_chosen():
    """Three lucky trades is not evidence of the best parameters. Without a minimum, the search
    reliably picks whichever setting traded least and got lucky."""

    def runner(params, start, end):
        if params.min_iv_rank == Decimal("40"):
            return metrics(trades=2, expectancy="99")  # tiny sample, spectacular number
        return metrics(trades=30, expectancy="4")

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(
        runner,
        folds=folds,
        base_params=BASE,
        grid={"min_iv_rank": (Decimal("20"), Decimal("40"))},
        min_is_trades=10,
    )
    assert result.folds[0].chosen.min_iv_rank != Decimal("40")


# ---- verdicts ------------------------------------------------------------------------------------


def _run(oos_expectancy: str, is_expectancy: str = "10", *, trades: int = 20, baseline: str | None = None):
    def runner(params, start, end):
        in_sample = start == folds[0].is_start
        if in_sample:
            return metrics(trades=trades, expectancy=is_expectancy)
        if baseline is not None and params == BASE:
            return metrics(trades=trades, expectancy=baseline)
        return metrics(trades=trades, expectancy=oos_expectancy)

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    return walk_forward(
        runner, folds=folds, base_params=BASE, grid={"min_iv_rank": (Decimal("20"), Decimal("40"))}
    )


def test_a_strategy_that_loses_on_unseen_data_fails():
    code, why = _run("-3").verdict()
    assert code == "FAIL"
    assert "loses money" in why


def test_a_strategy_that_collapses_out_of_sample_is_called_overfit():
    """Held up in tuning, fell apart on unseen data -- the signature of fitting the past.

    The baseline is set deliberately low so tuning *does* beat doing nothing; otherwise the
    "tuning worthless" verdict fires first, which is the correct precedence but not the case
    under test here.
    """
    code, _ = _run("2", "10", baseline="1").verdict()  # 80% degradation, tuning still helps
    assert code == "OVERFIT"


def test_worthless_tuning_is_reported_ahead_of_overfitting():
    """Both can be true at once. "Use the defaults" is the actionable half -- there is no point
    describing how badly a parameter search overfits when the advice is to delete it."""
    code, _ = _run("2", "10").verdict()  # tuned == baseline, and 80% degradation
    assert code == "TUNING WORTHLESS"


def test_tuning_that_does_not_beat_the_defaults_is_called_worthless():
    """The check most walk-forward implementations skip. A parameter search that cannot beat
    leaving the settings alone is rediscovering randomness."""
    code, why = _run("8", "10", baseline="9").verdict()
    assert code == "TUNING WORTHLESS"
    assert "ship the defaults" in why


def test_a_strategy_that_holds_up_passes():
    code, _ = _run("9", "10", baseline="4").verdict()
    assert code == "PASS"


def test_thin_folds_are_excluded_rather_than_averaged_in():
    """A dozen trades cannot judge a strategy, and quietly folding them into the mean makes a
    weak result look like a firm one."""
    result = _run("9", "10", trades=3)
    assert result.judgeable == []
    code, why = result.verdict()
    assert code == "INCONCLUSIVE"
    assert "not enough data" in why


def test_the_report_says_how_many_folds_were_dropped():
    rendered = _run("9", "10", trades=3).render()
    assert "too thin to judge" in rendered
    assert "excluded" in rendered


def test_render_covers_the_headline_numbers():
    rendered = _run("9", "10", baseline="4").render()
    for expected in ("Walk-forward validation", "VERDICT", "Mean out-of-sample expectancy", "no tuning"):
        assert expected in rendered.replace("defaults and no tuning", "no tuning")


def test_degradation_is_reported_per_fold():
    result = _run("5", "10", baseline="1")
    assert result.folds[0].degradation_pct == pytest.approx(50.0)


def test_tuning_gain_measures_tuned_against_untouched_defaults():
    result = _run("9", "10", baseline="4")
    assert result.folds[0].tuning_gain == Decimal("5")


def test_when_the_search_picks_the_defaults_no_second_baseline_run_is_needed():
    """Running the identical backtest twice would double the cost of every fold for a number
    already known."""
    calls = {"n": 0}

    def runner(params, start, end):
        calls["n"] += 1
        return metrics(trades=20, expectancy="5")

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(runner, folds=folds, base_params=BASE, grid={})

    assert result.folds[0].chosen == BASE
    assert calls["n"] == 2  # one in-sample, one out-of-sample, no duplicate baseline


# ---- degenerate inputs -----------------------------------------------------------------------


def test_fold_prints_its_windows_for_the_log():
    fold = make_folds(START, END, in_sample_days=180, out_of_sample_days=90)[0]
    text = str(fold)
    assert "tune" in text and "test" in text and str(fold.oos_end) in text


def test_degradation_and_gain_are_none_when_there_is_nothing_to_compare():
    """A fold that produced no expectancy at either end must report "unknown", not zero. Zero
    would read as "no degradation", which is the opposite of what is known."""
    none_metrics = metrics(trades=20, expectancy="0")
    object.__setattr__(none_metrics, "expectancy_per_trade", None)

    def runner(params, start, end):
        return none_metrics

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(runner, folds=folds, base_params=BASE, grid={})

    assert result.folds[0].degradation_pct is None
    assert result.folds[0].tuning_gain is None
    code, why = result.verdict()
    assert code == "INCONCLUSIVE"


def test_degradation_is_none_when_in_sample_expectancy_was_not_positive():
    """Dividing by a zero or negative in-sample expectancy produces a percentage that looks
    meaningful and isn't."""

    def runner(params, start, end):
        in_sample = start == folds[0].is_start
        return metrics(trades=20, expectancy="-2" if in_sample else "5")

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(runner, folds=folds, base_params=BASE, grid={})
    assert result.folds[0].degradation_pct is None
    assert result.verdict()[0] == "PASS"  # positive out-of-sample, nothing to compare against


def test_an_empty_grid_is_not_reported_as_worthless_tuning():
    """With nothing to search, the run compares the defaults against themselves. Calling that
    "tuning worthless" is a confident verdict about something that never happened."""

    def runner(params, start, end):
        return metrics(trades=20, expectancy="5")

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(runner, folds=folds, base_params=BASE, grid={})
    assert result.tuning_attempted is False
    assert result.verdict()[0] == "PASS"


def test_a_fold_where_nothing_traded_in_sample_falls_back_to_the_defaults():
    """Rather than crowning whichever candidate managed two lucky trades."""

    def runner(params, start, end):
        return metrics(trades=0, expectancy="0")

    folds = make_folds(START, date(2025, 10, 1), in_sample_days=180, out_of_sample_days=90)
    result = walk_forward(
        runner, folds=folds, base_params=BASE, grid={"min_iv_rank": (Decimal("20"), Decimal("40"))}
    )
    assert result.folds[0].chosen == BASE
    assert result.folds[0].chosen_description == "defaults"
