#!/usr/bin/env python3
"""CLI: backfill the local Parquet cache with historical option chains.

Requires OPTIONSBOT_POLYGON_API_KEY in .env.

**Read the budget before running this.** The free tier documents 5 API calls per minute; we
pace at 4, because at exactly 5 the server still rejects requests. Every projection must use
the pacing rate, not the documented one -- using 5 made every estimate here run 25% optimistic,
which is how a "2.3 hour" job turned out to take nearly three. The provider fetches each
contract's entire price history in one call (see optionsbot/data/providers/polygon.py), so the
cost scales with the number of *contracts*, not contracts x days:

    calls ~= 1 + expirations * rights * (1 + strikes_per_expiration)

`rights` is the easy factor of two to forget: fetching puts *and* calls doubles everything.
`strikes_per_expiration` is the other one: the strike band is a FRACTION of each ticker's own
price (~15% wide by default), so a $770 name and a $58 one need very different strike counts at
the same --strike-step. --dry-run fetches one cheap recent-price lookup per ticker to estimate
this for real rather than assuming every ticker is SPY-priced.

Every contract's bars are cached individually under <cache_dir>/bars, so an interrupted run
replays from disk in seconds and only fetches what it never reached. Always start with
--dry-run. Use --puts-only unless you intend to trade iron condors, which ship disabled.

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
    DEFAULT_THROTTLE_CALLS_PER_MINUTE,
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
            "keep only strikes on this grid. put_credit_spread targets its width as a fraction "
            "of each underlying's spot price, not a fixed points value, so a fine grid (default "
            "1) is what lets it find a close listed strike on every ticker. If iron_condor is "
            "enabled this MUST still divide its fixed spread_width, or its protective leg will "
            "not be in the cache and every proposal dies on long_strike_not_listed."
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

    # put_credit_spread no longer has a fixed spread_width to derive a grid from -- it targets a
    # width as a fraction of spot and searches the real chain for the nearest listed strike, so a
    # fine default grid (1) is what lets that search actually find something close on every
    # ticker. iron_condor is the one strategy still keyed to a fixed points width; if it's
    # enabled, a grid coarser than that width silently produces a cache it can never trade (it
    # looks `width` points from its short strike and finds nothing there), so that check stays.
    condor_width = strategies.iron_condor.spread_width if strategies.iron_condor.enabled else None
    required_step = condor_width if condor_width is not None else Decimal("1")
    strike_step = args.strike_step if args.strike_step is not None else required_step
    if condor_width is not None and strike_step > 0 and condor_width % strike_step != 0:
        logger.error(
            f"--strike-step {strike_step} does not divide iron_condor's spread_width {condor_width}; "
            f"its protective leg would be missing from the cache. Use {required_step} or a divisor of it."
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

    # Per-contract bar cache: chains can only be assembled once every contract is loaded, so
    # without this an interruption at 95% discards 95% of a multi-hour rate-limit budget.
    provider = PolygonProvider(
        settings.polygon_api_key, strike_step=strike_step, bar_cache_dir=settings.cache_dir / "bars"
    )

    # The strike band is a fraction of each ticker's OWN price (see build_chains), so the call
    # count is not one number shared by every ticker -- a $58 name needs a fraction of the
    # strikes a $770 one does at the same strike_step. One lightweight bars call per ticker (a
    # few days of history) gets a real recent price to estimate against, instead of assuming
    # every ticker is SPY-priced and overestimating the cheap ones by 5-10x.
    band = provider.strike_band_below + provider.strike_band_above
    logger.info(
        f"plan: {len(tickers)} ticker(s), {start}..{end}, {len(expirations)} monthly expirations, "
        f"strike step {strike_step}, {'puts only' if args.puts_only else 'puts and calls'}"
    )
    total = 0
    for ticker in tickers:
        try:
            recent = provider.underlying_bars(ticker, end - timedelta(days=10), end)
            price = float(list(recent.values())[-1]["c"]) if recent else None
        except DataUnavailableError:
            price = None
        if price is None:
            logger.warning(f"{ticker}: couldn't fetch a recent price for the estimate, assuming $700 (upper bound)")
            price = 700.0
        strikes_guess = max(1, int((band * price) / float(strike_step))) if strike_step else 100
        per_ticker = estimate_calls(len(expirations), strikes_guess, n_rights)
        total += per_ticker
        logger.info(f"  {ticker}: ~${price:.0f}, ~{strikes_guess} strikes/expiration -> ~{per_ticker} calls")
    minutes = total / DEFAULT_THROTTLE_CALLS_PER_MINUTE

    logger.info(f"estimated ~{total} API calls at {DEFAULT_THROTTLE_CALLS_PER_MINUTE}/min -> ~{minutes/60:.1f} hours")
    if args.dry_run:
        return 0

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

    if provider.skipped_contracts:
        logger.warning(
            f"{len(provider.skipped_contracts)} contract(s) could not be loaded and are missing "
            f"from their chains, e.g. {provider.skipped_contracts[0][0]}"
        )
    logger.info(f"done: written={written} skipped(cached)={skipped} tickers_failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
