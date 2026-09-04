"""Tests for data/indicators.py against hand-computed values."""
import pytest

from optionsbot.data.indicators import atr, bollinger_bands, ema, iv_percentile, iv_rank, rsi, sma


def test_sma_basic():
    out = sma([1, 2, 3, 4, 5], period=3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_sma_rejects_non_positive_period():
    with pytest.raises(ValueError):
        sma([1, 2, 3], period=0)


def test_ema_matches_hand_computation():
    # period=3, k=0.5: seed = mean(1,2,3)=2 at index 2; then out[3]=4*0.5+2*0.5=3; out[4]=5*0.5+3*0.5=4
    out = ema([1, 2, 3, 4, 5], period=3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_ema_returns_all_none_when_shorter_than_period():
    assert ema([1, 2], period=5) == [None, None]


def test_bollinger_bands_known_stdev_example():
    # classic textbook set: mean=5, population stdev=2
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    bands = bollinger_bands(values, period=8, num_std=2.0)
    assert bands["mid"][7] == pytest.approx(5.0)
    assert bands["upper"][7] == pytest.approx(9.0)
    assert bands["lower"][7] == pytest.approx(1.0)
    assert bands["mid"][:7] == [None] * 7


def test_rsi_matches_hand_computed_wilder_series():
    # period=2 for a hand-checkable series; see derivation in the accompanying PR/commit notes.
    values = [1, 2, 1, 2, 3]
    out = rsi(values, period=2)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(50.0)
    assert out[3] == pytest.approx(75.0)
    assert out[4] == pytest.approx(87.5)


def test_rsi_is_100_when_no_losses():
    values = [1, 2, 3, 4, 5, 6]
    out = rsi(values, period=3)
    assert out[3] == pytest.approx(100.0)


def test_rsi_returns_none_for_insufficient_data():
    assert rsi([1, 2], period=14) == [None, None]


def test_atr_matches_hand_computed_constant_range_series():
    high = [10, 10, 10]
    low = [8, 8, 8]
    close = [9, 9, 9]
    out = atr(high, low, close, period=2)
    assert out == [None, None, pytest.approx(2.0)]


def test_atr_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        atr([1, 2], [1], [1, 2])


def test_iv_rank_basic():
    assert iv_rank(50, [20, 30, 40, 60, 80]) == pytest.approx(50.0)


def test_iv_rank_none_when_range_degenerate():
    assert iv_rank(50, [30, 30, 30]) is None


def test_iv_rank_none_when_no_history():
    assert iv_rank(50, []) is None


def test_iv_percentile_basic():
    assert iv_percentile(50, [10, 20, 30, 60, 70]) == pytest.approx(60.0)


def test_iv_percentile_none_when_no_history():
    assert iv_percentile(50, []) is None
