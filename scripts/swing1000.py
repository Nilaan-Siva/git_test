"""The $1,000 swing experiment: 20-day breakout, checked once a day near the close.

Chosen over three alternatives by measurement, not preference (scripts/backtest_swing.py,
3 years of daily bars, 5 bps slippage per side, close-only fills). Numbers below are the
CORRECTED ones, after a split-adjustment bug was found: Alpaca's default bars are RAW, so
NVDA's 10:1 split read as a 90% crash and poisoned every window. Adjustment.ALL fixed it and
moved the headline from a wrong +82.1% to:

    breakout  +69.6%  24 trades, 50% win, 17.5% maxDD, every year positive
    SPY B&H   +75.0%  the do-nothing benchmark -- breakout roughly MATCHES it, does not beat it
    momo      +59.7%  rsi2 -16.8%  fvg -22.8%   (tested and rejected, not skipped)

Robustness checks that had to pass before this file existed: doubling slippage to 10 bps cost
only a few points; picks spread across 10 tickers, so it is not one lucky name. The known
weaknesses, on the record: 24 trades is a small sample, the window was mostly a bull tape, and
the edge concentrates in single stocks -- so a bear market will hurt, and the 10-day-low exit
is the only thing limiting how much.

SIZING IS FLAT 98% ON PURPOSE, and that was tested, not assumed. Volatility-targeted sizing --
the most-replicated "improvement" in the factor literature -- was backtested against this exact
config in scripts/backtest_swing_volsizing.py and LOST on every metric at every parameter, as
did turtle-style stop-distance risk sizing. Return, maxDD, Sharpe and return/maxDD were all
best at flat 98%, and every config degraded monotonically the further it moved from flat. See
that file's docstring for why the published result does not transfer to a one-position bot.

Rules, executed at ~15:35 ET on the day's running price as the close proxy:
  * flat: buy the strongest-momentum symbol whose price clears its PRIOR 20-day high,
    98% of ledger, fractional shares. No signal, no trade -- most days are holds.
  * holding: sell only when price closes under the PRIOR 10-day low. No profit target;
    the whole edge is letting winners run for weeks.
  * one position at a time; hard-capped ledger, Alpaca's fake $100k does not exist. The cap is
    state["contributed"], NOT the $1,000 in this file's name -- see the deposit note below.

CORE SIZE, grown Aug 25 2026 from $1,000 to $4,000 on the user's explicit approval. The reason
is the barbell ratio, not a change of confidence in this strategy: the moonshot's $100 convex
sleeve was 9% of the book against a 2-3% benchmark, and the per-bet fraction there cannot be
lowered (one option contract is indivisible -- 3% of a $50 ledger is $1.50 against a $47
cheapest ask). Growing the core was the only viable lever. At $4,000 the sleeve is 2.4%, inside
the benchmark. Deposits go through `--deposit AMOUNT`, never by hand-editing state.json, and
raise ledger_cash and contributed together so a deposit can never be booked as a return. All
performance is measured against contributed capital. The kill switch is 10% of contributed
(currently $400), so growing the core does not silently loosen the stop-out.
Every run appends a journal line whether it traded or not; the report comes from the journal.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "data" / "swing1000"
STATE_FILE = STATE_DIR / "state.json"
JOURNAL_FILE = STATE_DIR / "journal.jsonl"

# The filename says 1000 because that is what the experiment STARTED at on Aug 24 2026. The
# core was grown to $4,000 on Aug 25 (see the deposit note below); the name is kept because the
# swing1000-daily Routine invokes this file by path and renaming it would silently break the
# schedule. Treat "1000" as a historical label, not a live number -- the live number is
# state["contributed"].
STARTING_LEDGER = Decimal("1000.00")
# Deposits are tracked so returns stay honest. Once capital is added, equity-vs-$1,000 is a
# meaningless number: it would book a deposit as profit. Every performance figure is measured
# against state["contributed"] -- total money put in -- and every deposit is journalled.
KILL_SWITCH_FRACTION = Decimal("0.10")  # was a flat $100 on a $1,000 ledger; now scales
CASH_BUFFER = Decimal("0.98")
UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLE", "XLF", "GLD", "TLT",
            "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]
ENTRY_WINDOW_DAYS = 20   # breakout above the prior N-day high
EXIT_WINDOW_DAYS = 10    # exit below the prior N-day low
# KILL_SWITCH is derived from contributed capital, not hardcoded, so growing the core does not
# silently loosen the stop-out: 10% of money-in, whatever money-in currently is.


def load_state() -> dict:
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text())
        # back-fill for state files written before deposits existed
        st.setdefault("contributed", str(STARTING_LEDGER))
        return st
    return {"started": date.today().isoformat(), "ledger_cash": str(STARTING_LEDGER),
            "contributed": str(STARTING_LEDGER), "position": None, "day": 0}


def kill_switch(state: dict) -> Decimal:
    return (Decimal(state["contributed"]) * KILL_SWITCH_FRACTION).quantize(Decimal("0.01"))


def deposit(amount: Decimal) -> int:
    """Add capital to the core, auditably. Never edit state.json by hand for this.

    A deposit is not a trade and not a return: it raises both ledger_cash and contributed by the
    same amount, so equity/contributed is unchanged at the moment of the deposit and the strategy
    gets no credit for money that was simply handed to it. Safe to run while a position is open
    -- the position is untouched and the new cash is deployed on the next entry."""
    if amount <= 0:
        print("deposit must be positive"); return 1
    state = load_state()
    before_cash = Decimal(state["ledger_cash"])
    before_contrib = Decimal(state["contributed"])
    state["ledger_cash"] = str(before_cash + amount)
    state["contributed"] = str(before_contrib + amount)
    save_state(state)
    journal({"day": state["day"], "actions": [f"deposit: +${amount}"], "event": "deposit",
             "amount": str(amount), "ledger_cash": state["ledger_cash"],
             "contributed_before": str(before_contrib), "contributed": state["contributed"],
             "position": state.get("position"),
             "note": "core grown to bring the moonshot sleeve within the 2-3% barbell benchmark; "
                     "returns are measured against contributed capital, not against equity"})
    print(f"deposited ${amount}: cash {before_cash} -> {state['ledger_cash']}, "
          f"contributed {before_contrib} -> {state['contributed']}")
    print(f"kill switch now ${kill_switch(state)} (10% of contributed)")
    return 0


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def journal(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with JOURNAL_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def wait_for_fill(client, order_id: str, timeout_s: int = 90):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        order = client.get_order_by_id(order_id)
        if order.status in ("filled",):
            return order
        if order.status in ("canceled", "expired", "rejected"):
            raise RuntimeError(f"order {order_id} ended {order.status}")
        time.sleep(2)
    raise RuntimeError(f"order {order_id} not filled after {timeout_s}s")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if "--deposit" in sys.argv:
        return deposit(Decimal(sys.argv[sys.argv.index("--deposit") + 1]))
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    trading = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    data = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

    state = load_state()
    today = date.today().isoformat()
    if state.get("last_run_date") != today:
        state["day"] += 1
    state["last_run_date"] = today
    ledger = Decimal(state["ledger_cash"])
    position = state["position"]

    clock = trading.get_clock()
    if not clock.is_open:
        journal({"day": state["day"], "action": "market_closed"})
        save_state(state)
        print("market closed -- journalled only")
        return 0

    bars = data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=UNIVERSE, timeframe=TimeFrame.Day,
        # Adjustment.ALL is load-bearing: default bars are RAW, and a split inside the
        # 20-day window (NVDA did 10:1 in June 2024) would corrupt every high/low and read
        # as a 90% crash. Found the hard way in the first bake-off run.
        start=datetime.now(timezone.utc) - timedelta(days=45), feed=DataFeed.IEX,
        adjustment=Adjustment.ALL))

    sig = {}
    for sym in UNIVERSE:
        series = bars.data.get(sym, [])
        if len(series) < ENTRY_WINDOW_DAYS + 2:
            continue
        # today's (possibly partial) bar is the price proxy; windows use PRIOR completed bars
        last = series[-1]
        prior = series[:-1] if last.timestamp.date() == date.today() else series
        px = float(last.close)
        sig[sym] = {
            "px": px,
            "hi20": max(b.high for b in prior[-ENTRY_WINDOW_DAYS:]),
            "lo10": min(b.low for b in prior[-EXIT_WINDOW_DAYS:]),
            "mom20": px / float(prior[-ENTRY_WINDOW_DAYS].close) - 1,
        }

    actions = []

    # ---- exit check ----
    if position:
        s = sig.get(position["symbol"])
        if s and s["px"] < s["lo10"]:
            order = trading.submit_order(MarketOrderRequest(
                symbol=position["symbol"], qty=position["qty"],
                side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
            filled = wait_for_fill(trading, order.id)
            proceeds = (Decimal(str(filled.filled_avg_price)) * Decimal(str(filled.filled_qty))
                        ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            ledger += proceeds
            pnl = proceeds - Decimal(position["entry_cost"])
            actions.append(f"exit_10d_low: sold {position['symbol']} @ {filled.filled_avg_price} (P&L {pnl:+})")
            position = None
        elif s:
            actions.append(f"hold {position['symbol']}: {s['px']:.2f} vs 10d-low {s['lo10']:.2f}")

    # ---- entry check ----
    if position is None and not any(a.startswith("exit") for a in actions):
        cands = sorted(((s["mom20"], sym) for sym, s in sig.items() if s["px"] > s["hi20"]),
                       reverse=True)
        if cands:
            sym = cands[0][1]
            notional = (ledger * CASH_BUFFER).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if notional >= Decimal("1"):
                order = trading.submit_order(MarketOrderRequest(
                    symbol=sym, notional=float(notional),
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                filled = wait_for_fill(trading, order.id)
                cost = (Decimal(str(filled.filled_avg_price)) * Decimal(str(filled.filled_qty))
                        ).quantize(Decimal("0.01"))
                ledger -= cost
                position = {"symbol": sym, "qty": str(filled.filled_qty),
                            "entry_price": str(filled.filled_avg_price),
                            "entry_cost": str(cost), "entry_date": today}
                actions.append(f"breakout_buy: {filled.filled_qty} {sym} @ {filled.filled_avg_price}")
        elif position is None and not actions:
            actions.append("flat: no symbol above its 20-day high")

    equity = ledger + (Decimal(str(sig[position["symbol"]]["px"])) * Decimal(position["qty"])
                       if position and position["symbol"] in sig else Decimal("0"))
    state["ledger_cash"] = str(ledger)
    state["position"] = position
    save_state(state)
    journal({"day": state["day"], "actions": actions, "ledger_cash": str(ledger),
             "position": position, "equity": str(equity.quantize(Decimal("0.01"))),
             "contributed": state["contributed"],
             "signals": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                             for kk, vv in v.items()} for k, v in sig.items()}})
    print(f"day {state['day']}: {'; '.join(actions)}")
    contributed = Decimal(state["contributed"])
    ks = kill_switch(state)
    print(f"ledger cash={ledger} equity~={equity.quantize(Decimal('0.01'))} "
          f"contributed={contributed} return={((equity / contributed - 1) * 100):.2f}%")
    if equity and equity < ks:
        print(f"KILL SWITCH: equity below ${ks} (10% of ${contributed} contributed) "
              f"-- experiment functionally over, report due")
    return 0


if __name__ == "__main__":
    sys.exit(main())
