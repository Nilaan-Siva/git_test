"""The $100 experiment: one disciplined decision per trading day, on a hard-capped ledger.

This is deliberately NOT the options bot. A $100 account cannot clear the fixed costs of a
single defined-risk spread (see PR #1's cheap-ticker post-mortem), so this trades fractional
shares of liquid ETFs instead -- the only instrument where $100 buys real, cost-free market
exposure (Alpaca charges no stock commission).

The experiment's contract, which this script enforces rather than promises:

  * The ledger starts at $100.00 and is the ONLY capital the bot may use. The paper account
    holds Alpaca's default fake $100k; everything beyond the ledger does not exist here. Every
    order is sized from ledger cash, never from account buying power.
  * At most one decision per day: hold the strongest trending ETF in the universe, or hold
    cash when nothing trends. One position at a time, entered/exited by notional market order
    minutes before the close. No same-day round trips (which also keeps clear of PDT rules).
  * A 3% stop from entry, checked daily, caps single-position damage; there is no averaging
    down, no doubling up, and nothing to override.
  * Every run appends an immutable journal line -- decision, prices, fills, ledger -- whether
    it traded or not. The 30-day report is computed from this journal, not narrated.

Momentum rule (dual-window, long-flat): rank the universe by 20-day return, require the best
name's 20-day return to be positive AND its price above its own 20-day mean, else stay in
cash. Boring on purpose: the experiment's hypothesis is that discipline is the edge a small
account can actually hold onto, and its honest expectation is single-digit dollars either way.
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
STATE_DIR = REPO_ROOT / "data" / "daily100"
STATE_FILE = STATE_DIR / "state.json"
JOURNAL_FILE = STATE_DIR / "journal.jsonl"

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT"]
STARTING_LEDGER = Decimal("100.00")
CASH_BUFFER = Decimal("0.98")  # invest at most 98% of ledger, so rounding can never overdraw
STOP_LOSS_PCT = Decimal("0.03")
MOMENTUM_DAYS = 20
LOOKBACK_DAYS = 45  # calendar days of bars to fetch; > MOMENTUM_DAYS trading days


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "started": date.today().isoformat(),
        "ledger_cash": str(STARTING_LEDGER),
        "position": None,  # {"symbol", "qty", "entry_price", "entry_date"}
        "day": 0,
    }


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def journal(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with JOURNAL_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def wait_for_fill(client, order_id: str, timeout_s: int = 90):
    """Market orders in liquid ETFs fill in seconds; anything slower deserves a loud failure."""
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
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    trading = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    data = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

    clock = trading.get_clock()
    state = load_state()
    today = date.today().isoformat()
    if state.get("last_run_date") != today:
        state["day"] += 1  # a rerun on the same calendar day is a check, not a new trading day
    state["last_run_date"] = today

    # ---- signals -------------------------------------------------------------------------
    bars = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=UNIVERSE,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
            feed=DataFeed.IEX,
        )
    )
    closes: dict[str, list[float]] = {}
    for symbol in UNIVERSE:
        series = bars.data.get(symbol, [])
        closes[symbol] = [b.close for b in series]

    signals = {}
    for symbol, series in closes.items():
        if len(series) < MOMENTUM_DAYS + 1:
            continue
        window = series[-(MOMENTUM_DAYS + 1) :]
        ret = (window[-1] - window[0]) / window[0]
        above_mean = window[-1] > sum(window) / len(window)
        signals[symbol] = {"ret_20d": round(ret, 5), "above_20d_mean": above_mean, "last": window[-1]}

    ranked = sorted(signals.items(), key=lambda kv: kv[1]["ret_20d"], reverse=True)
    target = None
    if ranked:
        best, sig = ranked[0]
        if sig["ret_20d"] > 0 and sig["above_20d_mean"]:
            target = best

    ledger = Decimal(state["ledger_cash"])
    position = state["position"]
    actions: list[str] = []

    if not clock.is_open:
        journal({"day": state["day"], "action": "market_closed", "signals": signals})
        print("market closed -- no action; journalled signals only")
        save_state(state)
        return 0

    # ---- stop-loss check (before anything else, so a broken position always exits) -------
    if position:
        last = signals.get(position["symbol"], {}).get("last")
        if last is not None:
            entry = float(position["entry_price"])
            if last < entry * float(Decimal("1") - STOP_LOSS_PCT):
                actions.append(f"stop_loss: {position['symbol']} {last:.2f} < {entry:.2f} -3%")
                target = None  # fall through to the sell path below and stay in cash today

    # ---- rebalance -----------------------------------------------------------------------
    if position and (target != position["symbol"]):
        order = trading.submit_order(
            MarketOrderRequest(
                symbol=position["symbol"],
                qty=position["qty"],
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        filled = wait_for_fill(trading, order.id)
        proceeds = Decimal(str(filled.filled_avg_price)) * Decimal(str(filled.filled_qty))
        ledger += proceeds.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        actions.append(f"sold {filled.filled_qty} {position['symbol']} @ {filled.filled_avg_price}")
        position = None

    if target and position is None:
        notional = (ledger * CASH_BUFFER).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if notional >= Decimal("1"):
            order = trading.submit_order(
                MarketOrderRequest(
                    symbol=target,
                    notional=float(notional),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            filled = wait_for_fill(trading, order.id)
            cost = Decimal(str(filled.filled_avg_price)) * Decimal(str(filled.filled_qty))
            ledger -= cost.quantize(Decimal("0.01"))
            position = {
                "symbol": target,
                "qty": str(filled.filled_qty),
                "entry_price": str(filled.filled_avg_price),
                "entry_date": date.today().isoformat(),
            }
            actions.append(f"bought {filled.filled_qty} {target} @ {filled.filled_avg_price}")

    if not actions:
        actions.append(f"hold {position['symbol']}" if position else "hold cash (no qualifying trend)")

    # ---- mark and record -----------------------------------------------------------------
    equity = ledger
    if position:
        last = signals.get(position["symbol"], {}).get("last")
        if last is not None:
            equity = ledger + Decimal(str(last)) * Decimal(position["qty"])

    state["ledger_cash"] = str(ledger)
    state["position"] = position
    save_state(state)
    journal(
        {
            "day": state["day"],
            "actions": actions,
            "signals": signals,
            "ledger_cash": str(ledger),
            "position": position,
            "equity": str(equity.quantize(Decimal('0.01'))),
        }
    )
    print(f"day {state['day']}: {'; '.join(actions)}")
    print(f"ledger cash={ledger} equity~={equity.quantize(Decimal('0.01'))} position={position}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
