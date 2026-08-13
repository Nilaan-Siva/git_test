"""Strategy lookup: name -> class, and building the enabled set from config.

Kept separate from the strategies themselves so nothing has to import every strategy module to
find one, and so Phase 7's scorecard has a single place to consult when it wants to disable a
strategy that has stopped working.
"""
from __future__ import annotations

from typing import Mapping

from optionsbot.config.schema import StrategiesConfig, StrategyParams
from optionsbot.core.enums import StrategyName
from optionsbot.strategy.base import Strategy
from optionsbot.strategy.iron_condor import IronCondor
from optionsbot.strategy.put_credit_spread import PutCreditSpread

STRATEGY_CLASSES: Mapping[StrategyName, type[Strategy]] = {
    StrategyName.PUT_CREDIT_SPREAD: PutCreditSpread,
    StrategyName.IRON_CONDOR: IronCondor,
}


def params_for(config: StrategiesConfig, name: StrategyName) -> StrategyParams:
    """The configured parameters for one strategy.

    StrategiesConfig names its fields after the enum values, so this stays correct as long as
    that holds -- and raises loudly rather than silently returning defaults if it stops holding.
    """
    try:
        return getattr(config, name.value)
    except AttributeError as exc:
        raise KeyError(f"StrategiesConfig has no parameters for strategy {name.value!r}") from exc


def build_strategy(name: StrategyName, config: StrategiesConfig) -> Strategy:
    """Instantiate one strategy with its configured parameters, enabled or not."""
    try:
        cls = STRATEGY_CLASSES[name]
    except KeyError as exc:
        raise KeyError(f"no strategy implementation registered for {name.value!r}") from exc
    return cls(params_for(config, name))


def build_enabled_strategies(config: StrategiesConfig) -> list[Strategy]:
    """Every implemented strategy that config has switched on.

    Strategies declared in strategies.yaml but not yet implemented (the wheel, for now) are
    skipped rather than raising: an unimplemented strategy left enabled in config should not
    stop the whole bot from starting.
    """
    strategies = []
    for name in STRATEGY_CLASSES:
        strategy = build_strategy(name, config)
        if strategy.enabled:
            strategies.append(strategy)
    return strategies
