# optionsbot

An autonomous options trading bot, built in stages: 6-month historical backtest with realistic
slippage -> IBKR **paper** trading (unattended, monitored) -> a documented profitability gate
before any real capital is connected.

Full architecture, phased timeline, risk rules, and the go-live gate are in the project plan
(`/root/.claude/plans/root-claude-uploads-f0d86127-9359-5dc3-warm-clock.md` in this session, or
ask Claude to summarize it).

## Status

**Phases 0-1 complete** (foundations; data layer). Core domain models, market calendar,
validated config, Black-Scholes pricing/greeks, the Polygon historical provider, a Parquet
cache, and technical indicators are all in place and tested (83 tests).

**Blocked on you:** the actual 6-month backfill (`scripts/fetch_data.py`) needs a Polygon.io
free-tier API key in `.env` (`OPTIONSBOT_POLYGON_API_KEY`) -- it hasn't been run yet, and the
Polygon response-parsing code, while unit-tested against fixture JSON, hasn't been smoke-tested
against a live response. Run `python scripts/fetch_data.py --months 1 --tickers SPY` first as a
smoke test once you add the key.

Phase 2 (risk manager) and Phase 3 (strategies + backtest engine) are next; Phase 5 (IBKR paper
execution) requires no real money -- it connects to IBKR's free paper trading account only.

## Layout

```
optionsbot/
├── config/     # risk.yaml, strategies.yaml, universe.yaml (committed) + settings.py (.env)
├── core/       # domain models (OptionContract, Spread, Position, Chain, ...) + market calendar
├── data/       # ChainProvider (Polygon), Parquet cache, Black-Scholes pricing, indicators
├── ops/        # structured logging; health/scheduler land in Phase 5
scripts/        # fetch_data.py (backfill); run_backtest.py etc. land in later phases
tests/          # pytest
```

Modules not yet present (`strategy/`, `risk/`, `execution/`, `backtest/`, `portfolio/`,
`learning/`, `reporting/`) are scaffolded in the plan and land in later phases.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -v
```

`optionsbot/config/*.yaml` is human-edited trading configuration and is committed. Secrets and
per-machine settings (Polygon API key, IBKR host/port) go in a gitignored `.env` -- copy
`.env.example` to start.

### A note on `core/models.py`'s price sign convention

Every price field in the codebase is a signed cash-flow-to-trader amount (positive = you pay,
negative = you receive), matching IBKR's own combo-order sign convention. This is documented at
the top of `core/models.py` and is the single most bug-prone convention in the system -- read it
before touching any P&L code.
