"""Moonshot mode: the $100 -> $1,000 attempt, by daily 0DTE option parlay.

This exists because the user asked for the one strategy with any mathematical path from $100
to $1,000 in a month, after being told the odds. This is that strategy, and the odds are ON
THE RECORD: under 1% chance of reaching $1,000; the most likely outcome is losing 50-90% of
the ledger over the month. This is a lottery machine with a journal, run on paper money as a
live demonstration -- it is NOT the validated strategy in this repo (see PR #1 for that one).

Mechanics, morning run (--mode open, ~10:03 ET), upgraded to the Opening Range Breakout after
a research sweep found it the one 0DTE entry with a documented backtested edge (options.cafe,
299 trades: 42.5% win rate, +100% target vs -50% stop, +62.78% total; quantish.io's refined
SPX variant reports a 2.2 Sharpe):
  * Opening range: SPY's high/low over the first 30 minutes (9:30-10:00 ET).
  * Enter ONLY on a breakout -- price above the range high buys a call, below the low buys a
    put. Price still inside the range means a chop day: skip it, journal it. Chop days are
    where 0DTE longs bleed to death on theta.
  * Mon/Wed/Fri only -- the days that carried the edge in the backtest. Tue/Thu are skipped.
  * Contract: nearest expiration, the strike CLOSEST to the money whose ask fits ~95% of the
    ledger, walking out-of-the-money only as far as the budget forces; if even the cheapest
    ticket is unaffordable, the day is skipped, not forced.
  * Immediately after the fill, a +100% take-profit limit sell (2x entry) goes in for the day:
    a double banks itself intraday without waiting for the afternoon run.
  * Max loss is the premium paid: a bad day still loses ~95% of the ledger in one shot. That
    is the aggression the user chose, stated plainly. (The backtest's -50% intraday stop is
    NOT implemented -- Alpaca options orders here support market/limit only, and there is no
    streaming process to watch the tape -- so this implementation is strictly riskier per
    trade than the published version. Journalled as a known deviation.)

Afternoon run (--mode close, ~15:47 ET): if the take-profit already filled, book it; else
cancel the resting limit and sell at market, no exceptions -- 0DTE held to the bell expires to
dust, and "it might come back" is how parlays die.

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

# The index-level "fundamentals": the macro-event calendar, verified through the experiment
# window (ends ~Sep 17 2026). Pre-open releases (jobs, CPI) land at 8:30 ET so the opening
# range absorbs them -- often the best trend days, journalled but traded. The FOMC decision
# lands MID-SESSION at 14:00 ET, hours after our entry, and routinely reverses morning
# breakouts violently: that day is skipped outright.
EVENT_CALENDAR = {
    "2026-09-04": ("note", "jobs report 8:30 ET pre-open (Aug employment situation)"),
    "2026-09-11": ("note", "CPI release 8:30 ET pre-open, last print before the Sep FOMC"),
    "2026-09-16": ("skip", "FOMC rate decision 14:00 ET mid-session -- whipsaw risk, day skipped"),
}


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
    from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import ContractType, AssetStatus, OrderSide, TimeInForce

    trading, stocks, options = clients()
    state = load_state()
    today = date.today()
    if state.get("last_run_date") != today.isoformat():
        state["day"] += 1
    state["last_run_date"] = today.isoformat()

    if today.weekday() not in (0, 2, 4):  # Mon/Wed/Fri carried the edge in the ORB backtest
        journal({"day": state["day"], "mode": "moonshot_open", "action": "skip_day_of_week"})
        save_state(state)
        print("Tue/Thu skipped by the ORB day-of-week filter")
        return 0
    event = EVENT_CALENDAR.get(today.isoformat())
    if event and event[0] == "skip":
        journal({"day": state["day"], "mode": "moonshot_open", "action": "skip_macro_event", "detail": event[1]})
        save_state(state)
        print(f"macro-event skip: {event[1]}")
        return 0
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

    # Volatility regime gate -- the third leg of the SSRN three-filter stack (M/W/F + macro
    # exclusion + VIX 15-25) that lifted ORB win rate from 46.8% to 65.4%. No VIX feed here,
    # so 14-day ATR as a percent of price stands in: roughly, VIX 15 ~ 0.6% daily range and
    # VIX 25 ~ 1.6%. Outside the band the edge decays -- dead tape fizzles, stressed tape is
    # noise -- so those days are skipped, not sized down.
    daily = stocks.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=UNDERLYING,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=35),
            feed=DataFeed.IEX,
        )
    ).data[UNDERLYING]
    trs = [
        max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
        for prev, b in zip(daily, daily[1:])
    ]
    atr_pct = (sum(trs[-14:]) / len(trs[-14:])) / float(daily[-1].close) if len(trs) >= 14 else None
    if atr_pct is not None and not (0.005 <= atr_pct <= 0.018):
        journal(
            {
                "day": state["day"],
                "mode": "moonshot_open",
                "action": "skip_vol_regime",
                "atr_pct": round(atr_pct, 5),
                "detail": "14d ATR% outside the ~VIX-15-25 band where the ORB edge is documented",
            }
        )
        save_state(state)
        print(f"vol regime out of band (ATR {atr_pct:.2%}); skipped")
        return 0

    # Opening range: SPY minute bars for the first 30 minutes of today's session.
    session_start = datetime.now(timezone.utc).replace(hour=13, minute=30, second=0, microsecond=0)
    minute_bars = stocks.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=UNDERLYING,
            timeframe=TimeFrame.Minute,
            start=session_start,
            feed=DataFeed.IEX,
        )
    ).data.get(UNDERLYING, [])
    range_bars = [b for b in minute_bars if b.timestamp < session_start + timedelta(minutes=30)]
    if len(range_bars) < 10 or len(minute_bars) <= len(range_bars):
        journal({"day": state["day"], "mode": "moonshot_open", "action": "skip_insufficient_bars"})
        save_state(state)
        print("not enough minute bars to define the opening range yet")
        return 0
    or_high = max(b.high for b in range_bars)
    or_low = min(b.low for b in range_bars)
    spot = float(minute_bars[-1].close)

    # Session VWAP -- the standard technical confirmation for ORB entries. A breakout the
    # volume-weighted tape disagrees with is far more likely to be a false break.
    vol_total = sum(b.volume for b in minute_bars) or 1
    vwap = sum(((b.high + b.low + b.close) / 3) * b.volume for b in minute_bars) / vol_total

    if spot > or_high and spot > vwap:
        direction = "call"
    elif spot < or_low and spot < vwap:
        direction = "put"
    elif spot > or_high or spot < or_low:
        journal(
            {
                "day": state["day"],
                "mode": "moonshot_open",
                "action": "skip_breakout_against_vwap",
                "or_high": or_high,
                "or_low": or_low,
                "vwap": round(vwap, 4),
                "spot": spot,
            }
        )
        save_state(state)
        print(f"breakout unconfirmed by VWAP ({vwap:.2f}); skipped")
        return 0
    else:
        journal(
            {
                "day": state["day"],
                "mode": "moonshot_open",
                "action": "skip_inside_opening_range",
                "or_high": or_high,
                "or_low": or_low,
                "spot": spot,
            }
        )
        save_state(state)
        print(f"no breakout: spot {spot} inside opening range [{or_low}, {or_high}]; chop day skipped")
        return 0

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
    entry = Decimal(str(filled.filled_avg_price))
    cost = (entry * CONTRACT_MULTIPLIER).quantize(Decimal("0.01"))
    ledger -= cost

    # The backtested edge banks doubles: rest a +100% take-profit for the day right away.
    tp_price = (entry * 2).quantize(Decimal("0.01"))
    tp_order_id = None
    try:
        tp = trading.submit_order(
            LimitOrderRequest(
                symbol=chosen.symbol, qty=1, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                limit_price=float(tp_price),
            )
        )
        tp_order_id = str(tp.id)
    except Exception as exc:  # a missing TP is a degraded day, not a dead one
        journal({"day": state["day"], "mode": "moonshot_open", "action": f"tp_order_failed: {exc}"})

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
        "tp_order_id": tp_order_id,
        "tp_price": str(tp_price),
    }
    save_state(state)
    journal(
        {
            "day": state["day"],
            "mode": "moonshot_open",
            "action": f"bought 1x {chosen.symbol} ({direction}, strike {chosen.strike_price}, exp {nearest_exp}) @ {filled.filled_avg_price}",
            "breakout": {"or_high": or_high, "or_low": or_low, "spot": spot},
            "cost": str(cost),
            "tp_price": str(tp_price),
            "ledger_cash": str(ledger),
        }
    )
    print(f"day {state['day']}: bought {chosen.symbol} ({direction}) @ {filled.filled_avg_price} (${cost}), TP resting @ {tp_price}; ledger cash {ledger}")
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

    # Alpaca auto-liquidates expiring options from ~15:30 ET and can beat this run to the
    # exit (it did on day 1). If the account no longer holds the contract, book the actual
    # fill from order history instead of submitting a sell that 422s as a new short.
    held = {p.symbol for p in trading.get_all_positions()}
    if position["symbol"] not in held:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        fills = [
            o
            for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50))
            if o.symbol == position["symbol"] and str(o.side) == "OrderSide.SELL" and o.filled_avg_price
        ]
        if fills:
            fill_price = Decimal(str(fills[0].filled_avg_price))
            proceeds = (fill_price * CONTRACT_MULTIPLIER).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            note = f"reconciled: {position['symbol']} sold @ {fill_price} by broker auto-liquidation"
        else:
            proceeds = Decimal("0")  # expired worthless with no closing fill found
            note = f"reconciled: {position['symbol']} expired/removed with no sell fill found; proceeds 0"
        pnl = proceeds - Decimal(position["entry_cost"])
        ledger += proceeds
        state["ledger_cash"] = str(ledger)
        state["position"] = None
        save_state(state)
        journal(
            {
                "day": state["day"], "mode": "moonshot_close", "action": note,
                "proceeds": str(proceeds), "pnl": str(pnl), "ledger_cash": str(ledger),
            }
        )
        print(f"day {state['day']}: {note} (P&L {pnl:+}); ledger {ledger}")
        return 0

    # Did the resting +100% take-profit already bank the day?
    tp_id = position.get("tp_order_id")
    if tp_id:
        tp_order = trading.get_order_by_id(tp_id)
        if tp_order.status == "filled":
            proceeds = (Decimal(str(tp_order.filled_avg_price)) * CONTRACT_MULTIPLIER).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            pnl = proceeds - Decimal(position["entry_cost"])
            ledger += proceeds
            state["ledger_cash"] = str(ledger)
            state["position"] = None
            save_state(state)
            journal(
                {
                    "day": state["day"],
                    "mode": "moonshot_close",
                    "action": f"profit_target_hit: sold {position['symbol']} @ {tp_order.filled_avg_price}",
                    "proceeds": str(proceeds),
                    "pnl": str(pnl),
                    "ledger_cash": str(ledger),
                }
            )
            print(f"day {state['day']}: TP hit intraday, {position['symbol']} @ {tp_order.filled_avg_price} (P&L {pnl:+}); ledger {ledger}")
            return 0
        try:
            trading.cancel_order_by_id(tp_id)  # clear the resting sell so the market order can't double-sell
        except Exception:
            pass

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
