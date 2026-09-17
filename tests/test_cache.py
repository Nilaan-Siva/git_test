"""Tests for the Parquet chain cache and the CachingChainProvider wrapper -- in particular
that a wrapped provider is truly never called twice for the same (underlying, date)."""
from datetime import date
from decimal import Decimal

import pytest

from optionsbot.core.enums import Right
from optionsbot.core.models import Chain, OptionContract, OptionQuote
from optionsbot.data.cache import CachingChainProvider, ParquetChainCache
from optionsbot.data.providers.base import ChainProvider


def make_chain(as_of: date = date(2026, 8, 19)) -> Chain:
    contract_put = OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("445"), right=Right.PUT)
    contract_call = OptionContract(underlying="SPY", expiration=date(2026, 9, 18), strike=Decimal("455"), right=Right.CALL)
    return Chain(
        underlying="SPY",
        as_of=as_of,
        underlying_price=Decimal("450.25"),
        quotes=[
            OptionQuote(
                contract=contract_put,
                bid=Decimal("1.20"),
                ask=Decimal("1.30"),
                last=Decimal("1.25"),
                volume=150,
                open_interest=2000,
                implied_volatility=Decimal("0.18"),
                delta=Decimal("-0.28"),
                gamma=Decimal("0.015"),
                theta=Decimal("-0.04"),
                vega=Decimal("0.12"),
            ),
            OptionQuote(contract=contract_call, bid=None, ask=None, last=Decimal("2.10"), volume=0, open_interest=50),
        ],
    )


class FakeProvider(ChainProvider):
    def __init__(self, chain: Chain):
        self._chain = chain
        self.get_chain_calls = 0

    def get_chain(self, underlying, as_of):
        self.get_chain_calls += 1
        return self._chain

    def get_underlying_price(self, underlying, as_of):
        return self._chain.underlying_price


def test_cache_miss_then_hit_round_trip(tmp_path):
    cache = ParquetChainCache(tmp_path)
    chain = make_chain()
    assert not cache.has("SPY", chain.as_of)

    cache.write(chain)
    assert cache.has("SPY", chain.as_of)

    reloaded = cache.read("SPY", chain.as_of)
    assert reloaded.underlying == "SPY"
    assert reloaded.underlying_price == Decimal("450.25")
    assert len(reloaded.quotes) == 2

    put_quote = next(q for q in reloaded.quotes if q.contract.right == Right.PUT)
    assert put_quote.bid == Decimal("1.20")
    assert put_quote.ask == Decimal("1.30")
    assert put_quote.delta == Decimal("-0.28")
    assert put_quote.open_interest == 2000

    call_quote = next(q for q in reloaded.quotes if q.contract.right == Right.CALL)
    assert call_quote.bid is None  # None must round-trip through Parquet as None, not NaN/"nan"
    assert call_quote.ask is None
    assert call_quote.last == Decimal("2.10")


def test_read_raises_for_uncached_day(tmp_path):
    cache = ParquetChainCache(tmp_path)
    with pytest.raises(FileNotFoundError):
        cache.read("SPY", date(2026, 8, 19))


def test_caching_provider_only_fetches_once(tmp_path):
    chain = make_chain()
    fake = FakeProvider(chain)
    provider = CachingChainProvider(fake, ParquetChainCache(tmp_path))

    first = provider.get_chain("SPY", chain.as_of)
    second = provider.get_chain("SPY", chain.as_of)

    assert fake.get_chain_calls == 1  # second call was served entirely from cache
    assert first.underlying_price == second.underlying_price == Decimal("450.25")


def test_caching_provider_has_cached_reflects_writes(tmp_path):
    chain = make_chain()
    fake = FakeProvider(chain)
    provider = CachingChainProvider(fake, ParquetChainCache(tmp_path))

    assert not provider.has_cached("SPY", chain.as_of)
    provider.get_chain("SPY", chain.as_of)
    assert provider.has_cached("SPY", chain.as_of)


def test_caching_provider_different_dates_both_fetch(tmp_path):
    fake = FakeProvider(make_chain(as_of=date(2026, 8, 19)))
    provider = CachingChainProvider(fake, ParquetChainCache(tmp_path))

    provider.get_chain("SPY", date(2026, 8, 19))
    fake._chain = make_chain(as_of=date(2026, 8, 20))
    provider.get_chain("SPY", date(2026, 8, 20))

    assert fake.get_chain_calls == 2
