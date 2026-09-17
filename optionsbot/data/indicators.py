"""Pure-function technical indicators for strategy entry filters and the regime classifier.

Operate on plain lists of floats (already-parsed price series) -- no pandas dependency here, so
these are trivial to unit test against hand-computed values. Every function returns a list the
same length as its input, with `None` for the warmup period before enough history exists;
callers should never see a fabricated number where there isn't enough data yet.
"""
from __future__ import annotations

from typing import Optional, Sequence


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1 : i + 1]) / period
    return out


def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period  # SMA seed -- the conventional EMA start
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def bollinger_bands(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> dict[str, list[Optional[float]]]:
    mid = sma(values, period)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = mid[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance**0.5
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return {"mid": mid, "upper": upper, "lower": lower}


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi(values: Sequence[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's RSI. Needs `period` price changes (period+1 prices) before the first value."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    deltas = [values[i] - values[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def atr(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14
) -> list[Optional[float]]:
    """Wilder's Average True Range."""
    n = len(close)
    if not (len(high) == len(low) == n):
        raise ValueError("high, low, and close must be the same length")
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    true_ranges = [
        max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(1, n)
    ]
    avg_tr = sum(true_ranges[:period]) / period
    out[period] = avg_tr
    for i in range(period, len(true_ranges)):
        avg_tr = (avg_tr * (period - 1) + true_ranges[i]) / period
        out[i + 1] = avg_tr
    return out


def iv_rank(current_iv: float, historical_iv: Sequence[float]) -> Optional[float]:
    """Where current IV sits within its historical [min, max] range, as 0-100. `historical_iv`
    should be a trailing window (conventionally ~252 trading days) NOT including today. None if
    there's no history or the range is degenerate (min == max)."""
    if not historical_iv:
        return None
    lo, hi = min(historical_iv), max(historical_iv)
    if hi == lo:
        return None
    return (current_iv - lo) / (hi - lo) * 100.0


def iv_percentile(current_iv: float, historical_iv: Sequence[float]) -> Optional[float]:
    """The fraction of days in `historical_iv` with IV below current IV, as 0-100."""
    if not historical_iv:
        return None
    below = sum(1 for v in historical_iv if v < current_iv)
    return below / len(historical_iv) * 100.0
