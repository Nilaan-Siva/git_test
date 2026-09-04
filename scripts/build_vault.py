#!/usr/bin/env python3
"""CLI: build the Obsidian vault that explains the bot in plain language.

Safe to run as often as you like. Every note is regenerated from the bot's live config, except
the Learning Log, which only ever appends — so the history of what changed and what it did stays
intact.

    python scripts/build_vault.py                      # rebuild from current config
    python scripts/build_vault.py --results run.json   # include the latest backtest numbers

Then in Obsidian: Open folder as vault -> pick the `vault/` directory -> start at "Start Here".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from optionsbot.config.loader import load_yaml_config
from optionsbot.config.schema import RiskConfig, StrategiesConfig, UniverseConfig
from optionsbot.config.settings import CONFIG_DIR, PROJECT_ROOT
from optionsbot.reporting.vault import build_vault


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "vault", help="vault directory")
    parser.add_argument("--results", type=Path, default=None, help="backtest results JSON to include")
    args = parser.parse_args()

    risk = load_yaml_config(CONFIG_DIR / "risk.yaml", RiskConfig)
    strategies = load_yaml_config(CONFIG_DIR / "strategies.yaml", StrategiesConfig)
    universe = load_yaml_config(CONFIG_DIR / "universe.yaml", UniverseConfig)

    run = None
    if args.results:
        if not args.results.exists():
            print(f"results file not found: {args.results}", file=sys.stderr)
            return 1
        run = json.loads(args.results.read_text())

    n = build_vault(args.out, risk=risk, strategies=strategies, universe=universe, run=run)
    print(f"wrote {n} notes to {args.out}")
    print(f"\nIn Obsidian: 'Open folder as vault' -> {args.out} -> open 'Start Here'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
