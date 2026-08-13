"""Tests for the Polygon provider's parsing/orchestration logic, using a fake HTTP client so
no network access or real API key is required. These verify our code correctly turns Polygon's
documented JSON shapes into domain models; they do NOT verify Polygon's actual live response
shape, which should be smoke-tested once a real API key is available (see scripts/fetch_data.py
docstring)."""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.core.enums import Right
from optionsbot.data.providers.base import DataUnavailableError
from optionsbot.data.providers.polygon import PolygonProvider

UNDERLYING_PRICE_RESPONSE = {"close": 450.00}

CONTRACTS_RESPONSE = {
    "results": [
        {
            "ticker": "O:SPY260918P00445000",
            "underlying_ticker": "SPY",
            "expiration_date": "2026-09-18",
            "strike_price": 445.0,
            "contract_type": "put",
            "open_interest": 1200,
        },
        {
            "ticker": "O:SPY260918P00450000",
            "underlying_ticker": "SPY",
            "expiration_date": "2026-09-18",
            "strike_price": 450.0,
            "contract_type": "put",
            "open_interest": 3400,
        },
    ]
}


class FakeHttpClient:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict) -> dict:
        self.calls.append((url, params))
        for key, response in self._responses.items():
            if key in url:
                return response
        raise AssertionError(f"no fixture registered for url containing a known key: {url}")


def make_provider(underlying_price=None, contracts=None, daily_close=8.50):
    responses = {
        "/v1/open-close/": underlying_price if underlying_price is not None else UNDERLYING_PRICE_RESPONSE,
        "/v3/reference/options/contracts": contracts if contracts is not None else CONTRACTS_RESPONSE,
        "/v2/aggs/ticker/": {"results": [{"c": daily_close}]},
    }
    client = FakeHttpClient(responses)
    return PolygonProvider(api_key="test-key", client=client), client


def test_requires_api_key():
    with pytest.raises(ValueError):
        PolygonProvider(api_key="")


def test_get_underlying_price():
    provider, _ = make_provider()
    price = provider.get_underlying_price("SPY", date(2026, 8, 19))
    assert price == Decimal("450.0")


def test_get_underlying_price_raises_when_missing():
    provider, _ = make_provider(underlying_price={})
    with pytest.raises(DataUnavailableError):
        provider.get_underlying_price("SPY", date(2026, 8, 19))


def test_get_chain_parses_contracts_and_computes_greeks():
    provider, client = make_provider(daily_close=6.00)
    chain = provider.get_chain("SPY", date(2026, 8, 19))

    assert chain.underlying == "SPY"
    assert chain.underlying_price == Decimal("450.0")
    assert len(chain.quotes) == 2

    quote = next(q for q in chain.quotes if q.contract.strike == Decimal("450.0"))
    assert quote.contract.right == Right.PUT
    assert quote.open_interest == 3400
    assert quote.last == Decimal("6.0")
    # A put ~30 DTE priced at $6 with spot=strike=450 should solve to a plausible IV and a
    # delta in the middle of (-1, 0) -- pricing correctness itself is pricing.py's job; this
    # just confirms parsing actually wired the numbers through end to end.
    assert quote.implied_volatility is not None
    assert quote.delta is not None
    assert Decimal("-1") < quote.delta < Decimal("0")


def test_get_chain_skips_contracts_with_no_trading_that_day():
    provider, _ = make_provider(daily_close=None)
    with pytest.raises(DataUnavailableError):
        provider.get_chain("SPY", date(2026, 8, 19))


def test_get_chain_raises_when_no_contracts_found():
    provider, _ = make_provider(contracts={"results": []})
    with pytest.raises(DataUnavailableError):
        provider.get_chain("SPY", date(2026, 8, 19))
