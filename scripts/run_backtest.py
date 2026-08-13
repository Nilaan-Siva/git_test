#!/usr/bin/env python3
"""CLI: run a backtest and print a metrics report.

Every strategy is judged on the **pessimistic** run -- full spread crossed on every leg, wide
assumed markets. If expectancy is only positive under optimistic fills, the "edge" is fill
quality you will not get, and the strategy is rejected. Run both and compare; the gap between
them is the slippage drag you are betting against.

    # against cached Polygon data (requires scripts/fetch_data.py to have been run)
    python scripts/run_backtest.py --months 6 --slippage pessimistic

    # against the deterministic synthetic market -- engine smoke test, NOT evidence of edge
    python scripts/run_backtest.py --source synthetic --months 6 --slippage all
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal

from optionsbot.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from optionsbot.backtest.slippage import SlippageModel
from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import RiskConfig, StrategiesConfig, UniverseConfig
from optionsbot.config.settings import CONFIG_DIR, get_settings
from optionsbot.data.cache import CachingChainProvider, ParquetChainCache
from optionsbot.data.providers.base import ChainProvider
from optionsbot.data.providers.synthetic import SyntheticChainProvider
from optionsbot.ops.logging import configure_logging
from optionsbot.strategy.registry import build_enabled_strategies

SLIPPAGE_MODELS = {
    "optimistic": SlippageModel.optimistic,
    "realistic": SlippageModel.realistic,
    "pessimistic": SlippageModel.pessimistic,
}


def build_provider(source: str, tickers: list[str], start: date, end: date, seed: int) -> ChainProvider:
    if source == "synthetic":
        return SyntheticChainProvider(start=start, end=end, underlyings=tuple(tickers), seed=seed)
    settings = get_settings()
    # Cache-only: a backtest must never depend on a live API call, both for reproducibility and
    # because the free tier's rate limit would make a six-month run take hours.
    return CachingChainProvider(_NoRemote(), ParquetChainCache(settings.cache_dir))


class _NoRemote(ChainProvider):
    """Stands in for the upstream provider so a cache miss fails loudly instead of hitting the
    network mid-backtest."""

    def get_chain(self, underlying: str, as_of: date):
        from optionsbot.data.providers.base import DataUnavailableError

        raise DataUnavailableError(
            f"{underlying} {as_of} is not in the local cache -- run scripts/fetch_data.py first"
        )

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        from optionsbot.data.providers.base import DataUnavailableError

        raise DataUnavailableError(f"{underlying} {as_of} is not in the local cache")


def report(result: BacktestResult) -> None:
    print(result.metrics.render())
    starved = result.starved_tickers()
    if starved:
        print(
            f"\n*** DATA COVERAGE WARNING: {len(starved)} of {len(result.chain_days)} tickers had data for\n"
            f"    less than half the window: {', '.join(starved)}\n"
            f"    These numbers are NOT a result for the configured universe. Backfill them\n"
            f"    (scripts/fetch_data.py) or narrow --tickers to what is actually cached. ***"
        )
    coverage = result.coverage()
    if coverage:
        print("\nChain coverage: " + ", ".join(f"{t} {frac:.0%}" for t, frac in coverage.items()))
    counts = result.rejection_counts()
    if counts:
        print("\nWhy trades didn't happen (top reasons):")
        for reason, count in list(counts.items())[:10]:
            print(f"  {count:>6}  {reason}")
    print(f"\nEntries: {len(result.entries_of_kind('entry'))}   Exits: {len(result.entries_of_kind('exit'))}")
    exits: dict[str, int] = {}
    for position in result.closed_positions:
        exits[position.close_reason or "?"] = exits.get(position.close_reason or "?", 0) + 1
    if exits:
        print("Exit reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(exits.items())))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=6, help="trailing months to backtest")
    parser.add_argument("--tickers", nargs="*", default=None, help="override universe.yaml")
    parser.add_argument("--source", choices=["cache", "synthetic"], default="cache")
    parser.add_argument("--seed", type=int, default=1, help="synthetic market seed (ignored for --source cache)")
    parser.add_argument("--slippage", choices=[*SLIPPAGE_MODELS, "all"], default="all")
    parser.add_argument("--equity", type=Decimal, default=Decimal("25000"), help="starting equity")
    parser.add_argument(
        "--warmup-days",
        type=int,
        default=365,
        help="calendar days of pre-start data used to prime IV Rank and the trend average",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    universe = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)
    risk = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    strategies_config = load_yaml_config(CONFIG_DIR / "strategies.yaml", StrategiesConfig)
    tickers = args.tickers or universe.tickers

    end = date.today()
    start = end - timedelta(days=int(args.months * 30.44))
    provider = build_provider(args.source, tickers, start - timedelta(days=args.warmup_days), end, args.seed)

    if args.source == "synthetic":
        print("!! SYNTHETIC MARKET: engine smoke test only. These numbers are not evidence of edge. !!\n")

    chosen = list(SLIPPAGE_MODELS) if args.slippage == "all" else [args.slippage]
    results = []
    for name in chosen:
        engine = BacktestEngine(
            provider=provider,
            strategies=build_enabled_strategies(strategies_config),
            risk=risk,
            strategies_config=strategies_config,
            universe=universe,
            slippage=SLIPPAGE_MODELS[name](),
            config=BacktestConfig(
                start=start,
                end=end,
                tickers=tickers,
                starting_equity=args.equity,
                warmup_days=args.warmup_days,
            ),
        )
        result = engine.run()
        results.append(result)
        report(result)
        print()

    pessimistic = next((r for r in results if r.slippage_label == "pessimistic"), None)
    if pessimistic is not None:
        verdict = "PASSES" if pessimistic.metrics.is_profitable else "FAILS"
        print(f"Gate: strategy {verdict} the pessimistic-fill test.")
        return 0 if pessimistic.metrics.is_profitable else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
