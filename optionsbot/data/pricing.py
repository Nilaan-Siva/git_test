"""Black-Scholes European option pricing, greeks, and implied-volatility solving.

Why this exists: Polygon's free tier gives historical daily OHLC for option contracts but not
historical greeks or implied volatility (those require a paid plan). This module computes both
locally from the daily close via Black-Scholes -- standard practice for backtest signal
generation, not a claim of precise arbitrage-free pricing. SPY options are American-style; the
early-exercise premium this ignores is small for the short-dated, mostly-OTM contracts this bot
targets (put credit spreads, iron condors).

All rates/vols are annualized decimals (0.05 = 5%), T is in years, S/K are per-share prices,
all as plain floats -- this module is iterative numerical math, not money bookkeeping, so it
deliberately doesn't use Decimal (see core/models.py for the Decimal-everywhere money convention
that callers should convert back to at the boundary).
"""
from __future__ import annotations

import math

DEFAULT_RISK_FREE_RATE = 0.05  # rough T-bill proxy; pass `r` explicitly to override


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(s: float, k: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    if sigma <= 0 or t <= 0:
        raise ValueError("sigma and t must be positive")
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2


def bs_price(s: float, k: float, t: float, r: float, sigma: float, is_call: bool) -> float:
    """European option price via Black-Scholes."""
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    if is_call:
        return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)


def bs_greeks(s: float, k: float, t: float, r: float, sigma: float, is_call: bool) -> dict[str, float]:
    """delta, gamma, theta (per calendar day), vega (per 1 vol point, i.e. per 0.01 of sigma --
    the conventional quoting most platforms use)."""
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    pdf_d1 = _norm_pdf(d1)
    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
    gamma = pdf_d1 / (s * sigma * math.sqrt(t))
    vega = s * pdf_d1 * math.sqrt(t) / 100.0
    if is_call:
        theta_annual = -(s * pdf_d1 * sigma) / (2 * math.sqrt(t)) - r * k * math.exp(-r * t) * _norm_cdf(d2)
    else:
        theta_annual = -(s * pdf_d1 * sigma) / (2 * math.sqrt(t)) + r * k * math.exp(-r * t) * _norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta_annual / 365.0, "vega": vega}


def implied_volatility(
    market_price: float,
    s: float,
    k: float,
    t: float,
    r: float,
    is_call: bool,
    *,
    initial_guess: float = 0.30,
    tolerance: float = 1e-6,
    max_iterations: int = 100,
) -> float | None:
    """Solve for sigma via Newton's method (vega as the derivative), falling back to bisection
    if Newton fails to converge (common for deep ITM/OTM or very short-dated contracts, where
    vega is near zero). Returns None -- never a fabricated number -- if `market_price` violates
    a no-arbitrage bound or no solution is found within a wide, sane volatility bracket.
    """
    # European no-arbitrage lower bound uses the *discounted* strike, not spot intrinsic value
    # (S - K / K - S). A deep-ITM European put, for example, can legitimately price below its
    # spot intrinsic value -- that's precisely why American puts carry an early-exercise
    # premium. Using spot intrinsic here would wrongly reject valid deep-ITM prices as
    # "arbitrage violations" and silently return None for real market quotes.
    discounted_k = k * math.exp(-r * t)
    lower_bound = max(0.0, (s - discounted_k) if is_call else (discounted_k - s))
    if market_price < lower_bound - 1e-9:
        return None

    sigma = initial_guess
    for _ in range(max_iterations):
        try:
            price = bs_price(s, k, t, r, sigma, is_call)
            vega_per_unit_sigma = bs_greeks(s, k, t, r, sigma, is_call)["vega"] * 100.0
        except ValueError:
            break
        diff = price - market_price
        if abs(diff) < tolerance:
            return sigma
        if vega_per_unit_sigma < 1e-8:
            break
        sigma -= diff / vega_per_unit_sigma
        if sigma <= 0:
            sigma = 1e-4

    lo, hi = 1e-4, 5.0
    price_lo = bs_price(s, k, t, r, lo, is_call) - market_price
    price_hi = bs_price(s, k, t, r, hi, is_call) - market_price
    if price_lo * price_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        price_mid = bs_price(s, k, t, r, mid, is_call) - market_price
        if abs(price_mid) < tolerance:
            return mid
        if price_lo * price_mid < 0:
            hi = mid
        else:
            lo, price_lo = mid, price_mid
    return (lo + hi) / 2


def years_to_expiration(dte_calendar_days: int) -> float:
    """Convert calendar-day DTE to years, floored at one day so Black-Scholes stays
    well-defined (T>0) on the expiration day itself."""
    return max(dte_calendar_days, 1) / 365.0
