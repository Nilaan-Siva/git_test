# optionsbot

An autonomous options trading bot, built in stages: 6-month historical backtest with realistic
slippage -> IBKR **paper** trading (unattended, monitored) -> a documented profitability gate
before any real capital is connected.

Full architecture, phased timeline, risk rules, and the go-live gate are in the project plan
(`/root/.claude/plans/root-claude-uploads-f0d86127-9359-5dc3-warm-clock.md` in this session, or
ask Claude to summarize it).

## Status

**Phase 0 (foundations) complete.** Core domain models, market calendar, and validated config
are in place and tested. Phases 1-3 (data layer, risk manager, strategies + backtest engine) are
next; Phase 5 (IBKR paper execution) requires no real money -- it connects to IBKR's free paper
trading account only.

## Layout

```
optionsbot/
├── config/     # risk.yaml, strategies.yaml, universe.yaml (committed) + settings.py (.env)
├── core/       # domain models (OptionContract, Spread, Position, ...) and the market calendar
├── ops/        # structured logging; health/scheduler land in Phase 5
tests/          # pytest
```

Modules not yet present (`data/`, `strategy/`, `risk/`, `execution/`, `backtest/`, `portfolio/`,
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
