"""Tests for the config loader and the schemas that validate the shipped YAML files."""
from decimal import Decimal
from pathlib import Path

import pytest

from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import RiskConfig, StrategiesConfig, UniverseConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "optionsbot" / "config"


def test_load_shipped_risk_config():
    cfg = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    assert cfg.max_risk_per_trade_pct == Decimal("0.01")
    assert cfg.max_positions_per_underlying == 1
    assert "us_broad_market" in cfg.correlated_buckets
    assert "SPY" in cfg.correlated_buckets["us_broad_market"]


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
