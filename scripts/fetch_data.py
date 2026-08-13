#!/usr/bin/env python3
"""CLI: backfill the local Parquet cache with historical option chains.

Requires OPTIONSBOT_POLYGON_API_KEY in .env.

**Read the budget before running this.** The free tier allows 5 API calls per minute, which is
the constraint that shapes everything here. The provider fetches each option contract's entire
price history in a single call (see optionsbot/data/providers/polygon.py), so the cost scales
with the number of *contracts*, not contracts x days:

    calls ~= 1 + expirations * rights * (1 + strikes_per_expiration)

Both halves of that product matter, and `rights` is the one that is easy to forget: fetching
puts *and* calls doubles every number below. At 5 calls/min, with monthly expirations and a
5-point strike grid:

    SPY, 18 months, puts only                                 ~1.4 hours
    SPY, 18 months, puts and calls                            ~2.7 hours
    4 underlyings, 18 months, puts only                       ~5.5 hours
    12 underlyings, 18 months, puts and calls                 ~32 hours

Always start with --dry-run; it prints the estimate and exits. Use --strike-step 5 unless you
specifically need $1 strikes, and --puts-only unless you intend to trade iron condors, which are
disabled by default. Progress is written to the cache as it goes, so an interrupted run resumes
where it stopped rather than starting over.

Usage:
    python scripts/fetch_data.py --dry-run --tickers SPY --months 18      # estimate first
    python scripts/fetch_data.py --tickers SPY --months 18 --puts-only
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal

from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import StrategiesConfig, UniverseConfig
from optionsbot.config.settings import CONFIG_DIR, get_settings
from optionsbot.core.enums import Right
from optionsbot.data.cache import ParquetChainCache
from optionsbot.data.providers.base import DataUnavailableError
from optionsbot.data.providers.polygon import (
    FREE_TIER_CALLS_PER_MINUTE,
    PolygonProvider,
    monthly_expirations,
)
from optionsbot.ops.logging import configure_logging, get_logger

logger = get_logger(__name__)


def estimate_calls(n_expirations: int, strikes_per_expiration: int, n_rights: int) -> int:
    """Contracts, not contract-days -- and both rights, which is the easy factor-of-two to miss.

    One listing call per (expiration, right), then one aggregates call per contract covering the
    entire window.
    """
    listing = n_expirations * n_rights
    contracts = n_expirations * strikes_per_expiration * n_rights
    return 1 + listing + contracts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=18, help="trailing months to backfill (backtest + warmup)")
    parser.add_argument("--tickers", nargs="*", default=None, help="override universe.yaml")
    parser.add_argument(
        "--strike-step",
        type=Decimal,
        default=None,
        help=(
            "keep only strikes on this grid. MUST divide strategies.yaml's spread_width, or the "
            "protective leg will not be in the cache and every proposal dies on "
            "long_strike_not_listed. Defaults to the GCD of the configured spread widths."
        ),
    )
    parser.add_argument(
        "--puts-only",
        action="store_true",
        help="fetch puts only -- halves the backfill; enough for put credit spreads, not condors",
    )
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
    strategies = load_yaml_config(CONFIG_DIR / "strategies.yaml", StrategiesConfig)
    tickers = args.tickers or universe.tickers

    # A strike grid coarser than the spread width silently produces a cache the strategy cannot
    # trade: it finds a short strike, looks `width` points below for the protective leg, and
    # never finds one. Every day rejects with long_strike_not_listed and the backtest reports
    # zero trades as though no setup qualified. Derive the grid from config rather than let the
    # two drift apart.
    widths = [p.spread_width for p in (strategies.put_credit_spread, strategies.iron_condor) if p.enabled]
    required_step = min(widths) if widths else Decimal("1")
    strike_step = args.strike_step if args.strike_step is not None else required_step
    if strike_step > 0 and any(w % strike_step != 0 for w in widths):
        logger.error(
            f"--strike-step {strike_step} does not divide every enabled spread_width {widths}; "
            f"the protective leg would be missing from the cache. Use {required_step} or a divisor of it."
        )
        return 1

    end = date.today()
    start = end - timedelta(days=int(args.months * 30.44))
    # Polygon's free tier stops at 2 years; asking for more just returns 403s.
    earliest = end - timedelta(days=730)
    if start < earliest:
        logger.warning(f"free tier caps history at 2 years; clamping start {start} -> {earliest}")
        start = earliest

    expirations = monthly_expirations(start + timedelta(days=25), end + timedelta(days=60))
    rights = (Right.PUT,) if args.puts_only else None
    n_rights = 1 if args.puts_only else 2
    # A 12%-below-to-3%-above band on a 5-point grid over a ~700 underlying is ~21 strikes.
    strikes_guess = max(1, int((0.15 * 700) / float(strike_step))) if strike_step else 100
    per_ticker = estimate_calls(len(expirations), strikes_guess, n_rights)
    total = per_ticker * len(tickers)
    minutes = total / FREE_TIER_CALLS_PER_MINUTE

    logger.info(
        f"plan: {len(tickers)} ticker(s), {start}..{end}, {len(expirations)} monthly expirations, "
        f"strike step {strike_step}, {'puts only' if args.puts_only else 'puts and calls'}"
    )
    logger.info(f"estimated ~{total} API calls at {FREE_TIER_CALLS_PER_MINUTE}/min -> ~{minutes/60:.1f} hours")
    if args.dry_run:
        return 0

    provider = PolygonProvider(settings.polygon_api_key, strike_step=strike_step)
    cache = ParquetChainCache(settings.cache_dir)

    written, skipped, failed = 0, 0, 0
    for ticker in tickers:
        logger.info(f"--- {ticker} ---")
        try:
            for chain in provider.build_chains(ticker, start, end, expirations=expirations, rights=rights):
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
