#!/usr/bin/env python3
"""CLI: backfill the local Parquet cache with historical option chains for the configured
universe over a trailing window of trading days.

Requires OPTIONSBOT_POLYGON_API_KEY (see .env.example). NOT YET RUN against a real API key in
this repo -- do a small smoke test (e.g. --months 1 --tickers SPY) once a key is available,
since Polygon's exact response shape should be verified live; see the caveats documented in
optionsbot/data/providers/polygon.py.

Usage:
    python scripts/fetch_data.py --months 6
    python scripts/fetch_data.py --months 1 --tickers SPY
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import UniverseConfig
from optionsbot.config.settings import CONFIG_DIR, get_settings
from optionsbot.core.clock import trading_days_between
from optionsbot.data.cache import CachingChainProvider, ParquetChainCache
from optionsbot.data.providers.base import DataUnavailableError
from optionsbot.data.providers.polygon import PolygonProvider
from optionsbot.ops.logging import configure_logging, get_logger

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=6, help="trailing months of history to backfill")
    parser.add_argument("--tickers", nargs="*", default=None, help="override the universe.yaml ticker list")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_data_dirs()

    if not settings.polygon_api_key:
        logger.error("OPTIONSBOT_POLYGON_API_KEY is not set -- copy .env.example to .env and fill it in")
        return 1

    universe = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)
    tickers = args.tickers or universe.tickers

    end = date.today()
    start = end - timedelta(days=int(args.months * 30.44))
    days = trading_days_between(start, end)

    provider = CachingChainProvider(
        PolygonProvider(api_key=settings.polygon_api_key),
        ParquetChainCache(settings.cache_dir),
    )

    fetched, skipped, failed = 0, 0, 0
    for ticker in tickers:
        for day in days:
            if provider.has_cached(ticker, day):
                skipped += 1
                continue
            try:
                chain = provider.get_chain(ticker, day)
                fetched += 1
                logger.info(f"cached {ticker} {day}: {len(chain.quotes)} contracts")
            except DataUnavailableError as exc:
                failed += 1
                logger.warning(f"no data for {ticker} {day}: {exc}")

    logger.info(f"done: fetched={fetched} skipped(cached)={skipped} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
