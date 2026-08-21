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
from datetime import date, timedelta
from decimal import Decimal

import pytest

from optionsbot.core.enums import Right
from optionsbot.data.providers.base import DataUnavailableError
from optionsbot.data.providers.polygon import PolygonProvider, monthly_expirations, weekly_expirations

# Real timestamps captured from the live API: epoch ms at **midnight Eastern**, not UTC
# midnight. 1781064000000 is 2026-06-10 00:00 EDT (04:00 UTC).
JUN10 = 1781064000000
JUN11 = 1781150400000
PAST_EXPIRY = date(2026, 7, 17)  # comfortably past the CONTRACT_CACHE_MIN_AGE_DAYS floor


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


def test_strike_bands_are_computed_per_expiration_not_across_the_whole_window():
    """Banding once against the 18-month high and low multiplies the strike count -- and every
    extra strike is another API call at five per minute.

    Each expiration only matters from its entry window down to expiry, roughly two months, over
    which spot moves a few percent. Here the underlying runs 600 -> 900; a global band would ask
    for strikes from ~530 upward, while the near expiration should only ask around its own
    ~600-620 range.
    """
    requested: list[tuple[float, float]] = []

    class Banded(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                requested.append((params["strike_price.gte"], params["strike_price.lte"]))
                return {"results": []}
            if "aggs/ticker/O:" in url:
                return {"results": []}
            # spot climbs steeply across the window
            return {"results": [bar(JUN10, 600.0), bar(JUN11, 900.0)]}

    provider = PolygonProvider("key", client=Banded({}))
    list(
        provider.build_chains(
            "SPY", date(2026, 6, 10), date(2026, 6, 11), expirations=[date(2026, 7, 17)], dte_max=60
        )
    )

    assert requested, "no contract listing was requested"
    low, high = requested[0]
    # a global band on the 600..900 range would start near 528; a per-expiration one must not
    assert low > 520
    assert high < 1000


def test_expirations_with_no_relevant_days_are_skipped_entirely():
    """An expiration outside the window costs nothing -- it must not trigger a listing call."""

    class Counting(FakeHttpClient):
        def get(self, url, params):
            self.calls.append((url, params))
            if "reference/options/contracts" in url:
                return {"results": []}
            return {"results": [bar(JUN10, 725.43)]}

    client = Counting({})
    provider = PolygonProvider("key", client=client)
    # expiration is years past the data window, so no day is within dte_max of it
    list(provider.build_chains("SPY", date(2026, 6, 10), date(2026, 6, 10), expirations=[date(2030, 1, 18)]))
    assert not [u for u, _ in client.calls if "reference/options/contracts" in u]


def test_option_bars_are_cached_to_disk_and_reused(tmp_path):
    """What makes a multi-hour backfill survivable.

    Chains can only be assembled after every contract is loaded, so an interruption at 95%
    otherwise discards 95% of a rate-limited budget and starts from nothing. A second run must
    replay from disk without spending a single call.
    """
    client = make_client(**{"aggs/ticker/O:": {"results": [bar(JUN10, 8.50, volume=250)]}})
    provider = PolygonProvider("key", client=client, bar_cache_dir=tmp_path)

    first = provider.option_bars("O:SPY260717P00700000", date(2026, 6, 10), date(2026, 6, 10))
    calls_after_first = len(client.calls)
    second = provider.option_bars("O:SPY260717P00700000", date(2026, 6, 10), date(2026, 6, 10))

    assert first == second
    assert len(client.calls) == calls_after_first, "cached contract was refetched"
    assert list(tmp_path.glob("*.json")), "nothing was written to the bar cache"


def test_bar_cache_filenames_survive_the_colon_in_occ_tickers(tmp_path):
    client = make_client(**{"aggs/ticker/O:": {"results": [bar(JUN10, 8.50)]}})
    provider = PolygonProvider("key", client=client, bar_cache_dir=tmp_path)
    provider.option_bars("O:SPY260717P00700000", date(2026, 6, 10), date(2026, 6, 10))
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1 and ":" not in written[0].name


def test_a_truncated_cache_file_is_refetched_rather_than_trusted(tmp_path):
    """A run killed mid-write must not leave a corrupt file that a later run reads as a
    complete, empty contract history -- that would silently drop the contract from every chain.
    """
    client = make_client(**{"aggs/ticker/O:": {"results": [bar(JUN10, 8.50)]}})
    provider = PolygonProvider("key", client=client, bar_cache_dir=tmp_path)
    (tmp_path / "O_SPY260717P00700000.json").write_text('[{"t": 1781064000000, "c":')  # truncated

    bars = provider.option_bars("O:SPY260717P00700000", date(2026, 6, 10), date(2026, 6, 10))

    assert bars, "corrupt cache was trusted instead of refetched"
    assert len(client.calls) == 1


def test_no_bar_cache_dir_means_no_files_and_no_memoisation(tmp_path):
    client = make_client(**{"aggs/ticker/O:": {"results": [bar(JUN10, 8.50)]}})
    provider = PolygonProvider("key", client=client)
    provider.option_bars("O:SPY260717P00700000", date(2026, 6, 10), date(2026, 6, 10))
    provider.option_bars("O:SPY260717P00700000", date(2026, 6, 10), date(2026, 6, 10))
    assert len(client.calls) == 2
    assert not list(tmp_path.iterdir())


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


def test_weekly_expirations_are_every_friday_and_contain_the_monthlies():
    weeklies = weekly_expirations(date(2026, 1, 1), date(2026, 4, 30))
    assert all(d.weekday() == 4 for d in weeklies)
    assert [(b - a).days for a, b in zip(weeklies, weeklies[1:])] == [7] * (len(weeklies) - 1)
    assert weeklies[0] == date(2026, 1, 2)
    # Third Fridays are ordinary Fridays, so the monthly ladder must be a strict subset -- this
    # is what lets --weeklies replace (not supplement) the monthly list with no deduplication.
    monthlies = monthly_expirations(date(2026, 1, 1), date(2026, 4, 30))
    assert set(monthlies) < set(weeklies)


def test_weekly_expirations_respects_the_window_edges():
    # A window containing no Friday at all yields nothing rather than reaching outside it.
    assert weekly_expirations(date(2026, 1, 3), date(2026, 1, 8)) == []  # Sat..Thu
    # A window that IS a single Friday yields exactly that Friday.
    assert weekly_expirations(date(2026, 1, 2), date(2026, 1, 2)) == [date(2026, 1, 2)]


def test_monthly_expirations_crosses_a_year_boundary():
    result = monthly_expirations(date(2026, 11, 1), date(2027, 2, 28))
    assert result == [date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15), date(2027, 2, 19)]


# ---- transient network failures -------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"results": []}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _no_sleep(monkeypatch):
    import optionsbot.data.providers.polygon as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


def test_a_read_timeout_is_retried_rather_than_killing_the_run(monkeypatch):
    """The failure that ended a ten-hour backfill at hour four.

    Over thousands of calls a transient timeout is a certainty, not an edge case. It used to
    propagate as a raw requests exception straight out of build_chains, discarding every
    contract fetched so far.
    """
    import requests

    from optionsbot.data.providers.polygon import _RequestsHttpClient

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def flaky(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.Timeout("read timed out")
        return FakeResponse(payload={"results": [{"t": JUN10, "c": 1.0}]})

    monkeypatch.setattr(requests, "get", flaky)
    client = _RequestsHttpClient(api_key="k", calls_per_minute=0)

    assert client.get("https://example/x", {}) == {"results": [{"t": JUN10, "c": 1.0}]}
    assert calls["n"] == 3


def test_a_dropped_connection_is_retried(monkeypatch):
    import requests

    from optionsbot.data.providers.polygon import _RequestsHttpClient

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def flaky(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("connection reset")
        return FakeResponse()

    monkeypatch.setattr(requests, "get", flaky)
    assert _RequestsHttpClient(api_key="k", calls_per_minute=0).get("https://example/x", {}) == {"results": []}


def test_server_errors_are_retried_but_client_errors_are_not(monkeypatch):
    """A 500 is worth another attempt; a 400 will fail identically forever and retrying it only
    burns slots against a four-per-minute budget."""
    import requests

    from optionsbot.data.providers.polygon import _RequestsHttpClient

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def flaky(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(status_code=503) if calls["n"] == 1 else FakeResponse()

    monkeypatch.setattr(requests, "get", flaky)
    assert _RequestsHttpClient(api_key="k", calls_per_minute=0).get("https://example/x", {}) == {"results": []}
    assert calls["n"] == 2

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(status_code=400))
    with pytest.raises(RuntimeError):
        _RequestsHttpClient(api_key="k", calls_per_minute=0).get("https://example/x", {})


def test_exhausted_retries_raise_the_error_build_chains_knows_how_to_skip(monkeypatch):
    """DataUnavailableError, not a raw network exception -- that is what lets build_chains drop
    one unreachable contract and carry on rather than losing the whole backfill."""
    import requests

    from optionsbot.data.providers.polygon import _RequestsHttpClient

    _no_sleep(monkeypatch)

    def always_timeout(url, params=None, headers=None, timeout=None):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(requests, "get", always_timeout)
    client = _RequestsHttpClient(api_key="k", calls_per_minute=0, max_retries=3)

    with pytest.raises(DataUnavailableError, match="Timeout"):
        client.get("https://example/x", {})


def test_a_permission_error_fails_immediately_without_burning_retries(monkeypatch):
    import requests

    from optionsbot.data.providers.polygon import _RequestsHttpClient

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def forbidden(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(status_code=403, text="not entitled")

    monkeypatch.setattr(requests, "get", forbidden)
    with pytest.raises(DataUnavailableError, match="not entitled"):
        _RequestsHttpClient(api_key="k", calls_per_minute=0).get("https://example/x", {})
    assert calls["n"] == 1


# ---- the contract-listing cache -------------------------------------------------------------
#
# Listings were the untracked half of a restart's cost: an 18-month weekly backfill lists 83
# expirations per ticker, and every relaunch paid all of them again -- 104 minutes across five
# tickers at 4 calls/min -- before fetching a single new bar.


def test_contract_listings_are_cached_to_disk_and_reused(tmp_path):
    client = make_client(**{"reference/options/contracts": {"results": [contract(700.0)]}})
    provider = PolygonProvider("key", client=client, contract_cache_dir=tmp_path)
    kwargs = dict(expiration=PAST_EXPIRY, strike_min=Decimal("650"), strike_max=Decimal("750"), right=Right.PUT)

    first = provider.list_contracts("SPY", **kwargs)
    second = provider.list_contracts("SPY", **kwargs)

    assert first == second
    assert len(client.calls) == 1, "cached listing was refetched"


def test_a_cached_listing_narrower_than_the_request_is_refetched(tmp_path):
    """The strike band is recomputed per expiration from that expiration's own spot range, so it
    drifts for recent expirations. Returning a narrower cached band would silently thin the
    chain -- invisible in the data, fatal in a backtest."""
    client = make_client(**{"reference/options/contracts": {"results": [contract(700.0)]}})
    provider = PolygonProvider("key", client=client, contract_cache_dir=tmp_path)

    provider.list_contracts(
        "SPY", expiration=PAST_EXPIRY, strike_min=Decimal("690"), strike_max=Decimal("710"), right=Right.PUT
    )
    provider.list_contracts(
        "SPY", expiration=PAST_EXPIRY, strike_min=Decimal("600"), strike_max=Decimal("800"), right=Right.PUT
    )

    assert len(client.calls) == 2, "a wider band was served from a narrower cache"


def test_a_cached_listing_wider_than_the_request_is_filtered_not_refetched(tmp_path):
    client = make_client(
        **{"reference/options/contracts": {"results": [contract(600.0), contract(700.0), contract(800.0)]}}
    )
    provider = PolygonProvider("key", client=client, contract_cache_dir=tmp_path)

    provider.list_contracts(
        "SPY", expiration=PAST_EXPIRY, strike_min=Decimal("500"), strike_max=Decimal("900"), right=Right.PUT
    )
    narrowed = provider.list_contracts(
        "SPY", expiration=PAST_EXPIRY, strike_min=Decimal("650"), strike_max=Decimal("750"), right=Right.PUT
    )

    assert len(client.calls) == 1
    assert [c["strike_price"] for c in narrowed] == [700.0], "cache returned strikes outside the request"


def test_live_expirations_are_never_cached(tmp_path):
    """Strikes are still being added to an expiration that has not passed yet; caching those
    would pin a permanently thin chain."""
    client = make_client(**{"reference/options/contracts": {"results": [contract(700.0)]}})
    provider = PolygonProvider("key", client=client, contract_cache_dir=tmp_path)
    soon = date.today() + timedelta(days=3)
    kwargs = dict(expiration=soon, strike_min=Decimal("650"), strike_max=Decimal("750"), right=Right.PUT)

    provider.list_contracts("SPY", **kwargs)
    provider.list_contracts("SPY", **kwargs)

    assert len(client.calls) == 2
    assert not list(tmp_path.glob("*.json"))


def test_a_truncated_listing_cache_file_is_refetched_rather_than_trusted(tmp_path):
    client = make_client(**{"reference/options/contracts": {"results": [contract(700.0)]}})
    provider = PolygonProvider("key", client=client, contract_cache_dir=tmp_path)
    (tmp_path / f"SPY_{PAST_EXPIRY.isoformat()}_put.json").write_text('{"strike_min": "1", "resu')

    found = provider.list_contracts(
        "SPY", expiration=PAST_EXPIRY, strike_min=Decimal("650"), strike_max=Decimal("750"), right=Right.PUT
    )

    assert found and len(client.calls) == 1


def test_no_contract_cache_dir_means_no_files_and_no_reuse(tmp_path):
    client = make_client(**{"reference/options/contracts": {"results": [contract(700.0)]}})
    provider = PolygonProvider("key", client=client)
    kwargs = dict(expiration=PAST_EXPIRY, strike_min=Decimal("650"), strike_max=Decimal("750"), right=Right.PUT)

    provider.list_contracts("SPY", **kwargs)
    provider.list_contracts("SPY", **kwargs)

    assert len(client.calls) == 2
    assert not list(tmp_path.iterdir())


def test_underlying_bars_are_memoised_within_a_run(tmp_path):
    """fetch_data asks for the same window twice per ticker -- once to size its estimate, once
    inside build_chains. At 4 calls/min the duplicate is 15 wasted seconds per ticker per run."""
    client = make_client(**{"aggs/ticker/SPY": {"results": [bar(JUN10, 600.0)]}})
    provider = PolygonProvider("key", client=client)

    first = provider.underlying_bars("SPY", date(2026, 6, 1), date(2026, 6, 10))
    second = provider.underlying_bars("SPY", date(2026, 6, 1), date(2026, 6, 10))

    assert first == second
    assert len(client.calls) == 1
