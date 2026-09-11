"""ChainProvider: the interface every option-data source implements.

Strategies, the backtest engine, and (from Phase 5) the live paper-trading loop all consume
option chains through this interface, so swapping Polygon (historical) for IBKR (live) -- or
adding another provider later -- never touches strategy or risk code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal

from optionsbot.core.models import Chain


class DataUnavailableError(RuntimeError):
    """Raised when a provider cannot produce data for the requested underlying/date."""


class ChainProvider(ABC):
    @abstractmethod
    def get_chain(self, underlying: str, as_of: date) -> Chain:
        """The full options chain for `underlying` as of `as_of`. Raises DataUnavailableError
        if no chain can be produced (e.g. no trading that day, or the underlying is unknown)."""

    @abstractmethod
    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        """The underlying's price as of `as_of`, independent of the option chain itself."""
