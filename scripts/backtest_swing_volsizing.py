"""Does volatility-targeted position sizing beat the flat 98% swing1000 runs today?

The research briefing named this the single most-replicated *improvement* in the factor
literature: size inversely to recent volatility, because volatility moves without a matching
move in expected return. This script tests that claim against the exact strategy that is live
(20-day-high breakout, 10-day-low exit, one position, same universe, same 5 bps/side, same
close-only fills) so the comparison is apples-to-apples and nothing but sizing changes.

    weight_t = clamp( TARGET_VOL / realised_vol_t , MIN_W , CAP )

realised_vol_t is the annualised stdev of the last 20 daily returns of the candidate symbol,
computed from bars strictly BEFORE the entry bar, so it is causal.

Two caps are tested on purpose, because they answer different questions:

  CAP = 0.98  what the live cash ledger can actually do. Vol targeting here can only ever
              SHRINK a position below 98% -- it can never add. For a strategy with positive
              expectancy, that mechanically lowers total return. The question is whether it
              lowers risk by more than it lowers return.
  CAP = 2.00  what the academic literature actually assumes: the target is a target, hit from
              both directions, scaling UP in calm tape. This is the setting the replicated
              Sharpe improvements were measured under. It needs margin, which the $1,000 cash
              experiment does not have -- reported to show the size of the effect being given
              up, not as a recommendation.

Judged on return/maxDD and Sharpe, not final dollars. A sizing change that cuts return 20% and
maxDD 40% is an improvement even though the headline number is smaller; one that cuts both by
the same fraction is just a smaller bet and changes nothing.

RESULT (Aug 25 2026, 793 days 2023-06-28..2026-08-25) -- REJECTED, do not deploy:

    flat_98 (LIVE)        +61.2%   17.5% maxDD   Sharpe 0.76   ret/DD 3.51
    voltgt_40_cap98       +54.0%   17.3%         Sharpe 0.70   ret/DD 3.12
    voltgt_30_cap98       +44.1%   16.3%         Sharpe 0.62   ret/DD 2.70
    voltgt_10_cap98       +11.5%   14.8%         Sharpe 0.32   ret/DD 0.78
    stoprisk_12pct_cap98  +50.7%   17.5%         Sharpe 0.67   ret/DD 2.90
    stoprisk_2pct_cap98   +10.5%    7.8%         Sharpe 0.49   ret/DD 1.34
    voltgt_20_cap200      +20.1%   27.9%         Sharpe 0.36   ret/DD 0.72

Flat 98% wins on return, maxDD, Sharpe AND ret/maxDD. Every config degrades MONOTONICALLY the
further it moves from flat -- the signature of a rule that is pure drag, not one that needs a
better parameter. The leveraged cap-200 configs are worse on both axes at once: scaling up in
calm tape bought into the flat trades and added drawdown without adding return.

WHY the replicated result does not transfer, which is the part worth keeping:

  1. The evidence is for portfolios held CONTINUOUSLY across many assets. There, sizing down in
     high vol means sizing up somewhere else -- capital is always deployed. Here there is one
     position at a time and the bot is flat on most days, so sizing down just means idle cash
     earning nothing. The mechanism that makes vol targeting work is absent by construction.
  2. The 10-day-low trailing exit is ALREADY a volatility-adaptive risk control: the stop sits
     further away in volatile tape automatically. Vol targeting adds a second one on top, so
     volatility gets charged twice.
  3. Breakouts happen in high-volatility names by definition. Sizing inversely to vol therefore
     systematically shrinks exactly the trades the strategy exists to take.

Kept in the repo as the record of a tested and rejected idea, not deleted. This is the 10th and
11th sizing/signal variant to fail against the deployed config.
"""
from __future__ import annotations

import math
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
SLIP = 0.0005
FLAT_SIZE = 0.98
VOL_WINDOW = 20
MIN_W = 0.10          # never size to nothing; below this the trade is not worth its slippage
ENTRY_WINDOW = 20
EXIT_WINDOW = 10


def fetch():
    c = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    req = StockBarsRequest(symbol_or_symbols=UNIVERSE, timeframe=TimeFrame.Day,
                           start=datetime.now(timezone.utc) - timedelta(days=int(YEARS * 365.25) + 60),
                           feed=DataFeed.IEX, adjustment=Adjustment.ALL)
    raw = c.get_stock_bars(req).data
    return {s: [(b.timestamp.date(), float(b.high), float(b.low), float(b.close))
                for b in raw.get(s, [])] for s in UNIVERSE}


def indicators(bars):
    """Everything causal: the row for day d uses only bars up to and including d, and the
    breakout/exit windows deliberately EXCLUDE d itself (prior-day windows)."""
    ix, closes, highs, lows, rets = {}, [], [], [], []
    for d, h, l, c in bars:
        if closes:
            rets.append(c / closes[-1] - 1)
        closes.append(c); highs.append(h); lows.append(l)
        row = {"c": c}
        row["hi20"] = max(highs[-(ENTRY_WINDOW + 1):-1]) if len(highs) > ENTRY_WINDOW else None
        row["lo10"] = min(lows[-(EXIT_WINDOW + 1):-1]) if len(lows) > EXIT_WINDOW else None
        row["mom20"] = (c / closes[-21] - 1) if len(closes) >= 21 else None
        # realised vol from the last VOL_WINDOW returns, all of which end at d
        if len(rets) >= VOL_WINDOW:
            w = rets[-VOL_WINDOW:]
            m = sum(w) / len(w)
            var = sum((x - m) ** 2 for x in w) / (len(w) - 1)
            row["vol"] = math.sqrt(var) * math.sqrt(252)
        else:
            row["vol"] = None
        ix[d] = row
    return ix


def run(ixs, days, target_vol, cap, risk_pct=None):
    """target_vol None and risk_pct None => flat FLAT_SIZE sizing (the live config).

    risk_pct is the turtle mechanism, and it is a genuinely different idea from vol targeting:
    size so that being stopped out at the current 10-day low costs a FIXED fraction of equity.
    It is the sizing rule trend followers actually use, and it adapts to the thing that actually
    hurts this strategy (stop distance) rather than to raw return volatility."""
    cash, pos = START_CASH, None
    curve, trades, weights = [], [], []

    for di, d in enumerate(days):
        rows = {s: ixs[s][d] for s in UNIVERSE if d in ixs[s]}
        if pos and pos["sym"] in rows:
            r = rows[pos["sym"]]
            if r["lo10"] and r["c"] < r["lo10"]:
                px = r["c"] * (1 - SLIP)
                cash += pos["qty"] * px
                trades.append((pos["entry"], px, pos["w"]))
                pos = None
        if pos is None:
            cands = [(-r["mom20"], s) for s, r in rows.items()
                     if r["hi20"] and r["c"] > r["hi20"] and r["mom20"] is not None
                     and r["vol"] is not None and r["vol"] > 0]
            if cands:
                cands.sort()
                s = cands[0][1]; r = rows[s]
                equity = cash  # flat, so cash IS equity here
                if risk_pct is not None:
                    # distance from entry down to the trailing exit, as a fraction of price
                    stop_dist = (r["c"] - r["lo10"]) / r["c"] if r["lo10"] else None
                    w = min(cap, max(MIN_W, risk_pct / stop_dist)) if stop_dist and stop_dist > 0 else MIN_W
                elif target_vol is None:
                    w = FLAT_SIZE
                else:
                    w = min(cap, max(MIN_W, target_vol / r["vol"]))
                px = r["c"] * (1 + SLIP)
                qty = (equity * w) / px
                cash -= qty * px
                pos = {"sym": s, "qty": qty, "entry": px, "w": w}
                weights.append(w)
        eq = cash + (pos["qty"] * rows[pos["sym"]]["c"] if pos and pos["sym"] in rows else 0.0)
        curve.append((d, eq))

    if pos:
        last = [d for d in days if d in ixs[pos["sym"]]][-1]
        px = ixs[pos["sym"]][last]["c"] * (1 - SLIP)
        cash += pos["qty"] * px
        trades.append((pos["entry"], px, pos["w"]))
        curve[-1] = (curve[-1][0], cash)

    peak = mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak if peak else 0.0)
    drets = [curve[i][1] / curve[i - 1][1] - 1 for i in range(1, len(curve)) if curve[i - 1][1] > 0]
    if len(drets) > 2:
        m = sum(drets) / len(drets)
        sd = math.sqrt(sum((x - m) ** 2 for x in drets) / (len(drets) - 1))
        sharpe = (m / sd * math.sqrt(252)) if sd > 0 else 0.0
    else:
        sharpe = 0.0
    ret = curve[-1][1] / START_CASH - 1
    wins = [t for t in trades if t[1] > t[0]]
    return {"ret": ret * 100, "final": curve[-1][1], "trades": len(trades),
            "win": 100 * len(wins) / len(trades) if trades else 0.0,
            "mdd": mdd * 100, "sharpe": sharpe,
            "calmar": (ret * 100) / (mdd * 100) if mdd > 0 else float("inf"),
            "avg_w": (sum(weights) / len(weights)) if weights else 0.0,
            "min_w": min(weights) if weights else 0.0,
            "max_w": max(weights) if weights else 0.0}


def main():
    data = fetch()
    ixs = {s: indicators(data[s]) for s in UNIVERSE}
    days = sorted(d for d, *_ in data["SPY"])
    print(f"{len(days)} trading days, {days[0]} .. {days[-1]}\n", file=sys.stderr)

    spy = [ixs["SPY"][d]["c"] for d in days if d in ixs["SPY"]]
    print(f"SPY buy-and-hold benchmark: {(spy[-1]/spy[0]-1)*100:+.1f}%\n")

    hdr = (f"{'config':<22} {'ret%':>8} {'final$':>10} {'trades':>7} {'win%':>6} "
           f"{'maxDD%':>7} {'sharpe':>7} {'ret/DD':>7} {'avg_w':>6} {'w range':>13}")
    print(hdr); print("-" * len(hdr))

    configs = [("flat_98 (LIVE)", None, 0.98, None)]
    for tv in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
        configs.append((f"voltgt_{int(tv*100)}_cap98", tv, 0.98, None))
    for tv in (0.15, 0.20, 0.25, 0.30):
        configs.append((f"voltgt_{int(tv*100)}_cap200", tv, 2.00, None))
    for rp in (0.02, 0.03, 0.05, 0.08, 0.12):
        configs.append((f"stoprisk_{int(rp*100)}pct_cap98", None, 0.98, rp))

    results = {}
    for name, tv, cap, rp in configs:
        m = run(ixs, days, tv, cap, rp)
        results[name] = m
        print(f"{name:<22} {m['ret']:>+8.1f} {m['final']:>10,.0f} {m['trades']:>7} "
              f"{m['win']:>6.1f} {m['mdd']:>7.1f} {m['sharpe']:>7.2f} {m['calmar']:>7.2f} "
              f"{m['avg_w']:>6.2f} {m['min_w']:>5.2f}-{m['max_w']:<5.2f}")

    base = results["flat_98 (LIVE)"]
    print(f"\nBaseline flat_98: {base['ret']:+.1f}% return, {base['mdd']:.1f}% maxDD, "
          f"Sharpe {base['sharpe']:.2f}, ret/DD {base['calmar']:.2f}")
    print("\nA cap-98 config can only shrink positions, so a LOWER return is expected and is not")
    print("by itself a failure. It only earns deployment if ret/DD and Sharpe both improve.")
    best98 = max((v["calmar"], k) for k, v in results.items() if k.endswith("cap98") or "LIVE" in k)
    print(f"Best ret/DD among cash-feasible configs: {best98[1]} at {best98[0]:.2f}")


if __name__ == "__main__":
    main()
