"""Refinement bake-off for the deployed 20d/10d breakout.

Each variant is a single principled change, tested alone, so we can see what each knob
actually does. The selection rule is robustness (every year >= roughly flat, drawdown,
cost-insensitivity) BEFORE raw return -- picking the top backtest number out of a pile of
variants is how you overfit a bull window and die in the next regime.
"""
import sys, collections
sys.path.insert(0, "scripts")
import backtest_swing as B

data = B.fetch()
days = sorted(d for d, *_ in data["SPY"])
START, SLIP, SIZE = 1000.0, 0.0005, 0.98

# extended per-symbol indicator rows: entry-high/exit-low for several windows, ATR14
def build(bars):
    ix, closes, highs, lows, trs = {}, [], [], [], []
    for i, (d, o, h, l, c) in enumerate(bars):
        if closes:
            trs.append(max(h - l, abs(h - closes[-1]), abs(l - closes[-1])))
        closes.append(c); highs.append(h); lows.append(l)
        row = {"c": c,
               "hi20": max(highs[-21:-1]) if len(highs) >= 21 else None,
               "hi55": max(highs[-56:-1]) if len(highs) >= 56 else None,
               "lo10": min(lows[-11:-1]) if len(lows) >= 11 else None,
               "lo20": min(lows[-21:-1]) if len(lows) >= 21 else None,
               "mom20": (c / closes[-21] - 1) if len(closes) >= 21 else None,
               "atr14": sum(trs[-14:]) / 14 if len(trs) >= 14 else None,
               "sma200": sum(closes[-200:]) / 200 if len(closes) >= 200 else None}
        ix[d] = row
    return ix

ixs = {s: build(data[s]) for s in B.UNIVERSE}

def bake(entry_key, exit_mode, regime=False):
    cash, pos = START, None
    curve, trades = [], []
    for d in days:
        rows = {s: ixs[s][d] for s in B.UNIVERSE if d in ixs[s]}
        spy_ok = (not regime) or (rows.get("SPY", {}).get("sma200") and
                                  rows["SPY"]["c"] > rows["SPY"]["sma200"])
        if pos and pos["sym"] in rows:
            r = rows[pos["sym"]]; sell = False
            if exit_mode == "lo10" and r["lo10"] and r["c"] < r["lo10"]: sell = True
            elif exit_mode == "lo20" and r["lo20"] and r["c"] < r["lo20"]: sell = True
            elif exit_mode == "atr":
                pos["peak"] = max(pos["peak"], r["c"])
                if r["atr14"] and r["c"] < pos["peak"] - 3 * r["atr14"]: sell = True
            if sell:
                px = r["c"] * (1 - SLIP); cash += pos["qty"] * px
                trades.append(px / pos["entry"] - 1); pos = None
        if pos is None and spy_ok:
            cands = [(-r["mom20"], s) for s, r in rows.items()
                     if r[entry_key] and r["c"] > r[entry_key] and r["mom20"] is not None]
            if cands:
                cands.sort(); s = cands[0][1]; r = rows[s]
                px = r["c"] * (1 + SLIP); qty = cash * SIZE / px; cash -= qty * px
                pos = {"sym": s, "qty": qty, "entry": px, "peak": px}
        curve.append((d, cash + (pos["qty"] * rows[pos["sym"]]["c"]
                                 if pos and pos["sym"] in rows else 0)))
    if pos:
        r = ixs[pos["sym"]][[d for d in days if d in ixs[pos["sym"]]][-1]]
        cash += pos["qty"] * r["c"] * (1 - SLIP); trades.append(r["c"] * (1 - SLIP) / pos["entry"] - 1)
    peak = mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    prev, ylines = START, []
    for y in sorted({d.year for d, _ in curve}):
        last = [eq for d, eq in curve if d.year == y][-1]
        ylines.append(f"{y}:{(last/prev-1)*100:+.0f}%"); prev = last
    wins = sum(1 for t in trades if t > 0)
    return cash, len(trades), (100*wins/len(trades) if trades else 0), mdd*100, " ".join(ylines)

variants = [
    ("baseline 20d-in / 10d-out (LIVE)",       "hi20", "lo10", False),
    ("turtle-classic 55d-in / 20d-out",        "hi55", "lo20", False),
    ("20d-in / 3xATR trailing stop",           "hi20", "atr",  False),
    ("20d/10d + SPY>200SMA regime filter",     "hi20", "lo10", True),
    ("55d/20d + regime filter",                "hi55", "lo20", True),
]
print(f"{'variant':<38} {'final $':>9} {'ret%':>7} {'trades':>6} {'win%':>5} {'maxDD%':>7}  yearly")
for name, ek, xm, rg in variants:
    f, n, w, dd, yl = bake(ek, xm, rg)
    print(f"{name:<38} {f:>9,.0f} {(f/START-1)*100:>+6.1f} {n:>6} {w:>5.0f} {dd:>7.1f}  {yl}")
