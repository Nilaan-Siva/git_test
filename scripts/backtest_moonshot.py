"""Signal backtest for the moonshot's 0DTE entry rules, on real SPY minute bars.

WHAT THIS IS AND IS NOT. The signals (opening range, VWAP, regime gate, day-of-week) are
computed from real historical SPY minute bars (Alpaca IEX feed). The option P&L is MODELLED:
each trade prices a same-day ATM option with Black-Scholes at entry (hours to expiry) and at
exit (minutes to expiry), constant IV, no smile, no IV crush, no spread crossing. Real 0DTE
fills would be worse. This ranks entry rules against each other; it does not promise their
absolute returns. Proper option-level backtesting needs paid intraday options data.

Variants compared, each compounding a $100 ledger at ~95% per trade like the live bot:
  naive       -- buy at 10:03 in the direction of price vs today's open (day 1's actual rule)
  orb         -- 30-min opening range breakout only
  orb_vwap    -- ORB + VWAP agreement
  full_stack  -- ORB + VWAP + Mon/Wed/Fri + ATR regime band (the live configuration)

Exits for all variants: +100% take-profit (checked minute by minute), else 15:45 ET close.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
IV = 0.14  # flat modelled IV; ~ATM 0DTE SPY territory. Crude on purpose -- see docstring.
LEDGER0 = 100.0
BET_FRACTION = 0.95
MONTHS = 12


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, iv: float, is_call: bool) -> float:
    """Black-Scholes, zero rates -- fine at hours-to-expiry scale."""
    if t_years <= 0:
        intrinsic = spot - strike if is_call else strike - spot
        return max(intrinsic, 0.0)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    if is_call:
        return spot * norm_cdf(d1) - strike * norm_cdf(d2)
    return strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


def fetch_days():
    load_dotenv(REPO_ROOT / ".env")
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    start = datetime.now(timezone.utc) - timedelta(days=int(MONTHS * 30.44))
    bars = client.get_stock_bars(
        StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Minute, start=start, feed=DataFeed.IEX)
    ).data["SPY"]

    # Bucket regular-session bars by trading day (ET session 13:30-20:00 UTC, DST-naive: good
    # enough for a signal ranking; the few off-by-an-hour winter days shift timestamps, not
    # bar order, and every variant sees the same data).
    days: dict[str, list] = {}
    for b in bars:
        ts = b.timestamp
        if dtime(13, 30) <= ts.time() < dtime(20, 0):
            days.setdefault(ts.date().isoformat(), []).append(b)
    return {d: bs for d, bs in sorted(days.items()) if len(bs) >= 300}


def day_signals(day_bars):
    """Everything a variant needs, computed once per day at the 10:03 decision minute."""
    open_px = day_bars[0].open
    range_bars = day_bars[:30]
    decision_idx = 33  # ~10:03 ET
    if len(day_bars) <= decision_idx:
        return None
    or_high = max(b.high for b in range_bars)
    or_low = min(b.low for b in range_bars)
    upto = day_bars[: decision_idx + 1]
    vol = sum(b.volume for b in upto) or 1
    vwap = sum(((b.high + b.low + b.close) / 3) * b.volume for b in upto) / vol
    spot = upto[-1].close
    session_avg_vol = (vol / len(upto)) or 1
    recent_vol = sum(b.volume for b in upto[-3:]) / 3
    return {
        "open": open_px, "or_high": or_high, "or_low": or_low, "vwap": vwap,
        "spot": spot, "decision_idx": decision_idx,
        "vol_ratio": recent_vol / session_avg_vol,
    }


def simulate_trade(day_bars, sig, direction: str, stop_mode=None, exit_mode="tp100", tp_mult=2.0):
    """Model an ATM 0DTE option bought at the decision minute; returns fractional P&L.

    stop_mode: None | "midday" (-50% checked once ~12:30) | "cont" (-50% every minute)
    exit_mode:
      tp100    -- sell all at +100%, else 15:45 close (the live bot)
      partial  -- sell HALF at +50%, remainder at +100% or 15:45 close
      trail    -- once +50% is touched, trail a stop at breakeven (the hybrid the
                  stop-loss literature recommends: fixed risk first, trail once profitable)
    """
    is_call = direction == "call"
    spot = sig["spot"]
    strike = round(spot)
    minutes_left = len(day_bars) - sig["decision_idx"]
    entry_t = minutes_left / (390 * 252)
    entry_px = bs_price(spot, strike, entry_t, IV, is_call)
    if entry_px <= 0.05:
        return None
    tp_px = entry_px * tp_mult
    half_px = entry_px * 1.5
    stop_px = entry_px * 0.5
    midday_i = 147
    banked = 0.0          # realised fraction of the position already sold
    remaining = 1.0
    armed = False         # trailing stop armed (only in "trail" mode)

    for i, b in enumerate(day_bars[sig["decision_idx"] + 1 :], start=1):
        t = max(minutes_left - i, 1) / (390 * 252)
        favourable = b.high if is_call else b.low
        fav_val = bs_price(favourable, strike, t, IV, is_call)
        close_val = bs_price(b.close, strike, t, IV, is_call)

        if exit_mode == "partial" and remaining == 1.0 and fav_val >= half_px:
            banked += 0.5 * (half_px / entry_px)   # half out at +50%
            remaining = 0.5
        if exit_mode == "trail" and not armed and fav_val >= half_px:
            armed = True                            # breakeven stop now live
        if armed and close_val <= entry_px:
            return banked + remaining * 1.0 - 1.0   # out at breakeven

        if fav_val >= tp_px:
            return banked + remaining * tp_mult - 1.0
        if stop_mode == "cont" and close_val <= stop_px:
            return banked + remaining * (close_val / entry_px) - 1.0
        if stop_mode == "midday" and i == midday_i and close_val <= stop_px:
            return banked + remaining * (close_val / entry_px) - 1.0
        if i >= minutes_left - 15:
            return banked + remaining * (close_val / entry_px) - 1.0

    final = bs_price(day_bars[-1].close, strike, 1 / (390 * 252), IV, is_call)
    return banked + remaining * (final / entry_px) - 1.0


def run_variant(days, name: str, stop_mode=None, exit_mode="tp100", bet=BET_FRACTION):
    ledger = LEDGER0
    trades = wins = 0
    peak, max_dd = LEDGER0, 0.0
    returns: list[float] = []
    ruined_on = None
    atr_window: list[float] = []
    prev_close = None
    prev_day_close = None

    for day, bars in days.items():
        # Maintain the daily ATR(14)% series for the regime gate.
        day_high, day_low, day_close = max(b.high for b in bars), min(b.low for b in bars), bars[-1].close
        if prev_close is not None:
            tr = max(day_high - day_low, abs(day_high - prev_close), abs(day_low - prev_close))
            atr_window.append(tr / day_close)
            if len(atr_window) > 14:
                atr_window.pop(0)
        regime_ok = len(atr_window) == 14 and 0.005 <= sum(atr_window) / 14 <= 0.018
        weekday_ok = datetime.fromisoformat(day).weekday() in (0, 2, 4)
        prev_close = day_close

        sig = day_signals(bars)
        if sig is None:
            continue
        direction = None
        if name.startswith("gapfade"):
            if prev_day_close is None:
                prev_day_close = day_close
                continue
            gap = (sig["open"] - prev_day_close) / prev_day_close
            if gap > 0.002 and sig["spot"] > prev_day_close:      # gapped up, not yet filled
                direction = "put"
            elif gap < -0.002 and sig["spot"] < prev_day_close:   # gapped down, not yet filled
                direction = "call"
            prev_day_close = day_close
            if direction is None:
                continue
            ret = simulate_trade(bars, sig, direction, stop_mode=stop_mode, exit_mode=exit_mode)
            if ret is None:
                continue
            trades += 1
            wins += ret > 0
            returns.append(ret)
            if not ruined_on:
                ledger += ledger * bet * ret
                peak = max(peak, ledger)
                max_dd = max(max_dd, 1 - ledger / peak)
                if ledger < 5:
                    ruined_on = day
            continue
        if name.startswith("naive"):
            direction = "call" if sig["spot"] >= sig["open"] else "put"
        else:
            if sig["spot"] > sig["or_high"]:
                direction = "call"
            elif sig["spot"] < sig["or_low"]:
                direction = "put"
            if direction and name in ("orb_vwap", "full_stack"):
                if direction == "call" and sig["spot"] <= sig["vwap"]:
                    direction = None
                if direction == "put" and sig["spot"] >= sig["vwap"]:
                    direction = None
            if direction and name.startswith("full_stack") and not (weekday_ok and regime_ok):
                direction = None
            if direction and "str" in name:
                edge = sig["or_high"] if direction == "call" else sig["or_low"]
                if abs(sig["spot"] - edge) / sig["spot"] < 0.001:  # <0.1% past the line = a poke
                    direction = None
            if direction and "vol" in name and sig["vol_ratio"] < 1.5:
                direction = None
        if direction is None:
            continue

        ret = simulate_trade(bars, sig, direction, stop_mode=stop_mode, exit_mode=exit_mode)
        if ret is None:
            continue
        trades += 1
        wins += ret > 0
        returns.append(ret)
        if not ruined_on:
            ledger += ledger * bet * ret
            peak = max(peak, ledger)
            max_dd = max(max_dd, 1 - ledger / peak)
            if ledger < 5:
                ruined_on = day  # the live bot stops here; keep sampling stats regardless

    returns.sort()
    return {
        "variant": name, "trades": trades,
        "win_rate": wins / trades if trades else 0.0,
        "tp_rate": sum(r >= 0.999 for r in returns) / trades if trades else 0.0,
        "avg_ret": sum(returns) / trades if trades else 0.0,
        "median_ret": returns[trades // 2] if trades else 0.0,
        "final_ledger": ledger, "max_drawdown": max_dd, "ruined_on": ruined_on,
    }


def main() -> int:
    days = fetch_days()
    print(f"{len(days)} trading days of SPY minute bars ({MONTHS} months, IEX feed)")
    print(f"modelled: ATM 0DTE, BS flat IV={IV:.0%}; entry at the 10:03 decision minute\n")
    print(f"{'variant':34s} {'trades':>6s} {'win%':>6s} {'avg ret':>8s} {'$100 ->':>10s} {'ruined':>11s}")
    combos = [
        ("full_stack_str", "midday", "tp100", 0.95, "live bot (95% bet)"),
        ("full_stack_str", "midday", "partial", 0.95, "+ half out at +50%"),
        ("full_stack_str", "midday", "trail", 0.95, "+ breakeven trail"),
        ("full_stack_str", "cont", "trail", 0.95, "+ trail, cont stop"),
        ("full_stack_str", "midday", "tp100", 0.50, "50% bet size"),
        ("full_stack_str", "midday", "partial", 0.50, "50% bet + partial"),
        ("full_stack_str", "midday", "trail", 0.50, "50% bet + trail"),
        ("full_stack_str", "midday", "trail", 0.25, "25% bet + trail"),
        ("orb", "midday", "trail", 0.25, "no M/W/F+regime, 25% + trail"),
        ("gapfade", "midday", "tp100", 0.25, "GAP-FADE 25% bet"),
        ("gapfade", "cont", "tp100", 0.25, "GAP-FADE 25% + cont stop"),
        ("gapfade", "midday", "trail", 0.25, "GAP-FADE 25% + trail"),
    ]
    for name, stop, exit_mode, bet, label in combos:
        r = run_variant(days, name, stop_mode=stop, exit_mode=exit_mode, bet=bet)
        print(
            f"{label:34s} {r['trades']:6d} {r['win_rate']:6.1%} {r['avg_ret']:8.1%} "
            f"{r['final_ledger']:10.2f} {str(r['ruined_on'] or '-'):>11s}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
