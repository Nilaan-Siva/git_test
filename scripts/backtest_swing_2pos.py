"""Does holding 2 positions at once beat 1? Tested against the live 20d/10d baseline.

Same entry/exit rule as the live bot (buy the prior-20-day-high breakout with the strongest
20-day momentum, sell under the prior-10-day low) but with N independent slots instead of one,
each sized at SIZE/N of current cash so total exposure stays capped. When more than N candidates
qualify on the same day, momentum ranks which N get filled -- ties never both go to the same
symbol, and a symbol already held is never double-bought.
"""
import sys
sys.path.insert(0, "scripts")
import backtest_swing_variants as V

def bake_n(n_slots):
    cash = V.START
    positions = {}  # sym -> {qty, entry}
    curve, trades = [], []
    for d in V.days:
        rows = {s: V.ixs[s][d] for s in V.B.UNIVERSE if d in V.ixs[s]}
        # exits first
        for sym in list(positions):
            if sym not in rows:
                continue
            r = rows[sym]
            if r["lo10"] and r["c"] < r["lo10"]:
                px = r["c"] * (1 - V.SLIP)
                cash += positions[sym]["qty"] * px
                trades.append(px / positions[sym]["entry"] - 1)
                del positions[sym]
        # entries: fill open slots with the best-momentum qualifying candidates
        open_slots = n_slots - len(positions)
        if open_slots > 0:
            cands = sorted(
                ((-r["mom20"], s) for s, r in rows.items()
                 if s not in positions and r["hi20"] and r["c"] > r["hi20"] and r["mom20"] is not None)
            )
            for _, s in cands[:open_slots]:
                r = rows[s]
                px = r["c"] * (1 + V.SLIP)
                slot_cash = (cash * V.SIZE) / max(1, (n_slots - len(positions)))
                qty = slot_cash / px
                cash -= qty * px
                positions[s] = {"qty": qty, "entry": px}
        mtm = sum(p["qty"] * rows[s]["c"] for s, p in positions.items() if s in rows)
        curve.append((d, cash + mtm))
    for sym, p in positions.items():
        last_days = [d for d in V.days if d in V.ixs[sym]]
        r = V.ixs[sym][last_days[-1]]
        px = r["c"] * (1 - V.SLIP)
        cash += p["qty"] * px
        trades.append(px / p["entry"] - 1)
    peak = mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    wins = sum(1 for t in trades if t > 0)
    avg = sum(trades) / len(trades) * 100 if trades else 0
    prev, ylines = V.START, []
    for y in sorted({d.year for d, _ in curve}):
        last = [eq for d, eq in curve if d.year == y][-1]
        ylines.append(f"{y}:{(last/prev-1)*100:+.0f}%"); prev = last
    return cash, len(trades), (100*wins/len(trades) if trades else 0), mdd*100, avg, " ".join(ylines)

print(f"{'variant':<24} {'final $':>9} {'ret%':>7} {'trades':>6} {'win%':>5} {'maxDD%':>7} {'avg/trade%':>11}  yearly")
for n in (1, 2, 3):
    f, tr, w, dd, avg, yl = bake_n(n)
    print(f"{n} position(s):"+" "*(24-len(f'{n} position(s):'))+
          f"{f:>9,.0f} {(f/V.START-1)*100:>+6.1f} {tr:>6} {w:>5.0f} {dd:>7.1f} {avg:>+11.3f}  {yl}")

print("\n--- 2-position: cost sensitivity ---")
V.SLIP = 0.0005
f0, tr0, *_ = bake_n(2)
V.SLIP = 0.0010
f1, tr1, *_ = bake_n(2)
V.SLIP = 0.0020
f2, tr2, *_ = bake_n(2)
V.SLIP = 0.0005
print(f"5bps: ${f0:,.0f}  10bps: ${f1:,.0f}  20bps: ${f2:,.0f}  ({tr0} trades)")
