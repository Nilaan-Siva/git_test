"""Tests for core/clock.py. Deliberately avoids hardcoding "I remember X falls on a Y" facts
about specific future years -- every assertion either derives the expected date from the same
rule the calendar itself uses, or searches for a year with the property under test."""
from datetime import date, timedelta

from dateutil.easter import easter
from dateutil.relativedelta import MO, TH, relativedelta

from optionsbot.core.clock import (
    calendar_days_to_expiration,
    is_half_day,
    is_holiday,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    session_bounds,
    session_close_time,
    trading_days_between,
)

YEARS = range(2024, 2032)


def test_new_years_day_is_a_holiday():
    for year in YEARS:
        assert is_holiday(date(year, 1, 1)) or date(year, 1, 1).weekday() >= 5


def test_thanksgiving_is_fourth_thursday_and_day_after_is_half_day():
    for year in YEARS:
        thanksgiving = date(year, 11, 1) + relativedelta(weekday=TH(4))
        assert thanksgiving.weekday() == 3
        assert is_holiday(thanksgiving)
        assert is_half_day(thanksgiving + timedelta(days=1))
        assert session_close_time(thanksgiving + timedelta(days=1)).hour == 13


def test_good_friday_is_friday_before_easter_sunday():
    for year in YEARS:
        good_friday = easter(year) - timedelta(days=2)
        assert good_friday.weekday() == 4
        assert is_holiday(good_friday)


def test_labor_day_is_first_monday_of_september():
    for year in YEARS:
        labor_day = date(year, 9, 1) + relativedelta(weekday=MO(1))
        assert is_holiday(labor_day)
        assert not is_trading_day(labor_day)


def test_observed_shifts_saturday_holiday_to_preceding_friday():
    year = next(y for y in range(2020, 2050) if date(y, 7, 4).weekday() == 5)
    assert is_holiday(date(year, 7, 4))
    friday_before = date(year, 7, 3)
    assert is_holiday(friday_before)
    assert not is_trading_day(friday_before)


def test_observed_shifts_sunday_holiday_to_following_monday():
    year = next(y for y in range(2020, 2050) if date(y, 12, 25).weekday() == 6)
    assert is_holiday(date(year, 12, 25))
    monday_after = date(year, 12, 26)
    assert is_holiday(monday_after)
    assert not is_trading_day(monday_after)


def test_ordinary_weekday_is_a_trading_day():
    # walk forward from a fixed date until we hit a plain Wednesday in March (no holiday rule
    # ever lands there), for every test year.
    for year in YEARS:
        d = date(year, 3, 10)
        while d.weekday() != 2:
            d += timedelta(days=1)
        assert is_trading_day(d)


def test_weekend_is_never_a_trading_day():
    saturday = date(2026, 8, 15)
    assert saturday.weekday() == 5
    assert not is_trading_day(saturday)
    assert not is_trading_day(saturday + timedelta(days=1))


def test_next_trading_day_skips_a_plain_weekend():
    # find a Friday that is itself a trading day, for a representative year
    d = date(2026, 3, 1)
    while not (d.weekday() == 4 and is_trading_day(d)):
        d += timedelta(days=1)
    expected = d + timedelta(days=3)  # Monday, assuming no Monday holiday collision here
    while not is_trading_day(expected):
        expected += timedelta(days=1)
    assert next_trading_day(d) == expected


def test_prev_trading_day_is_inverse_of_next_trading_day():
    d = date(2026, 6, 10)
    while not is_trading_day(d):
        d += timedelta(days=1)
    assert prev_trading_day(next_trading_day(d)) == d


def test_trading_days_between_excludes_weekends():
    # a full week plus the following weekend
    days = trading_days_between(date(2026, 3, 2), date(2026, 3, 8))  # Mon..Sun
    assert len(days) == 5
    assert all(d.weekday() < 5 for d in days)


def test_trading_days_between_rejects_inverted_range():
    import pytest

    with pytest.raises(ValueError):
        trading_days_between(date(2026, 3, 8), date(2026, 3, 2))


def test_session_bounds_regular_day():
    d = date(2026, 3, 4)  # ordinary Wednesday
    assert is_trading_day(d)
    open_dt, close_dt = session_bounds(d)
    assert (open_dt.hour, open_dt.minute) == (9, 30)
    assert (close_dt.hour, close_dt.minute) == (16, 0)


def test_session_bounds_raises_on_non_trading_day():
    import pytest

    saturday = date(2026, 8, 15)
    with pytest.raises(ValueError):
        session_bounds(saturday)


def test_calendar_days_to_expiration_matches_contract_dte():
    assert calendar_days_to_expiration(date(2026, 9, 18), date(2026, 8, 19)) == 30
