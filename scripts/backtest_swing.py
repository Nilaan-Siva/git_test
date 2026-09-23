"""Swing-trading bake-off on free Alpaca daily bars.

Four documented swing strategies, tested under the same one-decision-per-day regime the live
bots actually run (signals and exits at the daily close, stops evaluated on the close so an
overnight gap-through is charged in full, never at the stop's fantasy price):

  rsi2      Connors RSI(2) mean reversion: close>200SMA and RSI2<10, exit close>5SMA, 10d cap
  breakout  20-day-high breakout, exit on close under the 10-day low (turtle-lite)
  momo      20-day momentum rotation, hold the leader while its return is positive
  fvg       daily fair value gap: today's low > high two days ago, in a 200SMA uptrend,
            stop at the gap bottom, 2R target, 10d cap

Costs: 5 bps slippage per side, zero commission (Alpaca stocks). One position at a time,
98% of equity per entry, fractional shares. Ties broken by signal strength, deterministically.

The point of the per-year breakdown: 2023-2026 was mostly a bull tape, so anything long-biased
looks smart on the full period. A strategy only counts as robust here if every year is at
least roughly flat AND it beats or ties SPY buy-and-hold, which is the do-nothing benchmark
any swing strategy must clear to justify its existence.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLE", "XLF", "GLD", "TLT",
            "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]
YEARS = 3
START_CASH = 1000.0
SLIP = 0.0005  # 5 bps per side
SIZE = 0.98


def fetch():
    c = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    req = StockBarsRequest(symbol_or_symbols=UNIVERSE, timeframe=TimeFrame.Day,
                           start=datetime.now(timezone.utc) - timedelta(days=int(YEARS * 365.25) + 30),
                           feed=DataFeed.IEX, adjustment=Adjustment.ALL)
    raw = c.get_stock_bars(req).data
    out = {}
    for sym in UNIVERSE:
        series = raw.get(sym, [])
        out[sym] = [(b.timestamp.date(), float(b.open), float(b.high), float(b.low), float(b.close))
                    for b in series]
    return out


def indicators(bars):
    """Per symbol: dict date -> row with everything each strategy needs, computed causally."""
    ix = {}
    closes, highs, lows = [], [], []
    gains, losses = [], []
    for i, (d, o, h, l, cl) in enumerate(bars):
        closes.append(cl); highs.append(h); lows.append(l)
        if i > 0:
            ch = cl - closes[-2]
            gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
        row = {"o": o, "h": h, "l": l, "c": cl}
        row["sma200"] = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        row["sma5"] = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        if len(gains) >= 2:
            ag, al = sum(gains[-2:]) / 2, sum(losses[-2:]) / 2
            row["rsi2"] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            row["rsi2"] = None
        # prior-day windows: a breakout must clear yesterday's 20-day high, not its own
        row["hi20"] = max(highs[-21:-1]) if len(highs) >= 21 else None
        row["lo10"] = min(lows[-11:-1]) if len(lows) >= 11 else None
        row["mom20"] = (cl / closes[-21] - 1) if len(closes) >= 21 else None
        row["fvg"] = (len(lows) >= 3 and lows[-1] > highs[-3])  # bullish 3-candle gap
        row["fvg_bottom"] = highs[-3] if len(highs) >= 3 else None
        ix[d] = row
    return ix


def run(name, data, ixs, days):
    cash, pos = START_CASH, None  # pos: {sym, qty, entry, stop, target, opened}
    equity_curve, trades = [], []

    def mark(d):
        if pos and d in ixs[pos["sym"]]:
            return cash + pos["qty"] * ixs[pos["sym"]][d]["c"]
        return cash + (pos["qty"] * pos["entry"] if pos else 0)

    for di, d in enumerate(days):
        rows = {s: ixs[s][d] for s in UNIVERSE if d in ixs[s]}
        # ---- exits ----
        if pos and pos["sym"] in rows:
            r, sell, why = rows[pos["sym"]], False, ""
            held = di - pos["opened"]
            if name == "rsi2":
                if r["sma5"] and r["c"] > r["sma5"]: sell, why = True, "target"
                elif held >= 10: sell, why = True, "time"
            elif name == "breakout":
                if r["lo10"] and r["c"] < r["lo10"]: sell, why = True, "trail"
            elif name == "momo":
                if r["mom20"] is not None and r["mom20"] <= 0: sell, why = True, "momo_off"
            elif name == "fvg":
                if r["c"] <= pos["stop"]: sell, why = True, "stop"
                elif r["c"] >= pos["target"]: sell, why = True, "target"
                elif held >= 10: sell, why = True, "time"
            if sell:
                px = r["c"] * (1 - SLIP)
                cash += pos["qty"] * px
                trades.append((pos["sym"], pos["entry"], px, why))
                pos = None
        # ---- entries (same-close, only when flat) ----
        if pos is None:
            cands = []
            for s, r in rows.items():
                if name == "rsi2":
                    if r["sma200"] and r["rsi2"] is not None and r["c"] > r["sma200"] and r["rsi2"] < 10:
                        cands.append((r["rsi2"], s))          # most oversold first
                elif name == "breakout":
                    if r["hi20"] and r["c"] > r["hi20"] and r["mom20"] is not None:
                        cands.append((-r["mom20"], s))        # strongest momentum first
                elif name == "momo":
                    if s in ("SPY","QQQ","IWM","DIA","XLK","XLE","XLF","GLD","TLT") and \
                       r["mom20"] is not None and r["mom20"] > 0:
                        cands.append((-r["mom20"], s))
                elif name == "fvg":
                    if r["fvg"] and r["sma200"] and r["c"] > r["sma200"] and r["fvg_bottom"]:
                        gap = (r["l"] - r["fvg_bottom"]) / r["c"]
                        cands.append((-gap, s))               # widest gap first
            if cands:
                cands.sort()
                s = cands[0][1]; r = rows[s]
                # momo: don't churn — only rotate if not just sold same bar (flat here anyway)
                px = r["c"] * (1 + SLIP)
                qty = (cash * SIZE) / px
                cash -= qty * px
                pos = {"sym": s, "qty": qty, "entry": px, "opened": di,
                       "stop": (r["fvg_bottom"] * 0.999 if name == "fvg" else 0.0),
                       "target": (px + 2 * (px - r["fvg_bottom"] * 0.999) if name == "fvg" else 1e18)}
        equity_curve.append((d, mark(d)))
    # liquidate for comparability
    if pos:
        lastr = ixs[pos["sym"]][[d for d in days if d in ixs[pos["sym"]]][-1]]
        px = lastr["c"] * (1 - SLIP)
        cash += pos["qty"] * px
        trades.append((pos["sym"], pos["entry"], px, "eod_liquidate"))

    wins = [t for t in trades if t[2] > t[1]]
    peak, mdd = 0.0, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    yearly = {}
    for d, eq in equity_curve:
        yearly.setdefault(d.year, [eq, eq])[1] = eq
        yearly[d.year][0] = min(yearly[d.year][0], 0) or yearly[d.year][0]
    # yearly return = last equity of year / last equity of prior year
    years = sorted({d.year for d, _ in equity_curve})
    ylines = []
    prev = START_CASH
    for y in years:
        last = [eq for d, eq in equity_curve if d.year == y][-1]
        ylines.append(f"{y}:{(last/prev-1)*100:+.1f}%")
        prev = last
    return {
        "final": cash, "trades": len(trades), "win%": 100 * len(wins) / len(trades) if trades else 0,
        "avg_trade%": (sum((t[2]/t[1]-1) for t in trades) / len(trades) * 100) if trades else 0,
        "maxDD%": mdd * 100, "yearly": " ".join(ylines),
    }


def main():
    data = fetch()
    for s in UNIVERSE:
        print(f"{s}: {len(data[s])} bars", file=sys.stderr)
    ixs = {s: indicators(data[s]) for s in UNIVERSE}
    days = sorted(d for d, *_ in data["SPY"])

    spy = [r["c"] for d, r in sorted(ixs["SPY"].items())]
    bh = START_CASH * (spy[-1] / spy[0])
    print(f"\nSPY buy-and-hold over the same {YEARS}y window: ${bh:,.2f}  "
          f"({(spy[-1]/spy[0]-1)*100:+.1f}%)\n")
    print(f"{'strategy':<10} {'final $':>10} {'ret%':>8} {'trades':>7} {'win%':>6} "
          f"{'avg/trade%':>11} {'maxDD%':>7}  yearly")
    for name in ("rsi2", "breakout", "momo", "fvg"):
        m = run(name, data, ixs, days)
        print(f"{name:<10} {m['final']:>10,.2f} {(m['final']/START_CASH-1)*100:>+7.1f} "
              f"{m['trades']:>7} {m['win%']:>6.1f} {m['avg_trade%']:>+11.3f} "
              f"{m['maxDD%']:>7.1f}  {m['yearly']}")


if __name__ == "__main__":
    main()
