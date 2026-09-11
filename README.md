# optionsbot

An autonomous options trading bot, built in stages: 6-month historical backtest with realistic
slippage -> IBKR **paper** trading (unattended, monitored) -> a documented profitability gate
before any real capital is connected.

Full architecture, phased timeline, risk rules, and the go-live gate are in the project plan
(`/root/.claude/plans/root-claude-uploads-f0d86127-9359-5dc3-warm-clock.md` in this session, or
ask Claude to summarize it).

## Status

**Phases 0-3 complete** (foundations; data layer; risk manager; strategies + backtest engine).
Domain models, market calendar, validated config, Black-Scholes pricing/greeks, the Polygon
provider, a Parquet cache, indicators, the risk manager, two strategies (put credit spread, iron
condor), the fill/slippage model, the backtest event loop, and the metrics report are all in
place. **334 tests, 100% branch coverage on `risk/`, 99% on `strategy/` and `backtest/`.**

**Blocked on you:** the actual 6-month backfill (`scripts/fetch_data.py`) needs a Polygon.io
free-tier API key in `.env` (`OPTIONSBOT_POLYGON_API_KEY`) -- it hasn't been run yet, and the
Polygon response-parsing code, while unit-tested against fixture JSON, hasn't been smoke-tested
against a live response. Run `python scripts/fetch_data.py --months 1 --tickers SPY` first as a
smoke test once you add the key.

Until then the engine is validated against `data/providers/synthetic.py`, a deterministic
generated market. That proves the machinery works; it says **nothing** about whether the
strategy makes money. See "Phase 3 findings" below -- one of them changes the project timeline.

Phase 4 (walk-forward validation) is next; Phase 5 (IBKR paper execution) requires no real money
-- it connects to IBKR's free paper trading account only.

## Phase 3 findings

Three things came out of building and running the engine that were not visible from the plan.

**1. The trade rate makes the go-live gate years away, not months.** This is the important one.
Measured on the synthetic market with the shipped config, at realistic fills:

| Universe | Trades / year | Time to 50 closed trades |
|---|---|---|
| SPY only | ~8 | ~6.5 years |
| SPY + QQQ | ~13 | ~3.9 years |
| SPY + QQQ + IWM + XSP | ~20 | ~2.5 years |

The plan's go-live gate needs **50 closed paper trades**, and assumed 2-4 trades/week. The real
constraint is structural, not conservative filtering: `max_positions_per_underlying: 1` plus a
30-50 DTE entry and a 21-DTE time stop means roughly a 19-day hold and therefore ~13 slots per
underlying per year, before any filter rejects anything. Widening the universe barely helps,
because SPY/XSP/QQQ/IWM are all one correlated bucket capped at 2 concurrent positions.

Nothing here is broken -- the limits are doing exactly what they were written to do. But the
timeline in the plan is not achievable with this configuration, and the options all involve a
decision that is yours, not the bot's:

  * **Ladder entries** -- raise `max_positions_per_underlying` to 2-3 at *different* expirations.
    Each trade still risks 1%; total exposure is still governed by the 6% heat cap. This is the
    standard fix and the one that changes risk posture least.
  * **Add genuinely uncorrelated underlyings** (TLT, GLD, XLE) so the bucket limit stops being
    the binding constraint. Costs nothing in risk terms; costs liquidity quality.
  * **Shorten DTE** to 21-35 with a 14-day time stop. Faster turnover, more gamma risk.
  * **Lower the go-live trade count.** Not recommended -- 50 is already the statistical floor.

**2. The IV Rank gate and a short-term trend gate cancel each other out.** Index IV rises when
price falls, so a 50-day trend filter rejects almost exactly the high-IV days the IVR filter
wants. With both at their originally-planned settings the strategy vetoed *every single day* of
a six-month sample and reported "0 trades" rather than "misconfigured". The trend average is now
200 days by default (`StrategyParams.trend_sma_period`), which expresses the setup the strategy
actually wants -- selling into a pullback *within* an uptrend. `tests/test_filters.py` pins this
down so it can't silently regress.

Related operational gotcha: `check_not_downtrend` passes when it has less history than its
period, so **a backtest with less than ~200 trading days of warmup has no trend filter at all**
and does not warn. `BacktestConfig.warmup_days` defaults to 365 calendar days for this reason.

**3. Modelled bid-ask width must not scale with option price.** The first fill model charged a
$4.50 SPY put a $0.22 spread; real SPY options are penny-wide regardless of price. On a 1-wide
spread that assumption exceeded the entire credit and made the strategy look broken for a reason
that was purely an artifact. Width is now a percentage clamped between absolute floor and cap
(`SlippageModel.min_leg_width` / `max_leg_width`), calibrated to liquid index options.

## Layout

```
optionsbot/
├── config/     # risk.yaml, strategies.yaml, universe.yaml (committed) + settings.py (.env)
├── core/       # domain models (OptionContract, Spread, Position, Chain, PortfolioState, ...)
│               # and the market calendar
├── data/       # ChainProvider (Polygon + synthetic), Parquet cache, Black-Scholes, indicators
├── risk/       # THE veto gate: sizing, portfolio limits, kill switches (risk/manager.approve)
├── strategy/   # put credit spread, iron condor; entry filters, exit rules, combo pricing
├── backtest/   # event loop, fill/slippage model, performance metrics
├── ops/        # structured logging; health/scheduler land in Phase 5
scripts/        # fetch_data.py (backfill), run_backtest.py
tests/          # pytest
```

Modules not yet present (`execution/`, `portfolio/`, `learning/`, `reporting/`) are scaffolded
in the plan and land in later phases.

## Running a backtest

```bash
# against the deterministic synthetic market -- engine check, NOT evidence of edge
.venv/bin/python scripts/run_backtest.py --source synthetic --months 6 --tickers SPY

# against cached Polygon data, once scripts/fetch_data.py has been run
.venv/bin/python scripts/run_backtest.py --months 6 --slippage all
```

Every run is scored three ways -- optimistic (perfect mid fills), realistic (the research
numbers), pessimistic (cross every spread in full). **A strategy is judged on the pessimistic
run.** If expectancy is only positive under optimistic fills, the edge is fill quality you will
not get, and the exit code is non-zero to make that hard to ignore.

The report also prints why trades *didn't* happen, aggregated by reason. A backtest that says
"12 trades" without saying 300 proposals were rejected for low IV Rank is hiding the part of the
strategy that does the work.

### A note on `risk/manager.py`

`approve()` is a pure, stateless function -- it has no memory between calls. If you evaluate two
`OrderIntent`s against the *same* `PortfolioState` snapshot before either has actually been
filled, both can be approved even if, combined, they'd exceed a limit neither call could see on
its own (this is deliberately characterized by a test, not hidden). The caller -- Phase 5's
execution router -- is responsible for refreshing `PortfolioState` between sequential approvals
within a trading cycle. Keeping the risk manager itself a pure function is what makes 100%
branch coverage on it tractable at all.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -v
```

`optionsbot/config/*.yaml` is human-edited trading configuration and is committed. Secrets and
per-machine settings (Polygon API key, IBKR host/port) go in a gitignored `.env` -- copy
`.env.example` to start.

### A note on `data/providers/synthetic.py`

It is a test instrument, not a data source. It generates a reproducible market from a seed so
the engine can be exercised end to end without burning Polygon's rate limit, and so strategy
regressions are catchable. Its most important parameter is `variance_risk_premium` -- the gap
between the volatility options are priced at and the volatility the underlying realizes. That
gap is the entire economic reason premium selling makes money, and here it is a dial. Set it to
zero and a correct engine reports a small commission-sized loss; set it high and a correct
engine finds the edge. Both are tests of arithmetic. Never read a P&L number off this provider
as though it said something about real markets.

### A note on `core/models.py`'s price sign convention

Every price field in the codebase is a signed cash-flow-to-trader amount (positive = you pay,
negative = you receive), matching IBKR's own combo-order sign convention. This is documented at
the top of `core/models.py` and is the single most bug-prone convention in the system -- read it
before touching any P&L code.
