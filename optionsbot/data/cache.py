"""Local Parquet cache for option chains, keyed by (underlying, date).

A historical trading day's chain never changes once it's over, so once fetched from a provider
it's written to disk and never re-fetched -- important given free-tier API rate limits across a
6-month, multi-underlying backfill.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from optionsbot.core.enums import Right
from optionsbot.core.models import Chain, OptionContract, OptionQuote
from optionsbot.data.providers.base import ChainProvider


def _opt_decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return Decimal(str(value))


def _opt_str(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)


class ParquetChainCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)

    def _path(self, underlying: str, as_of: date) -> Path:
        return self.cache_dir / underlying.upper() / f"{as_of.isoformat()}.parquet"

    def has(self, underlying: str, as_of: date) -> bool:
        return self._path(underlying, as_of).exists()

    def write(self, chain: Chain) -> Path:
        path = self._path(chain.underlying, chain.as_of)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "as_of": chain.as_of.isoformat(),
                "underlying_price": str(chain.underlying_price),
                "expiration": q.contract.expiration.isoformat(),
                "strike": str(q.contract.strike),
                "right": q.contract.right.value,
                "multiplier": q.contract.multiplier,
                "occ_symbol": q.contract.occ_symbol,
                "bid": str(q.bid) if q.bid is not None else None,
                "ask": str(q.ask) if q.ask is not None else None,
                "last": str(q.last) if q.last is not None else None,
                "volume": q.volume,
                "open_interest": q.open_interest,
                "implied_volatility": str(q.implied_volatility) if q.implied_volatility is not None else None,
                "delta": str(q.delta) if q.delta is not None else None,
                "gamma": str(q.gamma) if q.gamma is not None else None,
                "theta": str(q.theta) if q.theta is not None else None,
                "vega": str(q.vega) if q.vega is not None else None,
            }
            for q in chain.quotes
        ]
        pd.DataFrame(rows).to_parquet(path, index=False)
        return path

    def read(self, underlying: str, as_of: date) -> Chain:
        path = self._path(underlying, as_of)
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_parquet(path)
        if df.empty:
            return Chain(underlying=underlying.upper(), as_of=as_of, underlying_price=Decimal("0"), quotes=[])

        underlying_price = Decimal(str(df.iloc[0]["underlying_price"]))
        quotes = []
        for _, row in df.iterrows():
            contract = OptionContract(
                underlying=underlying.upper(),
                expiration=date.fromisoformat(row["expiration"]),
                strike=Decimal(str(row["strike"])),
                right=Right(row["right"]),
                multiplier=int(row["multiplier"]),
                occ_symbol=_opt_str(row["occ_symbol"]),
            )
            quotes.append(
                OptionQuote(
                    contract=contract,
                    bid=_opt_decimal(row["bid"]),
                    ask=_opt_decimal(row["ask"]),
                    last=_opt_decimal(row["last"]),
                    volume=int(row["volume"]),
                    open_interest=int(row["open_interest"]),
                    implied_volatility=_opt_decimal(row["implied_volatility"]),
                    delta=_opt_decimal(row["delta"]),
                    gamma=_opt_decimal(row["gamma"]),
                    theta=_opt_decimal(row["theta"]),
                    vega=_opt_decimal(row["vega"]),
                )
            )
        return Chain(underlying=underlying.upper(), as_of=as_of, underlying_price=underlying_price, quotes=quotes)


class CachingChainProvider(ChainProvider):
    """Wraps any ChainProvider with a transparent read-through Parquet cache. The wrapped
    provider is never called twice for the same (underlying, date)."""

    def __init__(self, provider: ChainProvider, cache: ParquetChainCache):
        self._provider = provider
        self._cache = cache

    def has_cached(self, underlying: str, as_of: date) -> bool:
        return self._cache.has(underlying, as_of)

    def get_chain(self, underlying: str, as_of: date) -> Chain:
        if self._cache.has(underlying, as_of):
            return self._cache.read(underlying, as_of)
        chain = self._provider.get_chain(underlying, as_of)
        self._cache.write(chain)
        return chain

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        return self._provider.get_underlying_price(underlying, as_of)
