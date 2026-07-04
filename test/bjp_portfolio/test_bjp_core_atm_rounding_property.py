"""Property-based test for the round_atm ATM-reference computation.

Implements Property 8 from the design: ATM rounding is the nearest strike
multiple, with ties (a spot exactly halfway between two multiples) rounding up
to the higher multiple (Req 4.4). The production code under test lives in
``strategies/bjp_portfolio/bjp_core.py`` and is imported as ``bjp_core`` via the
sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 8: ATM rounding is nearest
# multiple with ties rounding up. For any positive spot price and strike step
# in {50, 100}, the computed ATM reference is an exact multiple of the step,
# its absolute distance from the spot is at most half the step, and a spot
# exactly halfway between two multiples rounds up to the higher multiple.
# Validates: Requirements 4.4.

# Small tolerance for the float distance comparison; result is an exact int and
# spot is a float, so abs(result - spot) is computed in floating point.
_EPS = 1e-9


@settings(max_examples=200)
@given(
    spot=st.floats(
        min_value=0.01,
        max_value=200_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    step=st.sampled_from([50, 100]),
)
# Exact half-step boundaries must round up to the higher multiple.
@example(spot=25.0, step=50)  # halfway above 0 -> 50
@example(spot=75.0, step=50)  # halfway above 50 -> 100
@example(spot=125.0, step=50)  # halfway above 100 -> 150
@example(spot=50.0, step=100)  # halfway above 0 -> 100
@example(spot=150.0, step=100)  # halfway above 100 -> 200
@example(spot=250.0, step=100)  # halfway above 200 -> 300
def test_round_atm_is_nearest_multiple_ties_up(spot: float, step: int):
    result = core.round_atm(spot, step)

    # The result is an exact multiple of the step.
    assert isinstance(result, int)
    assert result % step == 0

    # The result is within half a step of the spot (ties land exactly at
    # half a step because they round up to the higher multiple).
    assert abs(result - spot) <= step / 2 + _EPS

    # Ties round up: when the spot is exactly halfway between two multiples,
    # the result is the higher multiple, so result - spot == step / 2 exactly.
    lower = (int(spot) // step) * step
    if spot - lower == step / 2:
        assert result == lower + step


@settings(max_examples=200)
@given(
    k=st.integers(min_value=0, max_value=4000),
    step=st.sampled_from([50, 100]),
)
def test_round_atm_exact_half_rounds_to_higher_multiple(k: int, step: int):
    # A spot exactly halfway between k*step and (k+1)*step rounds up.
    half = step // 2  # 25 for step 50, 50 for step 100 (both exact as floats)
    spot = float(k * step + half)

    result = core.round_atm(spot, step)

    assert result == (k + 1) * step
    assert result % step == 0
    assert abs(result - spot) == pytest.approx(step / 2)


def test_round_atm_explicit_boundary_examples():
    # Just below the half-step boundary rounds down to the lower multiple.
    assert core.round_atm(24.99, 50) == 0
    assert core.round_atm(74.99, 50) == 50
    assert core.round_atm(49.99, 100) == 0
    assert core.round_atm(149.99, 100) == 100

    # Exactly on the half-step boundary rounds up to the higher multiple.
    assert core.round_atm(25.0, 50) == 50
    assert core.round_atm(75.0, 50) == 100
    assert core.round_atm(50.0, 100) == 100
    assert core.round_atm(150.0, 100) == 200

    # Just above the half-step boundary stays at the higher multiple.
    assert core.round_atm(25.01, 50) == 50
    assert core.round_atm(50.01, 100) == 100

    # Exact multiples map to themselves.
    assert core.round_atm(50.0, 50) == 50
    assert core.round_atm(100.0, 100) == 100
