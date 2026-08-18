"""Moonshot mode: the $100 -> $1,000 attempt, by daily 0DTE option parlay.

This exists because the user asked for the one strategy with any mathematical path from $100
to $1,000 in a month, after being told the odds. This is that strategy, and the odds are ON
THE RECORD: under 1% chance of reaching $1,000; the most likely outcome is losing 50-90% of
the ledger over the month. This is a lottery machine with a journal, run on paper money as a
live demonstration -- it is NOT the validated strategy in this repo (see PR #1 for that one).

Mechanics, morning run (--mode open, ~9:57 ET):
  * Direction: SPY's move from today's open (first ~25 minutes of tape). Up -> call, down -> put.
  * Contract: today's expiration (or nearest), the strike CLOSEST to the money whose ask fits
    ~95% of the ledger. One contract, market order. If nothing near the money is affordable,
    the search walks further out-of-the-money -- worse odds, journalled as such -- and if even
    the cheapest lottery ticket costs more than the ledger, the day is skipped, not forced.
  * Max loss is the premium paid: a bad day loses ~95% of the ledger in one shot. That is the
    aggression the user chose, stated plainly.

Afternoon run (--mode close, ~15:47 ET): sell whatever is open at market, no exceptions --
0DTE held to the bell expires to dust, and "it might come back" is how parlays die.

The ledger/journal contract is identical to daily100.py: hard-capped ledger, append-only
journal committed to git, equity marked from actual fills. Day 30 reports whatever the journal
says, including a comparison against just holding SPY.
"""
from __future__ import annotations

import argparse
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

UNDERLYING = "SPY"
BUDGET_FRACTION = Decimal("0.95")
CONTRACT_MULTIPLIER = 100


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def journal(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with JOURNAL_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def wait_for_fill(client, order_id: str, timeout_s: int = 120):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        order = client.get_order_by_id(order_id)
        if order.status == "filled":
            return order
        if order.status in ("canceled", "expired", "rejected"):
            raise RuntimeError(f"order {order_id} ended {order.status}")
        time.sleep(2)
    raise RuntimeError(f"order {order_id} not filled after {timeout_s}s")


def clients():
    load_dotenv(REPO_ROOT / ".env")
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient

    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    return (
        TradingClient(key, secret, paper=True),
        StockHistoricalDataClient(key, secret),
        OptionHistoricalDataClient(key, secret),
    )


def mode_open() -> int:
    from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
    from alpaca.trading.enums import ContractType, AssetStatus, OrderSide, TimeInForce

    trading, stocks, options = clients()
    state = load_state()
    today = date.today()
    if state.get("last_run_date") != today.isoformat():
        state["day"] += 1
    state["last_run_date"] = today.isoformat()

    if not trading.get_clock().is_open:
        journal({"day": state["day"], "mode": "moonshot_open", "action": "market_closed"})
        save_state(state)
        print("market closed; no entry")
        return 0
    if state.get("position"):
        journal({"day": state["day"], "mode": "moonshot_open", "action": "skip_position_already_open"})
        save_state(state)
        print("position already open; skipping entry")
        return 0

    # Direction from today's tape so far: last trade vs today's official open.
    bars = stocks.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=UNDERLYING,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=4),
            feed=DataFeed.IEX,
        )
    ).data[UNDERLYING]
    today_bar = bars[-1]
    spot, day_open = float(today_bar.close), float(today_bar.open)
    direction = "call" if spot >= day_open else "put"

    # Today's expiration if it exists, else the nearest one this week.
    contracts_resp = trading.get_option_contracts(
        GetOptionContractsRequest(
            underlying_symbols=[UNDERLYING],
            status=AssetStatus.ACTIVE,
            type=ContractType.CALL if direction == "call" else ContractType.PUT,
            expiration_date_gte=today,
            expiration_date_lte=today + timedelta(days=4),
            strike_price_gte=str(round(spot * 0.97, 2)),
            strike_price_lte=str(round(spot * 1.03, 2)),
            limit=200,
        )
    )
    contracts = list(contracts_resp.option_contracts or [])
    if not contracts:
        journal({"day": state["day"], "mode": "moonshot_open", "action": "no_contracts_found"})
        save_state(state)
        print("no contracts found; skipping day")
        return 0
    nearest_exp = min(c.expiration_date for c in contracts)
    contracts = [c for c in contracts if c.expiration_date == nearest_exp]
    # Nearest-the-money first, then walk outward until one fits the budget.
    contracts.sort(key=lambda c: abs(float(c.strike_price) - spot))

    ledger = Decimal(state["ledger_cash"])
    budget = (ledger * BUDGET_FRACTION).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    quotes = options.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=[c.symbol for c in contracts])
    )
    chosen, ask = None, None
    for c in contracts:
        q = quotes.get(c.symbol)
        if q is None or q.ask_price in (None, 0):
            continue
        cost = Decimal(str(q.ask_price)) * CONTRACT_MULTIPLIER
        if cost <= budget:
            chosen, ask = c, Decimal(str(q.ask_price))
            break

    if chosen is None:
        journal(
            {
                "day": state["day"],
                "mode": "moonshot_open",
                "action": "skip_unaffordable",
                "detail": f"cheapest viable {direction} near {spot} exceeds budget {budget}",
            }
        )
        save_state(state)
        print(f"no affordable {direction}; skipping day (budget {budget})")
        return 0

    order = trading.submit_order(
        MarketOrderRequest(symbol=chosen.symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    )
    filled = wait_for_fill(trading, order.id)
    cost = (Decimal(str(filled.filled_avg_price)) * CONTRACT_MULTIPLIER).quantize(Decimal("0.01"))
    ledger -= cost
    state["ledger_cash"] = str(ledger)
    state["position"] = {
        "kind": "option",
        "symbol": chosen.symbol,
        "qty": "1",
        "entry_price": str(filled.filled_avg_price),
        "entry_cost": str(cost),
        "strike": str(chosen.strike_price),
        "spot_at_entry": spot,
        "direction": direction,
        "expiration": str(nearest_exp),
        "entry_date": today.isoformat(),
    }
    save_state(state)
    journal(
        {
            "day": state["day"],
            "mode": "moonshot_open",
            "action": f"bought 1x {chosen.symbol} ({direction}, strike {chosen.strike_price}, exp {nearest_exp}) @ {filled.filled_avg_price}",
            "spot": spot,
            "day_open": day_open,
            "cost": str(cost),
            "ledger_cash": str(ledger),
        }
    )
    print(f"day {state['day']}: bought {chosen.symbol} ({direction}) @ {filled.filled_avg_price} (${cost}); ledger cash {ledger}")
    return 0


def mode_close() -> int:
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    trading, _, _ = clients()
    state = load_state()
    position = state.get("position")
    if not position or position.get("kind") != "option":
        journal({"day": state["day"], "mode": "moonshot_close", "action": "nothing_to_close"})
        print("no option position to close")
        return 0

    ledger = Decimal(state["ledger_cash"])
    order = trading.submit_order(
        MarketOrderRequest(
            symbol=position["symbol"], qty=int(position["qty"]), side=OrderSide.SELL, time_in_force=TimeInForce.DAY
        )
    )
    filled = wait_for_fill(trading, order.id)
    proceeds = (Decimal(str(filled.filled_avg_price)) * CONTRACT_MULTIPLIER).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    pnl = proceeds - Decimal(position["entry_cost"])
    ledger += proceeds
    state["ledger_cash"] = str(ledger)
    state["position"] = None
    save_state(state)
    journal(
        {
            "day": state["day"],
            "mode": "moonshot_close",
            "action": f"sold {position['symbol']} @ {filled.filled_avg_price}",
            "proceeds": str(proceeds),
            "pnl": str(pnl),
            "ledger_cash": str(ledger),
        }
    )
    print(f"day {state['day']}: sold {position['symbol']} @ {filled.filled_avg_price} -> {proceeds} (P&L {pnl:+}); ledger {ledger}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["open", "close"], required=True)
    args = parser.parse_args()
    return mode_open() if args.mode == "open" else mode_close()


if __name__ == "__main__":
    sys.exit(main())
