"""Tests for data/pricing.py against known textbook values (Hull, Options Futures and Other
Derivatives) and structural invariants (put-call parity, IV round-trip)."""
import math

import pytest

from optionsbot.data.pricing import bs_greeks, bs_price, implied_volatility, years_to_expiration

# Hull's canonical example: S=42, K=40, r=10%, sigma=20%, T=0.5y, no dividends.
# Call ~= 4.76, N(d1) ~= 0.7791 (call delta).
HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA = 42.0, 40.0, 0.5, 0.10, 0.20


def test_bs_price_matches_hull_call_example():
    call = bs_price(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=True)
    assert call == pytest.approx(4.76, abs=0.01)


def test_bs_price_matches_hull_put_via_parity():
    # C - P = S - K*e^-rT  =>  P = C - S + K*e^-rT
    call = bs_price(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=True)
    put = bs_price(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=False)
    expected_put = call - HULL_S + HULL_K * math.exp(-HULL_R * HULL_T)
    assert put == pytest.approx(expected_put, abs=1e-9)
    assert put == pytest.approx(0.81, abs=0.01)


def test_put_call_parity_holds_generally():
    for s, k, t, r, sigma in [(100, 100, 1.0, 0.03, 0.25), (50, 60, 0.1, 0.01, 0.5), (450, 445, 30 / 365, 0.05, 0.15)]:
        call = bs_price(s, k, t, r, sigma, is_call=True)
        put = bs_price(s, k, t, r, sigma, is_call=False)
        assert call - put == pytest.approx(s - k * math.exp(-r * t), abs=1e-8)


def test_call_delta_matches_hull_example():
    greeks = bs_greeks(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=True)
    assert greeks["delta"] == pytest.approx(0.7791, abs=0.001)


def test_put_delta_is_call_delta_minus_one():
    call_greeks = bs_greeks(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=True)
    put_greeks = bs_greeks(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=False)
    assert put_greeks["delta"] == pytest.approx(call_greeks["delta"] - 1.0, abs=1e-9)


def test_put_delta_is_negative_for_otm_put():
    # a put with strike below spot is OTM; delta should be in (-1, 0)
    greeks = bs_greeks(450, 440, 30 / 365, 0.05, 0.20, is_call=False)
    assert -1.0 < greeks["delta"] < 0.0


def test_gamma_and_vega_are_positive_and_shared_across_call_and_put():
    call_greeks = bs_greeks(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=True)
    put_greeks = bs_greeks(HULL_S, HULL_K, HULL_T, HULL_R, HULL_SIGMA, is_call=False)
    assert call_greeks["gamma"] > 0
    assert call_greeks["gamma"] == pytest.approx(put_greeks["gamma"], abs=1e-9)
    assert call_greeks["vega"] > 0
    assert call_greeks["vega"] == pytest.approx(put_greeks["vega"], abs=1e-9)


def test_implied_volatility_recovers_known_sigma():
    true_sigma = 0.22
    price = bs_price(450, 445, 30 / 365, 0.05, true_sigma, is_call=False)
    solved = implied_volatility(price, 450, 445, 30 / 365, 0.05, is_call=False)
    assert solved == pytest.approx(true_sigma, abs=1e-4)


def test_implied_volatility_recovers_across_a_range_of_moneyness():
    for k in (400, 430, 450, 470, 500):
        true_sigma = 0.18
        price = bs_price(450, k, 45 / 365, 0.05, true_sigma, is_call=False)
        solved = implied_volatility(price, 450, k, 45 / 365, 0.05, is_call=False)
        assert solved == pytest.approx(true_sigma, abs=1e-3)


def test_implied_volatility_returns_none_below_intrinsic_value():
    # a price violating the no-arbitrage lower bound (intrinsic value) has no valid IV
    result = implied_volatility(market_price=0.01, s=450, k=460, t=30 / 365, r=0.05, is_call=False)
    assert result is None


def test_years_to_expiration_floors_at_one_day():
    assert years_to_expiration(0) == pytest.approx(1 / 365)
    assert years_to_expiration(-5) == pytest.approx(1 / 365)
    assert years_to_expiration(30) == pytest.approx(30 / 365)
