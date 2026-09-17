"""Market calendar and session-time utilities (NYSE/CBOE equity-options calendar, US/Eastern).

Rule-based, best-effort: covers the nine standard full-day NYSE holidays (with weekend
observance for the fixed-date ones) plus the two reliable early-close days (day after
Thanksgiving, Christmas Eve). This is not a byte-for-byte replacement for an official exchange
calendar -- if you discover a missed ad-hoc closure (e.g. a national day of mourning) or an
early close this doesn't predict, add it to EXTRA_HOLIDAYS / EXTRA_HALF_DAYS below rather than
growing the rule logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from dateutil.easter import easter
from dateutil.relativedelta import MO, TH, relativedelta

EASTERN = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)

# Manually-known exceptions not covered by the standard rules. Populate with ISO dates as
# they're discovered (e.g. EXTRA_HOLIDAYS.add(date(2001, 9, 11))).
EXTRA_HOLIDAYS: set[date] = set()
EXTRA_HALF_DAYS: set[date] = set()


def _observed(d: date) -> date:
    """Shift a fixed-date holiday off the weekend per NYSE convention (Sat -> Fri, Sun -> Mon)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def _holidays_for_year(year: int) -> frozenset[date]:
    # Fixed-date holidays: include BOTH the nominal date and its weekend-shifted observance.
    # "July 4th is a holiday" is true even in a year it falls on a Saturday and the market
    # observes it Friday -- the nominal date isn't a trading day either way (it's a weekend),
    # so including it here only affects is_holiday()'s semantics, never is_trading_day()'s.
    fixed_dates = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 12, 25)]
    holidays: set[date] = set()
    for d in fixed_dates:
        holidays.add(d)
        holidays.add(_observed(d))
    holidays.update(
        {
            date(year, 1, 1) + relativedelta(weekday=MO(3)),  # MLK Day
            date(year, 2, 1) + relativedelta(weekday=MO(3)),  # Presidents Day
            easter(year) - timedelta(days=2),  # Good Friday
            date(year, 5, 31) + relativedelta(weekday=MO(-1)),  # Memorial Day
            date(year, 9, 1) + relativedelta(weekday=MO(1)),  # Labor Day
            date(year, 11, 1) + relativedelta(weekday=TH(4)),  # Thanksgiving
        }
    )
    return frozenset(holidays)


@lru_cache(maxsize=None)
def _half_days_for_year(year: int) -> frozenset[date]:
    thanksgiving = date(year, 11, 1) + relativedelta(weekday=TH(4))
    half_days = {thanksgiving + timedelta(days=1)}
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in _holidays_for_year(year):
        half_days.add(christmas_eve)
    return frozenset(half_days)


def is_holiday(d: date) -> bool:
    return d in _holidays_for_year(d.year) or d in EXTRA_HOLIDAYS


def is_half_day(d: date) -> bool:
    return d in _half_days_for_year(d.year) or d in EXTRA_HALF_DAYS


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_trading_day(d: date) -> bool:
    return not is_weekend(d) and not is_holiday(d)


def session_close_time(d: date) -> time:
    return HALF_DAY_CLOSE if is_half_day(d) else REGULAR_CLOSE


def session_bounds(d: date) -> tuple[datetime, datetime]:
    """Regular-session (open, close) as tz-aware US/Eastern datetimes. Raises on non-trading days."""
    if not is_trading_day(d):
        raise ValueError(f"{d} is not a trading day")
    open_dt = datetime.combine(d, REGULAR_OPEN, tzinfo=EASTERN)
    close_dt = datetime.combine(d, session_close_time(d), tzinfo=EASTERN)
    return open_dt, close_dt


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def prev_trading_day(d: date) -> date:
    prv = d - timedelta(days=1)
    while not is_trading_day(prv):
        prv -= timedelta(days=1)
    return prv


def trading_days_between(start: date, end: date) -> list[date]:
    """Trading days in the inclusive range [start, end]."""
    if end < start:
        raise ValueError("end must not be before start")
    days = []
    d = start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def calendar_days_to_expiration(expiration: date, as_of: date) -> int:
    """Standard options DTE: calendar days, not trading days."""
    return (expiration - as_of).days


def now_eastern() -> datetime:
    return datetime.now(EASTERN)


def is_in_regular_session(dt: datetime | None = None) -> bool:
    dt = dt.astimezone(EASTERN) if dt is not None else now_eastern()
    d = dt.date()
    if not is_trading_day(d):
        return False
    open_dt, close_dt = session_bounds(d)
    return open_dt <= dt <= close_dt


@dataclass(frozen=True)
class MarketClock:
    """Thin wrapper so callers can inject a fixed `as_of` (backtesting, tests) instead of
    depending on wall-clock time scattered through the codebase. Leave `as_of=None` for live use.
    """

    as_of: datetime | None = None

    def now(self) -> datetime:
        return self.as_of if self.as_of is not None else now_eastern()

    def today(self) -> date:
        return self.now().date()

    def in_session(self) -> bool:
        return is_in_regular_session(self.now())
