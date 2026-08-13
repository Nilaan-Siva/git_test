"""Tests for the Polygon provider, using a fake HTTP client so no network or API key is needed.

Every fixture here mirrors a **real response captured from the live API on 2026-08-13**, not the
documented shape. That distinction is the whole point of this file's existence: the previous
version tested against the documentation and passed cleanly while the provider was, in
production, reading contracts from 2010, ignoring pagination, and treating a missing
open-interest field as zero. Fixtures that flatter the code teach you nothing.

Specifically, the shapes below encode these observed facts:
  * aggregate bars carry `t` as epoch **milliseconds**, with `c` close and `v` volume
  * the contracts endpoint has **no** `open_interest` field on the free tier
  * the contracts endpoint paginates via `next_url`
"""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.core.enums import Right
from optionsbot.data.providers.base import DataUnavailableError
from optionsbot.data.providers.polygon import PolygonProvider, monthly_expirations

# Real timestamps captured from the live API: epoch ms at **midnight Eastern**, not UTC
# midnight. 1781064000000 is 2026-06-10 00:00 EDT (04:00 UTC).
JUN10 = 1781064000000
JUN11 = 1781150400000


def bar(ts: int, close: float, volume: int = 500) -> dict:
    return {"t": ts, "o": close, "h": close, "l": close, "c": close, "v": volume}


def contract(strike: float, right: str = "put", expiration: str = "2026-07-17") -> dict:
    # NOTE: no open_interest key -- the free tier genuinely does not return one.
    return {
        "ticker": f"O:SPY260717{'P' if right == 'put' else 'C'}{int(strike * 1000):08d}",
        "underlying_ticker": "SPY",
        "expiration_date": expiration,
        "strike_price": strike,
        "contract_type": right,
    }


class FakeHttpClient:
    """Routes by URL substring and records every call for assertion."""

    def __init__(self, routes: dict[str, dict], default: dict | None = None):
        self.routes = routes
        self.default = default if default is not None else {"results": []}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict) -> dict:
        self.calls.append((url, params))
        for key, response in self.routes.items():
            if key in url:
                return response
        return self.default


def make_client(**routes) -> FakeHttpClient:
    return FakeHttpClient(routes)


# ---- the contracts endpoint ---------------------------------------------------------------


def test_list_contracts_never_sends_as_of():
    """The bug that made the first backfill read 2010-2012 contracts.

    `as_of` combined with `expired=true` returns the oldest contracts Polygon has ever listed,
    ordered by ticker, rather than the ones live on that date. Filtering by expiration_date is
    both correct and much cheaper, so `as_of` must never appear in the request.
    """
    client = make_client(**{"reference/options/contracts": {"results": [contract(700)]}})
    provider = PolygonProvider("key", client=client)

    provider.list_contracts(
        "SPY", expiration=date(2026, 7, 17), strike_min=Decimal("650"), strike_max=Decimal("760")
    )

    _, params = client.calls[0]
    assert "as_of" not in params
    assert params["expiration_date"] == "2026-07-17"
    assert params["strike_price.gte"] == 650.0
    assert params["strike_price.lte"] == 760.0


def test_list_contracts_follows_pagination():
    """The response paginates at 1000; ignoring next_url silently truncated every fetch."""
    page_two = {"results": [contract(690)]}
    page_one = {"results": [contract(700)], "next_url": "https://api.polygon.io/PAGE2"}

    class Paging(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            return page_two if "PAGE2" in url else page_one

    client = Paging({})
    provider = PolygonProvider("key", client=client)
    results = provider.list_contracts(
        "SPY", expiration=date(2026, 7, 17), strike_min=Decimal("650"), strike_max=Decimal("760")
    )

    assert len(results) == 2
    assert len(client.calls) == 2


def test_list_contracts_stops_at_max_pages():
    """A next_url that always points somewhere must not loop forever."""

    class Endless(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            return {"results": [contract(700)], "next_url": "https://api.polygon.io/MORE"}

    client = Endless({})
    provider = PolygonProvider("key", client=client)
    provider.list_contracts(
        "SPY", expiration=date(2026, 7, 17), strike_min=Decimal("1"), strike_max=Decimal("9999"), max_pages=3
    )
    assert len(client.calls) == 3


# ---- bar parsing --------------------------------------------------------------------------


def test_bar_timestamps_resolve_to_the_eastern_session_date():
    """A 04:00-UTC timestamp is midnight in New York, and the bar belongs to that ET date.
    Getting this wrong shifts the entire price series by one day against the option chains."""
    client = make_client(**{"aggs/ticker/SPY": {"results": [bar(JUN10, 725.43)]}})
    provider = PolygonProvider("key", client=client)
    assert list(provider.underlying_bars("SPY", date(2026, 6, 10), date(2026, 6, 10))) == [date(2026, 6, 10)]


def test_underlying_bars_keys_by_calendar_date_from_epoch_millis():
    client = make_client(**{"aggs/ticker/SPY": {"results": [bar(JUN10, 725.43), bar(JUN11, 730.0)]}})
    provider = PolygonProvider("key", client=client)

    bars = provider.underlying_bars("SPY", date(2026, 6, 10), date(2026, 6, 11))

    assert set(bars) == {date(2026, 6, 10), date(2026, 6, 11)}
    assert bars[date(2026, 6, 10)]["c"] == 725.43


def test_get_underlying_price_reads_the_close():
    client = make_client(**{"aggs/ticker/SPY": {"results": [bar(JUN10, 725.43)]}})
    provider = PolygonProvider("key", client=client)
    assert provider.get_underlying_price("SPY", date(2026, 6, 10)) == Decimal("725.43")


def test_get_underlying_price_raises_when_the_day_is_missing():
    client = make_client(**{"aggs/ticker/SPY": {"results": []}})
    provider = PolygonProvider("key", client=client)
    with pytest.raises(DataUnavailableError):
        provider.get_underlying_price("SPY", date(2026, 6, 10))


def test_bars_without_a_timestamp_are_skipped_not_crashed_on():
    client = make_client(**{"aggs/ticker/SPY": {"results": [{"c": 700.0}, bar(JUN10, 725.43)]}})
    provider = PolygonProvider("key", client=client)
    assert set(provider.underlying_bars("SPY", date(2026, 6, 10), date(2026, 6, 10))) == {date(2026, 6, 10)}


# ---- chain assembly ------------------------------------------------------------------------


def build_provider_with_chain(**kwargs) -> PolygonProvider:
    class Routed(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                return {"results": [contract(700), contract(695)]}
            if "aggs/ticker/O:" in url:  # an option contract
                return {"results": [bar(JUN10, 8.50, volume=250)]}
            return {"results": [bar(JUN10, 725.43)]}  # the underlying

    return PolygonProvider("key", client=Routed({}), **kwargs)


def test_build_chains_yields_a_chain_per_day_with_computed_greeks():
    provider = build_provider_with_chain()
    chains = list(
        provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)])
    )

    assert len(chains) == 1
    chain = chains[0]
    assert chain.underlying == "SPY"
    assert chain.underlying_price == Decimal("725.43")
    assert len(chain.quotes) == 2
    quote = chain.quotes[0]
    assert quote.contract.right == Right.PUT
    assert quote.last == Decimal("8.5")
    assert quote.volume == 250
    # IV and greeks are computed locally, since the free tier supplies neither
    assert quote.implied_volatility is not None
    assert quote.delta is not None and quote.delta < 0  # a put


def test_open_interest_is_none_not_zero():
    """The bug that would have produced a confidently empty backtest.

    The free tier returns no open_interest field. Defaulting it to 0 made every contract look
    untraded, so the liquidity floor rejected all of them -- and a run with no data is
    indistinguishable, in the report, from a run that found no setups.
    """
    provider = build_provider_with_chain()
    chain = next(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)]))
    assert all(q.open_interest is None for q in chain.quotes)
    assert all(q.volume > 0 for q in chain.quotes)


def test_bid_and_ask_stay_none_rather_than_being_invented():
    provider = build_provider_with_chain()
    chain = next(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)]))
    assert all(q.bid is None and q.ask is None for q in chain.quotes)


def test_strike_step_filters_out_off_grid_strikes():
    """SPY lists $1 strikes near the money. Fetching them all triples the API budget for
    granularity a 5-wide spread never uses."""
    provider = build_provider_with_chain(strike_step=Decimal("10"))
    chain = next(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)]))
    assert [q.contract.strike for q in chain.quotes] == [Decimal("700")]  # 695 is off the grid


def test_build_chains_fetches_each_contract_exactly_once_regardless_of_window_length():
    """The property that makes a free-tier backfill feasible: call count scales with contracts,
    not with contracts x days. At 5 calls/minute the naive version needs over a hundred hours."""

    class Counting(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                return {"results": [contract(700), contract(695)]}
            if "aggs/ticker/O:" in url:
                return {"results": [bar(JUN10, 8.50), bar(JUN11, 8.20)]}
            return {"results": [bar(JUN10, 725.43), bar(JUN11, 730.0)]}

    client = Counting({})
    provider = PolygonProvider("key", client=client)
    chains = list(
        provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 11), expirations=[date(2026, 7, 17)])
    )

    assert len(chains) == 2  # two days of chains...
    option_calls = [c for c, _ in client.calls if "aggs/ticker/O:" in c]
    assert len(option_calls) == 2  # ...from one call per contract, not per contract per day


def test_rights_filter_halves_the_listing_calls():
    """The factor-of-two that made the first live run blow its time budget.

    Leaving `rights` unset fetches puts and calls, doubling every backfill estimate. A
    put-credit-spread run only needs puts, and at 5 calls/minute that difference is hours.
    """

    class Counting(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                return {"results": [contract(700)]}
            if "aggs/ticker/O:" in url:
                return {"results": [bar(JUN10, 8.50)]}
            return {"results": [bar(JUN10, 725.43)]}

    both = Counting({})
    PolygonProvider("key", client=both).build_chains(
        "SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)]
    ).__next__()

    puts = Counting({})
    list(
        PolygonProvider("key", client=puts).build_chains(
            "SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)], rights=(Right.PUT,)
        )
    )

    listing = lambda c: [p for u, p in c.calls if "reference/options/contracts" in u]
    assert len(listing(both)) == 1 and "contract_type" not in listing(both)[0]
    assert len(listing(puts)) == 1 and listing(puts)[0]["contract_type"] == "put"


def test_build_chains_raises_when_the_underlying_has_no_bars():
    client = make_client(**{"aggs/ticker/SPY": {"results": []}})
    provider = PolygonProvider("key", client=client)
    with pytest.raises(DataUnavailableError):
        list(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10)))


def test_build_chains_raises_when_no_expirations_are_in_range():
    client = make_client(**{"aggs/ticker/SPY": {"results": [bar(JUN10, 725.43)]}})
    provider = PolygonProvider("key", client=client)
    with pytest.raises(DataUnavailableError):
        list(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[]))


def test_contracts_expiring_before_the_day_are_excluded():
    """A contract that has already expired has no place in that day's chain."""

    class Stale(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                return {"results": [contract(700, expiration="2026-06-01")]}
            if "aggs/ticker/O:" in url:
                return {"results": [bar(JUN10, 8.50)]}
            return {"results": [bar(JUN10, 725.43)]}

    provider = PolygonProvider("key", client=Stale({}))
    assert list(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 6, 1)])) == []


def test_zero_or_missing_close_is_skipped_rather_than_priced():
    class NoTrades(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                return {"results": [contract(700)]}
            if "aggs/ticker/O:" in url:
                return {"results": [{"t": JUN10, "c": 0.0, "v": 0}]}
            return {"results": [bar(JUN10, 725.43)]}

    provider = PolygonProvider("key", client=NoTrades({}))
    assert list(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2026, 7, 17)])) == []


def test_get_chain_delegates_to_build_chains():
    provider = build_provider_with_chain()
    chain = provider.get_chain("SPY", date(2026, 6, 10))
    assert chain.as_of == date(2026, 6, 10)


def test_get_chain_raises_when_nothing_is_available():
    client = make_client(**{"aggs/ticker/SPY": {"results": [bar(JUN10, 725.43)]}})
    provider = PolygonProvider("key", client=client)
    with pytest.raises(DataUnavailableError):
        provider.get_chain("SPY", date(2026, 6, 10))


def test_provider_requires_an_api_key():
    with pytest.raises(ValueError):
        PolygonProvider("")


# ---- expiration calendar ---------------------------------------------------------------------


def test_monthly_expirations_are_third_fridays():
    result = monthly_expirations(date(2026, 1, 1), date(2026, 4, 30))
    assert result == [date(2026, 1, 16), date(2026, 2, 20), date(2026, 3, 20), date(2026, 4, 17)]
    assert all(d.weekday() == 4 for d in result)
    assert all(15 <= d.day <= 21 for d in result)


def test_monthly_expirations_respects_the_window_edges():
    assert monthly_expirations(date(2026, 1, 17), date(2026, 2, 19)) == []


def test_monthly_expirations_crosses_a_year_boundary():
    result = monthly_expirations(date(2026, 11, 1), date(2027, 2, 28))
    assert result == [date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15), date(2027, 2, 19)]
