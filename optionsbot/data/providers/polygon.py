"""Polygon.io (now branded Massive) historical options data provider.

**Everything below about the free tier was verified against the live API on 2026-08-13**, which
matters, because the first version of this file was written against the documentation and got
three things wrong in ways that fixture-based unit tests could not catch:

  1. Passing `as_of` to the contracts endpoint alongside `expired=true` silently returned
     contracts from 2010-2012 -- ordered by ticker, oldest first -- instead of the contracts
     live on that date. The fix is to filter by `expiration_date` and not send `as_of` at all.
  2. The response paginates at 1000 results with a `next_url`, which was ignored, so only the
     first page was ever read. Combined with (1) that meant reading ancient history exclusively.
  3. `open_interest` is **not present** in the free-tier contracts response. The old code did
     `.get("open_interest", 0)`, so every contract looked completely untraded and the liquidity
     floor rejected all of them -- a backtest that reports zero trades because it has no data,
     while looking exactly like one that found no setups. OptionQuote.open_interest is now
     Optional and this provider leaves it None; strategy/filters.py falls back to volume.

Free-tier limits, measured rather than assumed:

  * **5 API calls per minute.** The 6th in a minute returns HTTP 429. This is the binding
    constraint on everything below.
  * **2 years of history.** 2024-09 works; 2021-09 returns 403.
  * **No snapshot endpoint** (403 NOT_AUTHORIZED), so no open interest, no greeks, no bid/ask,
    historical or otherwise. IV and greeks are computed locally from the daily close via
    data/pricing.py; bid/ask stay None and backtest/slippage.py models the spread explicitly.

The design consequence of 5 calls/minute is `build_chains`. The obvious implementation -- one
call per contract per day -- needs ~1,950 calls per underlying per day of backtest and would
take over a hundred hours. But the aggregates endpoint accepts a *date range* and returns every
daily bar for a contract in a single call, so fetching each contract once covers the whole
window. That plus restricting to monthly expirations (which are the liquid ones anyway) turns
an infeasible backfill into roughly half an hour per underlying.

`get_chain` (the ChainProvider interface) still works for a single day and is what live/paper
trading will use, but it is deliberately the slow path: prefer `build_chains` for backfills.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator, Optional, Protocol

from optionsbot.core.clock import EASTERN
from optionsbot.core.enums import Right
from optionsbot.core.models import Chain, OptionContract, OptionQuote
from optionsbot.data.pricing import (
    DEFAULT_RISK_FREE_RATE,
    bs_greeks,
    implied_volatility,
    years_to_expiration,
)
from optionsbot.data.providers.base import ChainProvider, DataUnavailableError

POLYGON_BASE_URL = "https://api.polygon.io"
FREE_TIER_CALLS_PER_MINUTE = 5


class HttpClient(Protocol):
    """Minimal seam over whatever HTTP library is actually used, so tests can inject a fake
    without hitting the network or requiring `requests` to be installed."""

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class _RequestsHttpClient:
    """Real HTTP client, with client-side throttling and 429 backoff.

    The throttle is not politeness -- at 5 calls/minute a backfill spends almost all of its wall
    clock waiting, so pacing requests locally is strictly faster than discovering the limit via
    429s and retrying.
    """

    api_key: str
    timeout_seconds: float = 20.0
    calls_per_minute: int = FREE_TIER_CALLS_PER_MINUTE
    max_retries: int = 5
    _call_times: list[float] = field(default_factory=list)

    def _throttle(self) -> None:
        if self.calls_per_minute <= 0:
            return
        now = time.monotonic()
        self._call_times = [t for t in self._call_times if now - t < 60.0]
        if len(self._call_times) >= self.calls_per_minute:
            sleep_for = 60.0 - (now - self._call_times[0]) + 0.25
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            self._call_times = [t for t in self._call_times if now - t < 60.0]
        self._call_times.append(time.monotonic())

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        import requests  # local import: keeps `requests` an optional ("data" extra) dependency

        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(self.max_retries):
            self._throttle()
            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout_seconds)
            if resp.status_code == 429:
                time.sleep(min(60, 2**attempt * 5))
                continue
            if resp.status_code == 403:
                raise DataUnavailableError(f"not entitled to {url}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json()
        raise DataUnavailableError(f"rate limited after {self.max_retries} attempts: {url}")


def _to_decimal(value: float | int | None, places: int = 4) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(float(value), places)))


def _bar_date(bar: dict[str, Any]) -> Optional[date]:
    """The session date of a daily bar.

    Polygon puts epoch milliseconds in `t`, set to **midnight Eastern** -- verified live: a
    2026-06-10 bar carries 1781064000000, which is 04:00 UTC (EDT). Converting in UTC happens to
    yield the right calendar date year-round, since ET midnight is 04:00 or 05:00 UTC either
    way, but converting in the market's own timezone is what the value actually means and
    removes the need to re-derive that argument later.
    """
    ts = bar.get("t")
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=EASTERN).date()


def monthly_expirations(start: date, end: date) -> list[date]:
    """Third Friday of each month in [start, end].

    Restricting a backfill to monthlies cuts the API call budget roughly fourfold versus
    weeklies, and costs little: monthly expirations carry the deepest open interest and tightest
    markets, so they are what a liquidity-constrained strategy should be trading regardless.
    """
    out: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        first_friday = cursor + timedelta(days=(4 - cursor.weekday()) % 7)
        third_friday = first_friday + timedelta(days=14)
        if start <= third_friday <= end:
            out.append(third_friday)
        cursor = date(cursor.year + (cursor.month // 12), (cursor.month % 12) + 1, 1)
    return out


class PolygonProvider(ChainProvider):
    def __init__(
        self,
        api_key: str,
        *,
        client: HttpClient | None = None,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        strike_band_below: float = 0.12,
        strike_band_above: float = 0.03,
        strike_step: Optional[Decimal] = None,
    ):
        """`strike_band_*` bound which strikes are worth fetching, as fractions of spot.

        The defaults are asymmetric on purpose: this bot sells put spreads, so it needs a decent
        run of strikes *below* the money (a 30-delta put sits ~4-6% under spot, and the search
        needs room around that) and almost nothing above. Widening these is the single easiest
        way to blow the API budget -- every extra strike is another call.

        `strike_step` keeps only strikes that are exact multiples of it. SPY lists $1 strikes
        near the money, which triples the fetch cost for granularity a 5-wide spread never uses.
        """
        if not api_key:
            raise ValueError("PolygonProvider requires a non-empty api_key")
        self._client = client or _RequestsHttpClient(api_key=api_key)
        self._risk_free_rate = risk_free_rate
        self.strike_band_below = strike_band_below
        self.strike_band_above = strike_band_above
        self.strike_step = strike_step

    # ---- low-level endpoints ---------------------------------------------------------------

    def underlying_bars(self, underlying: str, start: date, end: date) -> dict[date, dict[str, Any]]:
        """Every daily bar for the underlying across a range, in one call."""
        raw = self._client.get(
            f"{POLYGON_BASE_URL}/v2/aggs/ticker/{underlying.upper()}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}",
            params={"adjusted": "true", "limit": 50000},
        )
        out = {}
        for bar in raw.get("results") or []:
            day = _bar_date(bar)
            if day is not None:
                out[day] = bar
        return out

    def option_bars(self, option_ticker: str, start: date, end: date) -> dict[date, dict[str, Any]]:
        """Every daily bar for one option contract across a range, in one call.

        This is the method that makes a free-tier backfill possible at all -- see the module
        docstring. One call per contract, not one per contract per day.
        """
        raw = self._client.get(
            f"{POLYGON_BASE_URL}/v2/aggs/ticker/{option_ticker}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}",
            params={"adjusted": "true", "limit": 50000},
        )
        out = {}
        for bar in raw.get("results") or []:
            day = _bar_date(bar)
            if day is not None:
                out[day] = bar
        return out

    def list_contracts(
        self,
        underlying: str,
        *,
        expiration: date,
        strike_min: Decimal,
        strike_max: Decimal,
        right: Optional[Right] = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Contracts for one expiration within a strike band, following pagination.

        Deliberately does NOT send `as_of`: combined with `expired=true` that parameter returns
        the oldest contracts Polygon has ever listed rather than the ones live on that date.
        Filtering by `expiration_date` is both correct and far cheaper.
        """
        params: dict[str, Any] = {
            "underlying_ticker": underlying.upper(),
            "expiration_date": expiration.isoformat(),
            "strike_price.gte": float(strike_min),
            "strike_price.lte": float(strike_max),
            "expired": "true",
            "limit": 1000,
        }
        if right is not None:
            params["contract_type"] = right.value.lower()

        results: list[dict[str, Any]] = []
        url = f"{POLYGON_BASE_URL}/v3/reference/options/contracts"
        for _ in range(max_pages):
            raw = self._client.get(url, params=params)
            results.extend(raw.get("results") or [])
            next_url = raw.get("next_url")
            if not next_url:
                break
            url, params = next_url, {}
        return results

    # ---- ChainProvider interface ------------------------------------------------------------

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        bars = self.underlying_bars(underlying, as_of, as_of)
        bar = bars.get(as_of)
        if bar is None or bar.get("c") is None:
            raise DataUnavailableError(f"no underlying close for {underlying} on {as_of}")
        return Decimal(str(bar["c"]))

    def get_chain(self, underlying: str, as_of: date) -> Chain:
        """One day's chain. Correct but slow -- prefer `build_chains` for backfills."""
        chains = list(self.build_chains(underlying, as_of, as_of))
        if not chains:
            raise DataUnavailableError(f"no chain for {underlying} on {as_of}")
        return chains[0]

    # ---- the bulk path ------------------------------------------------------------------------

    def build_chains(
        self,
        underlying: str,
        start: date,
        end: date,
        *,
        dte_min: int = 25,
        dte_max: int = 60,
        expirations: Optional[list[date]] = None,
        rights: Optional[tuple[Right, ...]] = None,
    ) -> Iterator[Chain]:
        """Yield one Chain per trading day in [start, end], fetching each contract only once.

        Call budget is roughly `1 + expirations * (1 + strikes * rights)`, independent of how
        many days the window covers -- which is the whole point.

        `rights` is the other half of the budget, and it is easy to overlook: leaving it None
        fetches puts *and* calls, doubling every estimate. A put-credit-spread-only run should
        pass `(Right.PUT,)` and halve its backfill time; an iron condor genuinely needs both.
        """
        underlying = underlying.upper()
        spot_bars = self.underlying_bars(underlying, start, end)
        if not spot_bars:
            raise DataUnavailableError(f"no underlying bars for {underlying} in {start}..{end}")

        if expirations is None:
            expirations = monthly_expirations(start + timedelta(days=dte_min), end + timedelta(days=dte_max))
        if not expirations:
            raise DataUnavailableError(f"no expirations in range for {underlying}")

        spots = {day: float(bar["c"]) for day, bar in spot_bars.items() if bar.get("c")}
        lo_spot, hi_spot = min(spots.values()), max(spots.values())
        strike_min = Decimal(str(round(lo_spot * (1 - self.strike_band_below), 2)))
        strike_max = Decimal(str(round(hi_spot * (1 + self.strike_band_above), 2)))

        # contract ticker -> (contract metadata, {date: bar})
        loaded: list[tuple[dict[str, Any], dict[date, dict[str, Any]]]] = []
        for expiration in expirations:
            contracts: list[dict[str, Any]] = []
            for right in rights or (None,):
                contracts.extend(
                    self.list_contracts(
                        underlying,
                        expiration=expiration,
                        strike_min=strike_min,
                        strike_max=strike_max,
                        right=right,
                    )
                )
            for raw in contracts:
                strike = raw.get("strike_price")
                if strike is None:
                    continue
                if self.strike_step is not None and Decimal(str(strike)) % self.strike_step != 0:
                    continue
                ticker = raw.get("ticker")
                if not ticker:
                    continue
                bars = self.option_bars(ticker, start, min(end, expiration))
                if bars:
                    loaded.append((raw, bars))

        for day in sorted(spots):
            spot = Decimal(str(spots[day]))
            quotes = []
            for raw, bars in loaded:
                bar = bars.get(day)
                if bar is None:
                    continue
                quote = self._quote_from_bar(raw, bar, underlying_price=spot, as_of=day)
                if quote is not None:
                    quotes.append(quote)
            if quotes:
                yield Chain(underlying=underlying, as_of=day, underlying_price=spot, quotes=quotes)

    def _quote_from_bar(
        self, contract_raw: dict[str, Any], bar: dict[str, Any], *, underlying_price: Decimal, as_of: date
    ) -> Optional[OptionQuote]:
        expiration = contract_raw.get("expiration_date")
        strike = contract_raw.get("strike_price")
        contract_type = contract_raw.get("contract_type")
        if not (expiration and strike and contract_type):
            return None

        close = bar.get("c")
        if close is None or close <= 0:
            return None  # no trades that day; skip rather than fabricate a quote

        contract = OptionContract(
            underlying=contract_raw.get("underlying_ticker", ""),
            expiration=date.fromisoformat(expiration),
            strike=Decimal(str(strike)),
            right=Right.CALL if contract_type == "call" else Right.PUT,
            occ_symbol=contract_raw.get("ticker"),
        )
        if contract.dte(as_of) < 0:
            return None

        t_years = years_to_expiration(contract.dte(as_of))
        s, k = float(underlying_price), float(strike)
        is_call = contract.right == Right.CALL
        iv = implied_volatility(close, s, k, t_years, self._risk_free_rate, is_call)
        greeks = bs_greeks(s, k, t_years, self._risk_free_rate, iv, is_call) if iv is not None else {}

        return OptionQuote(
            contract=contract,
            bid=None,
            ask=None,
            last=_to_decimal(close),
            volume=int(bar.get("v") or 0),
            # Not available on the free tier at all -- None, never 0. See the module docstring.
            open_interest=None,
            implied_volatility=_to_decimal(iv) if iv is not None else None,
            delta=_to_decimal(greeks.get("delta")) if greeks else None,
            gamma=_to_decimal(greeks.get("gamma"), places=6) if greeks else None,
            theta=_to_decimal(greeks.get("theta")) if greeks else None,
            vega=_to_decimal(greeks.get("vega")) if greeks else None,
        )
