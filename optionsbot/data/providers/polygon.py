"""Polygon.io historical options data provider.

Free-tier reality (as researched during planning -- verify with a live smoke test once a real
API key is available, since provider response shapes do drift): Polygon's free plan gives
historical daily OHLC for option contracts (up to ~2 years back) but NOT historical greeks or
implied volatility -- those require a paid plan. This provider fetches the contract reference
and daily close, then computes IV and greeks locally via optionsbot.data.pricing (Black-Scholes
on the daily close, using the underlying's own daily close as spot).

Historical bid/ask is similarly unavailable on the free tier: OptionQuote.bid/ask are left None
here rather than fabricated. backtest/slippage.py (Phase 3) is responsible for the resulting
synthetic-spread assumption -- a data provider should report what it actually knows, not invent
quotes.

Network calls are isolated behind the injectable `HttpClient` seam so `get_chain`'s parsing and
orchestration logic can be (and is, in tests/test_polygon_provider.py) unit-tested with fixture
JSON and no real network access or API key.

Known scaling limitation: this fetches one HTTP call per contract per day (contracts-list + one
daily-close call per contract), which is rate-limit-heavy for a full 6-month backfill on a free
plan. The Parquet cache (data/cache.py) ensures no (underlying, date) pair is ever re-fetched,
but the *first* backfill will be slow. Polygon's grouped-daily-bars endpoint can fetch every
option ticker's OHLC for one day in a single call and would be the natural optimization if rate
limits become a blocker -- not implemented yet since its free-tier availability is unconfirmed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from optionsbot.core.enums import Right
from optionsbot.core.models import Chain, OptionContract, OptionQuote
from optionsbot.data.providers.base import ChainProvider, DataUnavailableError
from optionsbot.data.pricing import (
    DEFAULT_RISK_FREE_RATE,
    bs_greeks,
    implied_volatility,
    years_to_expiration,
)

POLYGON_BASE_URL = "https://api.polygon.io"


class HttpClient(Protocol):
    """Minimal seam over whatever HTTP library is actually used, so tests can inject a fake
    without hitting the network or requiring `requests` to be installed."""

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class _RequestsHttpClient:
    api_key: str
    timeout_seconds: float = 15.0

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        import requests  # local import: keeps `requests` an optional ("data" extra) dependency

        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = requests.get(url, params=params, headers=headers, timeout=self.timeout_seconds)
        resp.raise_for_status()
        return resp.json()


def _to_decimal(value: float | int | None, places: int = 4) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(float(value), places)))


class PolygonProvider(ChainProvider):
    def __init__(
        self,
        api_key: str,
        *,
        client: HttpClient | None = None,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    ):
        if not api_key:
            raise ValueError("PolygonProvider requires a non-empty api_key")
        self._client = client or _RequestsHttpClient(api_key=api_key)
        self._risk_free_rate = risk_free_rate

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        raw = self._client.get(
            f"{POLYGON_BASE_URL}/v1/open-close/{underlying.upper()}/{as_of.isoformat()}",
            params={"adjusted": "true"},
        )
        close = raw.get("close")
        if close is None:
            raise DataUnavailableError(f"no underlying close for {underlying} on {as_of}")
        return Decimal(str(close))

    def get_chain(self, underlying: str, as_of: date) -> Chain:
        underlying_price = self.get_underlying_price(underlying, as_of)
        contracts_raw = self._fetch_contracts(underlying, as_of)
        quotes = []
        for contract_raw in contracts_raw:
            quote = self._quote_for_contract(contract_raw, underlying_price=underlying_price, as_of=as_of)
            if quote is not None:
                quotes.append(quote)
        if not quotes:
            raise DataUnavailableError(f"no priceable option contracts for {underlying} as of {as_of}")
        return Chain(underlying=underlying.upper(), as_of=as_of, underlying_price=underlying_price, quotes=quotes)

    def _fetch_contracts(self, underlying: str, as_of: date) -> list[dict[str, Any]]:
        raw = self._client.get(
            f"{POLYGON_BASE_URL}/v3/reference/options/contracts",
            params={
                "underlying_ticker": underlying.upper(),
                "as_of": as_of.isoformat(),
                "limit": 1000,
                "expired": "true",
            },
        )
        return raw.get("results", [])

    def _fetch_daily_close(self, option_ticker: str, as_of: date) -> float | None:
        raw = self._client.get(
            f"{POLYGON_BASE_URL}/v2/aggs/ticker/{option_ticker}/range/1/day/{as_of.isoformat()}/{as_of.isoformat()}",
            params={"adjusted": "true"},
        )
        results = raw.get("results") or []
        if not results:
            return None
        return results[0].get("c")

    def _quote_for_contract(
        self, contract_raw: dict[str, Any], *, underlying_price: Decimal, as_of: date
    ) -> OptionQuote | None:
        ticker = contract_raw.get("ticker")
        expiration = contract_raw.get("expiration_date")
        strike = contract_raw.get("strike_price")
        contract_type = contract_raw.get("contract_type")
        if not (ticker and expiration and strike and contract_type):
            return None

        close = self._fetch_daily_close(ticker, as_of)
        if close is None or close <= 0:
            return None  # no trades that day; skip rather than fabricate a quote

        contract = OptionContract(
            underlying=contract_raw.get("underlying_ticker", ""),
            expiration=date.fromisoformat(expiration),
            strike=Decimal(str(strike)),
            right=Right.CALL if contract_type == "call" else Right.PUT,
            occ_symbol=ticker,
        )

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
            volume=0,
            open_interest=int(contract_raw.get("open_interest", 0) or 0),
            implied_volatility=_to_decimal(iv) if iv is not None else None,
            delta=_to_decimal(greeks.get("delta")) if greeks else None,
            gamma=_to_decimal(greeks.get("gamma"), places=6) if greeks else None,
            theta=_to_decimal(greeks.get("theta")) if greeks else None,
            vega=_to_decimal(greeks.get("vega")) if greeks else None,
        )
