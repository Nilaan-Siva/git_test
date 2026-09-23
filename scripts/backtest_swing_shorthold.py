"""Does shortening the hold to a few days help? Tested, not assumed.

The user wants "a couple of days instead of weeks" for more profit. Three ways to get there,
each isolating one change so we can see what's actually driving any difference:

  time2/time3   SAME 20d-high entry as the live bot, but forced exit after 2 or 3 days
                regardless of price -- isolates "does cutting the hold short help, holding
                the entry signal constant?"
  fast5/3       5-day-high entry (a much twitchier signal) + 3-day-low exit -- the
                "actually trade faster end to end" version
  fast5/3+time3 same fast entry, but also cap the hold at 3 days even if not stopped out

All four run through the same split-adjusted data and 5bps/side costs as the live baseline.
"""
import sys
sys.path.insert(0, "scripts")
import backtest_swing_variants as V  # reuses build(), ixs, days, START/SLIP/SIZE

def bake_time(entry_key, max_days):
    cash, pos = V.START, None
    curve, trades = [], []
    for d in V.days:
        rows = {s: V.ixs[s][d] for s in V.B.UNIVERSE if d in V.ixs[s]}
        if pos and pos["sym"] in rows:
            r = rows[pos["sym"]]
            pos["held"] += 1
            if pos["held"] >= max_days:
                px = r["c"] * (1 - V.SLIP); cash += pos["qty"] * px
                trades.append(px / pos["entry"] - 1); pos = None
        if pos is None:
            cands = [(-r["mom20"], s) for s, r in rows.items()
                     if r[entry_key] and r["c"] > r[entry_key] and r["mom20"] is not None]
            if cands:
                cands.sort(); s = cands[0][1]; r = rows[s]
                px = r["c"] * (1 + V.SLIP); qty = cash * V.SIZE / px; cash -= qty * px
                pos = {"sym": s, "qty": qty, "entry": px, "held": 0}
        curve.append((d, cash + (pos["qty"] * rows[pos["sym"]]["c"]
                                 if pos and pos["sym"] in rows else 0)))
    if pos:
        r = V.ixs[pos["sym"]][[d for d in V.days if d in V.ixs[pos["sym"]]][-1]]
        cash += pos["qty"] * r["c"] * (1 - V.SLIP); trades.append(r["c"] * (1 - V.SLIP) / pos["entry"] - 1)
    peak = mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = sum(1 for t in trades if t > 0)
    avg = sum(trades) / len(trades) * 100 if trades else 0
    return cash, len(trades), (100 * wins / len(trades) if trades else 0), mdd * 100, avg

def bake_fast(entry_key, exit_key, max_days=None):
    cash, pos = V.START, None
    curve, trades = [], []
    for d in V.days:
        rows = {s: V.ixs[s][d] for s in V.B.UNIVERSE if d in V.ixs[s]}
        if pos and pos["sym"] in rows:
            r = rows[pos["sym"]]; sell = False
            pos["held"] += 1
            if r[exit_key] and r["c"] < r[exit_key]: sell = True
            elif max_days and pos["held"] >= max_days: sell = True
            if sell:
                px = r["c"] * (1 - V.SLIP); cash += pos["qty"] * px
                trades.append(px / pos["entry"] - 1); pos = None
        if pos is None:
            cands = [(-r["mom20"], s) for s, r in rows.items()
                     if r[entry_key] and r["c"] > r[entry_key] and r["mom20"] is not None]
            if cands:
                cands.sort(); s = cands[0][1]; r = rows[s]
                px = r["c"] * (1 + V.SLIP); qty = cash * V.SIZE / px; cash -= qty * px
                pos = {"sym": s, "qty": qty, "entry": px, "held": 0}
        curve.append((d, cash + (pos["qty"] * rows[pos["sym"]]["c"]
                                 if pos and pos["sym"] in rows else 0)))
    if pos:
        r = V.ixs[pos["sym"]][[d for d in V.days if d in V.ixs[pos["sym"]]][-1]]
        cash += pos["qty"] * r["c"] * (1 - V.SLIP); trades.append(r["c"] * (1 - V.SLIP) / pos["entry"] - 1)
    peak = mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = sum(1 for t in trades if t > 0)
    avg = sum(trades) / len(trades) * 100 if trades else 0
    return cash, len(trades), (100 * wins / len(trades) if trades else 0), mdd * 100, avg

print("Needs a 5-day-high/3-day-low column pair, adding to indicator rows first...")
for s in V.B.UNIVERSE:
    bars = V.data[s]
    highs, lows = [], []
    for i, (d, o, h, l, c) in enumerate(bars):
        highs.append(h); lows.append(l)
        V.ixs[s][d]["hi5"] = max(highs[-6:-1]) if len(highs) >= 6 else None
        V.ixs[s][d]["lo3"] = min(lows[-4:-1]) if len(lows) >= 4 else None

print(f"\n{'variant':<32} {'final $':>9} {'ret%':>7} {'trades':>6} {'win%':>5} {'maxDD%':>7} {'avg/trade%':>11}")
for label, fn, args in [
    ("baseline 20d-in/10d-out (LIVE)", bake_fast, ("hi20", "lo10", None)),
    ("20d-in, forced exit in 2 days", bake_time, ("hi20", 2)),
    ("20d-in, forced exit in 3 days", bake_time, ("hi20", 3)),
    ("5d-in / 3d-out (fast)", bake_fast, ("hi5", "lo3", None)),
    ("5d-in / 3d-out, cap 3 days", bake_fast, ("hi5", "lo3", 3)),
]:
    f, n, w, dd, avg = fn(*args)
    print(f"{label:<32} {f:>9,.0f} {(f/V.START-1)*100:>+6.1f} {n:>6} {w:>5.0f} {dd:>7.1f} {avg:>+11.3f}")
