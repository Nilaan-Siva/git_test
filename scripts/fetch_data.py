#!/usr/bin/env python3
"""CLI: backfill the local Parquet cache with historical option chains.

Requires OPTIONSBOT_POLYGON_API_KEY in .env.

**Read the budget before running this.** The free tier allows 5 API calls per minute, which is
the constraint that shapes everything here. The provider fetches each option contract's entire
price history in a single call (see optionsbot/data/providers/polygon.py), so the cost scales
with the number of *contracts*, not contracts x days:

    calls ~= 1 + expirations * (1 + strikes_per_expiration)

At 5/min that is roughly:

    SPY, 6 months, monthly expirations, 5-point strike grid   ~15 min
    SPY, 18 months (6 backtest + 12 warmup), 5-point grid     ~40 min
    4 underlyings, 18 months, 5-point grid                    ~3 hours
    12 underlyings, 18 months, 1-point grid                   ~30+ hours

Use --strike-step 5 unless you specifically need $1 strikes; it cuts the budget roughly
fivefold, and a 5-wide spread never looks at the intervening strikes anyway. Progress is written
to the cache as it goes, so an interrupted run resumes without refetching.

Usage:
    python scripts/fetch_data.py --tickers SPY --months 18 --strike-step 5
    python scripts/fetch_data.py --dry-run --tickers SPY QQQ --months 18   # just the estimate
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal

from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import UniverseConfig
from optionsbot.config.settings import CONFIG_DIR, get_settings
from optionsbot.data.cache import ParquetChainCache
from optionsbot.data.providers.base import DataUnavailableError
from optionsbot.data.providers.polygon import (
    FREE_TIER_CALLS_PER_MINUTE,
    PolygonProvider,
    monthly_expirations,
)
from optionsbot.ops.logging import configure_logging, get_logger

logger = get_logger(__name__)


def estimate_calls(n_expirations: int, strikes_per_expiration: int) -> int:
    return 1 + n_expirations * (1 + strikes_per_expiration)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=18, help="trailing months to backfill (backtest + warmup)")
    parser.add_argument("--tickers", nargs="*", default=None, help="override universe.yaml")
    parser.add_argument("--strike-step", type=Decimal, default=Decimal("5"), help="keep only strikes on this grid")
    parser.add_argument("--dry-run", action="store_true", help="print the API-call estimate and exit")
    parser.add_argument("--force", action="store_true", help="refetch days already cached")
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
    # Polygon's free tier stops at 2 years; asking for more just returns 403s.
    earliest = end - timedelta(days=730)
    if start < earliest:
        logger.warning(f"free tier caps history at 2 years; clamping start {start} -> {earliest}")
        start = earliest

    expirations = monthly_expirations(start + timedelta(days=25), end + timedelta(days=60))
    # A 12%-below-to-3%-above band on a 5-point grid over a ~700 underlying is ~21 strikes.
    strikes_guess = int((0.15 * 700) / float(args.strike_step)) if args.strike_step else 100
    per_ticker = estimate_calls(len(expirations), strikes_guess)
    total = per_ticker * len(tickers)
    minutes = total / FREE_TIER_CALLS_PER_MINUTE

    logger.info(
        f"plan: {len(tickers)} ticker(s), {start}..{end}, {len(expirations)} monthly expirations, "
        f"strike step {args.strike_step}"
    )
    logger.info(f"estimated ~{total} API calls at {FREE_TIER_CALLS_PER_MINUTE}/min -> ~{minutes/60:.1f} hours")
    if args.dry_run:
        return 0

    provider = PolygonProvider(settings.polygon_api_key, strike_step=args.strike_step)
    cache = ParquetChainCache(settings.cache_dir)

    written, skipped, failed = 0, 0, 0
    for ticker in tickers:
        logger.info(f"--- {ticker} ---")
        try:
            for chain in provider.build_chains(ticker, start, end, expirations=expirations):
                if not args.force and cache.has(ticker, chain.as_of):
                    skipped += 1
                    continue
                cache.write(chain)
                written += 1
                if written % 20 == 0:
                    logger.info(f"{ticker}: {written} days cached (latest {chain.as_of}, {len(chain.quotes)} quotes)")
        except DataUnavailableError as exc:
            failed += 1
            logger.warning(f"{ticker}: {exc}")
        except KeyboardInterrupt:
            logger.info("interrupted -- progress is cached, rerun to resume")
            break

    logger.info(f"done: written={written} skipped(cached)={skipped} tickers_failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
