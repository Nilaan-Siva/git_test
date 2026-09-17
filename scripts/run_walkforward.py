#!/usr/bin/env python3
"""CLI: walk-forward validation -- tune on old data, judge on data never seen.

This is the gate that decides whether the strategy is real. A backtest tuned and scored on the
same history proves nothing; walk-forward chooses parameters using only each fold's earlier
window and then scores them on the later one.

Every fold also runs the untouched defaults on the same unseen window, so the report can answer
the question that actually matters: did tuning beat *not* tuning? If it didn't, the parameter
search is fitting noise and should be dropped.

    python scripts/run_walkforward.py --months 18
    python scripts/run_walkforward.py --months 18 --in-sample-days 240 --out-of-sample-days 60

Requires a populated cache -- run scripts/fetch_data.py first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal

from optionsbot.backtest.engine import BacktestConfig, BacktestEngine
from optionsbot.backtest.metrics import BacktestMetrics
from optionsbot.backtest.slippage import SlippageModel
from optionsbot.backtest.walkforward import DEFAULT_GRID, make_folds, walk_forward
from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import RiskConfig, StrategiesConfig, StrategyParams, UniverseConfig
from optionsbot.config.settings import CONFIG_DIR, get_settings
from optionsbot.data.cache import CachingChainProvider, ParquetChainCache
from optionsbot.data.providers.base import ChainProvider, DataUnavailableError
from optionsbot.ops.logging import configure_logging
from optionsbot.strategy.registry import build_enabled_strategies

SLIPPAGE = {
    "optimistic": SlippageModel.optimistic,
    "realistic": SlippageModel.realistic,
    "pessimistic": SlippageModel.pessimistic,
}


class _CacheOnly(ChainProvider):
    """Never touches the network: a walk-forward run makes dozens of backtest passes, and a
    cache miss must fail loudly rather than quietly spending hours of rate limit."""

    def get_chain(self, underlying: str, as_of: date):
        raise DataUnavailableError(f"{underlying} {as_of} not cached -- run scripts/fetch_data.py")

    def get_underlying_price(self, underlying: str, as_of: date) -> Decimal:
        raise DataUnavailableError(f"{underlying} {as_of} not cached")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=18, help="total history to walk across")
    parser.add_argument("--in-sample-days", type=int, default=180, help="tuning window length")
    parser.add_argument("--out-of-sample-days", type=int, default=90, help="test window length")
    parser.add_argument("--anchored", action="store_true", help="grow the tuning window instead of rolling it")
    parser.add_argument("--min-trades", type=int, default=10, help="trades needed for a fold to count")
    parser.add_argument("--slippage", choices=list(SLIPPAGE), default="realistic")
    parser.add_argument("--equity", type=Decimal, default=Decimal("25000"))
    parser.add_argument("--warmup-days", type=int, default=365)
    parser.add_argument("--tickers", nargs="*", default=None)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    risk = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    strategies = load_yaml_config(CONFIG_DIR / "strategies.yaml", StrategiesConfig)
    universe = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)
    tickers = args.tickers or universe.tickers

    provider = CachingChainProvider(_CacheOnly(), ParquetChainCache(settings.cache_dir))
    slippage_factory = SLIPPAGE[args.slippage]

    def runner(params: StrategyParams, start: date, end: date) -> BacktestMetrics:
        config = strategies.model_copy(update={"put_credit_spread": params})
        engine = BacktestEngine(
            provider=provider,
            strategies=build_enabled_strategies(config),
            risk=risk,
            strategies_config=config,
            universe=universe,
            slippage=slippage_factory(),
            config=BacktestConfig(
                start=start,
                end=end,
                tickers=tickers,
                starting_equity=args.equity,
                warmup_days=args.warmup_days,
            ),
        )
        return engine.run().metrics

    end = date.today()
    start = end - timedelta(days=int(args.months * 30.44))
    folds = make_folds(
        start,
        end,
        in_sample_days=args.in_sample_days,
        out_of_sample_days=args.out_of_sample_days,
        anchored=args.anchored,
    )
    if not folds:
        print(
            f"No folds fit in {args.months} months with a {args.in_sample_days}-day tuning window "
            f"and {args.out_of_sample_days}-day test window. Shorten the windows or fetch more history.",
            file=sys.stderr,
        )
        return 1

    print(f"Walking {len(folds)} fold(s) over {start}..{end}, {args.slippage} fills.")
    print("Each fold searches the parameter grid in-sample, then scores the winner -- and the")
    print("untouched defaults -- on the unseen window. This takes a few minutes.\n")

    result = walk_forward(
        runner,
        folds=folds,
        base_params=strategies.put_credit_spread,
        grid=DEFAULT_GRID,
        min_trades=args.min_trades,
    )
    print(result.render())

    code, _ = result.verdict()
    return 0 if code == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
