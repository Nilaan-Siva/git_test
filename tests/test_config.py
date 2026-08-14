"""Tests for the config loader and the schemas that validate the shipped YAML files."""
from decimal import Decimal
from pathlib import Path

import pytest

from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import RiskConfig, StrategiesConfig, UniverseConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "optionsbot" / "config"


def test_load_shipped_risk_config():
    cfg = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    assert "us_broad_market" in cfg.correlated_buckets
    assert "SPY" in cfg.correlated_buckets["us_broad_market"]


def test_shipped_per_trade_risk_stays_within_the_five_percent_ceiling():
    """The account rule started at "never more than 1% per trade" and was deliberately raised to
    5%, in exchange for accepting the drawdown math that comes with it (a 5-loss streak
    compounds to ~22.6%, but the 15% kill switch fires around loss 4, before the streak
    finishes). It is still a ceiling, not a target, so the shipped value may be lower -- but it
    must never drift above 5% silently, whatever else is tuned."""
    cfg = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    assert 0 < cfg.max_risk_per_trade_pct <= Decimal("0.05")


def test_shipped_config_ladders_across_distinct_expirations():
    """Allowing several positions per underlying is only diversification if they sit on
    different expirations; otherwise it is one bet held twice."""
    cfg = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    if cfg.max_positions_per_underlying > 1:
        assert cfg.require_distinct_expirations is True


def test_every_tradable_ticker_belongs_to_a_correlated_bucket():
    """An unbucketed ticker silently escapes the correlation limit -- it can be held alongside
    every other name no matter how tightly it tracks them. With a twelve-name universe that is
    exactly how a portfolio becomes one leveraged S&P bet while every individual check passes.
    """
    risk = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    universe = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)
    bucketed = {t.upper() for members in risk.correlated_buckets.values() for t in members}
    unbucketed = {t.upper() for t in universe.tickers} - bucketed
    assert not unbucketed, f"tickers with no correlated bucket: {sorted(unbucketed)}"


def test_concurrent_position_budget_is_bounded_by_heat_not_position_counts():
    """Heat should be the binding constraint, with the position counts as secondary guards.
    If the count limits bind first, the 6% cap never actually governs anything and tuning it
    has no effect -- a confusing failure mode to debug later."""
    risk = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    universe = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)

    slots_by_counts = 0
    for members in risk.correlated_buckets.values():
        tradable = [t for t in members if t.upper() in {u.upper() for u in universe.tickers}]
        slots_by_counts += min(
            len(tradable) * risk.max_positions_per_underlying,
            risk.max_positions_per_correlated_bucket,
        )
    slots_by_heat = int(risk.max_portfolio_heat_pct / risk.max_risk_per_trade_pct)
    assert slots_by_counts >= slots_by_heat, (
        f"position counts allow only {slots_by_counts} concurrent positions but heat allows "
        f"{slots_by_heat}; the heat cap is dead config"
    )


def test_load_shipped_strategies_config():
    cfg = load_yaml_config(CONFIG_DIR / "strategies.yaml", StrategiesConfig)
    assert cfg.put_credit_spread.enabled is True
    assert cfg.iron_condor.enabled is False
    assert cfg.wheel.enabled is False
    assert cfg.put_credit_spread.target_dte_min <= cfg.put_credit_spread.target_dte_max


def test_load_shipped_universe_config():
    cfg = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)
    assert "SPY" in cfg.tickers
    assert cfg.min_option_open_interest > 0


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_yaml_config(tmp_path / "does_not_exist.yaml", RiskConfig)


def test_invalid_config_raises_value_error_with_path(tmp_path):
    bad = tmp_path / "bad_risk.yaml"
    bad.write_text("max_risk_per_trade_pct: not_a_number\n")
    with pytest.raises(ValueError, match="bad_risk.yaml"):
        load_yaml_config(bad, RiskConfig)


def test_empty_yaml_file_falls_back_to_defaults(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    cfg = load_yaml_config(empty, RiskConfig)
    assert cfg.max_risk_per_trade_pct == Decimal("0.01")
