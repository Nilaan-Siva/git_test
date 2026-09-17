"""Tests for strategy/filters.py -- the entry gates.

Beyond checking each gate in isolation, this file pins down the interaction that nearly killed
the strategy during Phase 3: a short-term trend filter is almost the exact inverse of the IV
Rank filter, because index IV rises when price falls. Together they can veto every single day,
which reads in a report as "no trades" rather than as "misconfigured", so it is asserted here
directly.
"""
from datetime import date
from decimal import Decimal

from optionsbot.config.schema import StrategyParams, UniverseConfig
from optionsbot.core.enums import Right
from optionsbot.core.models import OptionContract, OptionQuote
from optionsbot.strategy.filters import (
    check_earnings_blackout,
    check_iv_rank,
    check_legs_liquidity,
    check_liquidity,
    check_not_downtrend,
)

TODAY = date(2026, 8, 13)
PARAMS = StrategyParams()
UNIVERSE = UniverseConfig()


def quote(*, bid=None, ask=None, oi: int = 1000) -> OptionQuote:
    return OptionQuote(
        contract=OptionContract(
            underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("450"), right=Right.PUT
        ),
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        last=Decimal("1.00"),
        open_interest=oi,
    )


# ---- IV Rank -----------------------------------------------------------------------------------


def test_iv_rank_passes_above_the_floor():
    assert check_iv_rank(Decimal("45"), PARAMS) is None


def test_iv_rank_rejects_below_the_floor():
    reason = check_iv_rank(Decimal("18"), PARAMS)
    assert reason is not None and reason.startswith("iv_rank_too_low")


def test_iv_rank_boundary_is_inclusive():
    assert check_iv_rank(PARAMS.min_iv_rank, PARAMS) is None


def test_unknown_iv_rank_rejects_rather_than_passes():
    """Early in a backtest there isn't enough IV history to compute a rank. Passing there would
    manufacture trades the live bot would never take, and would silently make the warmup period
    the most active stretch of the run."""
    reason = check_iv_rank(None, PARAMS)
    assert reason is not None and reason.startswith("iv_rank_unknown")


def test_unknown_iv_rank_passes_when_the_strategy_has_no_iv_floor():
    """The wheel runs with min_iv_rank 0 -- it shouldn't be blocked by a signal it doesn't use."""
    no_floor = StrategyParams(min_iv_rank=Decimal("0"))
    assert check_iv_rank(None, no_floor) is None


# ---- earnings blackout ---------------------------------------------------------------------------


def test_earnings_blackout_rejects_an_upcoming_release():
    reason = check_earnings_blackout(TODAY, [date(2026, 8, 18)], 7)
    assert reason is not None and reason.startswith("earnings_blackout")


def test_earnings_blackout_boundary_is_inclusive():
    assert check_earnings_blackout(TODAY, [date(2026, 8, 20)], 7) is not None  # exactly 7 days
    assert check_earnings_blackout(TODAY, [date(2026, 8, 21)], 7) is None  # 8 days


def test_earnings_today_is_still_a_blackout():
    assert check_earnings_blackout(TODAY, [TODAY], 7) is not None


def test_past_earnings_are_ignored():
    """A release that already happened is priced in; counting it would keep the bot out of the
    market for a week after every report for no reason."""
    assert check_earnings_blackout(TODAY, [date(2026, 8, 10)], 7) is None


def test_no_earnings_dates_passes():
    assert check_earnings_blackout(TODAY, [], 7) is None


# ---- liquidity -------------------------------------------------------------------------------------


def test_liquidity_passes_a_deep_tight_market():
    assert check_liquidity(quote(bid="1.00", ask="1.04", oi=5000), UNIVERSE) is None


def test_liquidity_rejects_thin_open_interest():
    reason = check_liquidity(quote(bid="1.00", ask="1.04", oi=10), UNIVERSE)
    assert reason is not None and reason.startswith("illiquid_open_interest")


def test_open_interest_boundary_is_inclusive():
    assert check_liquidity(quote(bid="1.00", ask="1.04", oi=UNIVERSE.min_option_open_interest), UNIVERSE) is None


def test_liquidity_rejects_a_wide_market():
    # mid 1.00, width 0.40 -> 40% of mid, far past the 10% cap
    reason = check_liquidity(quote(bid="0.80", ask="1.20"), UNIVERSE)
    assert reason is not None and reason.startswith("illiquid_spread")


def test_missing_bid_ask_skips_the_width_check_rather_than_failing_it():
    """The Polygon free tier has no historical bid/ask. A missing quote is not evidence of a
    wide market -- backtest/slippage.py models the unknown width explicitly instead, and
    rejecting here would make every historical contract untradeable."""
    assert check_liquidity(quote(oi=5000), UNIVERSE) is None


def test_legs_liquidity_reports_the_first_failing_leg():
    good, bad = quote(oi=5000), quote(oi=1)
    assert check_legs_liquidity([good, good], UNIVERSE) is None
    reason = check_legs_liquidity([good, bad], UNIVERSE)
    assert reason is not None and reason.startswith("illiquid_open_interest")


# ---- trend --------------------------------------------------------------------------------------------


def test_trend_filter_passes_above_the_average():
    closes = [100.0] * 199 + [120.0]
    assert check_not_downtrend(closes, period=200) is None


def test_trend_filter_rejects_below_the_average():
    closes = [100.0] * 199 + [80.0]
    reason = check_not_downtrend(closes, period=200)
    assert reason is not None and reason.startswith("downtrend")


def test_trend_filter_passes_when_history_is_too_short():
    """This gate vetoes a known-bad condition; it must not block trading merely because it has
    no opinion yet. The IVR gate already enforces its own warmup."""
    assert check_not_downtrend([100.0] * 50, period=200) is None
    assert check_not_downtrend([], period=200) is None


def test_trend_filter_tolerates_a_pullback_that_stays_above_the_long_average():
    """The setup the strategy exists for: rich premium during a dip inside an ongoing uptrend."""
    closes = [float(100 + i) for i in range(200)] + [250.0]  # sharp pullback from 299, still high
    assert check_not_downtrend(closes, period=200) is None


def test_short_and_long_trend_gates_disagree_during_a_pullback():
    """The Phase 3 bug, pinned down.

    IV Rank rises when price falls, so a 50-day gate rejects exactly the high-IV days the IVR
    gate wants -- the two together vetoed every day of a six-month sample and the bot never
    traded. A long-term gate keeps the pullback tradeable. If someone shortens
    `trend_sma_period` back toward 50, this test explains what breaks and why.
    """
    closes = [float(100 + i) for i in range(200)] + [250.0]
    assert check_not_downtrend(closes, period=50) is not None  # short gate: "downtrend"
    assert check_not_downtrend(closes, period=200) is None  # long gate: trend intact
