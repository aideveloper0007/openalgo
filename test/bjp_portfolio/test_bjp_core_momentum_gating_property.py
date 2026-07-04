"""Property-based test for momentum-gated entry.

Implements Property 14 from the design: for any candidate momentum leg with a
recorded reference premium ``R`` and a ``PointsDown`` threshold ``N``, and any
price path within the entry-exit window, the leg's entry order is placed if and
only if the LTP reaches or falls below ``R - N`` during the window, and is never
placed while the LTP stays above ``R - N`` (Req 9.2, 9.3).

The gating predicate under test is ``should_enter_momentum_leg`` (with its
helpers ``momentum_entry_threshold`` / ``momentum_satisfied``), which is a pure,
non-mutating evaluation the monitoring loop calls on each price update while the
leg is ``pending_momentum``. The comparison is **inclusive**: a premium exactly
at ``R - N`` enters.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 14: Momentum gating enters only
# after the required fall. For a candidate momentum leg with reference premium R
# and PointsDown threshold N (so the entry level is R - N), and any price path in
# the entry-exit window, should_enter_momentum_leg returns True at a cycle iff
# that cycle's LTP has reached or fallen below R - N (inclusive); it never
# returns True while the LTP stays strictly above R - N, and across a path the
# leg is entered iff the minimum LTP on the path reaches R - N.
# Validates: Requirements 9.2, 9.3.

# Positive, finite reference premiums bounded well below float precision loss so
# the exact-at-threshold equality remains representable.
_reference = st.floats(
    min_value=1.0,
    max_value=50_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# PointsDown magnitude N: 0..5000 points of required fall.
_points_down = st.floats(
    min_value=0.0,
    max_value=5_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Candidate premiums observed during the window: anywhere from zero upward.
_premiums = st.floats(
    min_value=0.0,
    max_value=100_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# A price path: a non-empty sequence of observed premiums across cycles.
_price_paths = st.lists(_premiums, min_size=1, max_size=40)

# Strictly-positive gaps used to build paths that stay above the threshold.
_positive_gaps = st.floats(
    min_value=1e-3,
    max_value=10_000.0,
    allow_nan=False,
    allow_infinity=False,
)


def _armed_momentum_leg(reference: float, points_down: float) -> core.LegState:
    """Build a pending momentum short leg with its reference premium recorded."""
    leg = core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="ATM",
            momentum=core.LegMomentum(points_down=points_down),
        ),
    )
    # Arm the leg exactly as the engine does at Entry_Time (Req 9.1): this sets
    # momentum_ref = reference and pending_momentum = True.
    recorded = core.record_momentum_reference(leg, ltp=reference)
    assert recorded is True
    assert leg.pending_momentum is True
    assert leg.momentum_ref == reference
    return leg


# ---------------------------------------------------------------------------
# Per-cycle: enter iff LTP <= R - N (inclusive)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(reference=_reference, points_down=_points_down, premium=_premiums)
def test_momentum_entry_iff_at_or_below_threshold(
    reference: float, points_down: float, premium: float
):
    leg = _armed_momentum_leg(reference, points_down)
    threshold = reference - points_down
    assert core.should_enter_momentum_leg(leg, premium=premium) == (premium <= threshold)


@settings(max_examples=200)
@given(reference=_reference, points_down=_points_down)
def test_momentum_entry_exactly_at_threshold(reference: float, points_down: float):
    leg = _armed_momentum_leg(reference, points_down)
    threshold = reference - points_down
    # Premium exactly at R - N enters (inclusive); just above does not.
    assert core.should_enter_momentum_leg(leg, premium=threshold) is True
    assert core.should_enter_momentum_leg(leg, premium=threshold + 1.0) is False


# ---------------------------------------------------------------------------
# Never enter while the LTP stays strictly above R - N
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    reference=_reference,
    points_down=_points_down,
    gaps=st.lists(_positive_gaps, min_size=1, max_size=40),
)
def test_momentum_never_enters_while_above_threshold(
    reference: float, points_down: float, gaps: list[float]
):
    leg = _armed_momentum_leg(reference, points_down)
    threshold = reference - points_down
    # Every premium on this path sits strictly above the entry threshold.
    path = [threshold + gap for gap in gaps]
    assert all(px > threshold for px in path)
    assert not any(core.should_enter_momentum_leg(leg, premium=px) for px in path)


# ---------------------------------------------------------------------------
# Across a path: entered iff the LTP reaches R - N at some point in the window
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(reference=_reference, points_down=_points_down, path=_price_paths)
def test_momentum_entered_over_path_iff_threshold_reached(
    reference: float, points_down: float, path: list[float]
):
    leg = _armed_momentum_leg(reference, points_down)
    threshold = reference - points_down

    # Simulate the monitoring loop walking the price path, entering on the first
    # cycle whose LTP reaches the threshold (as the engine would).
    entered = False
    for px in path:
        if core.should_enter_momentum_leg(leg, premium=px):
            entered = True
            break

    # The leg is entered during the window iff some observed LTP reached R - N,
    # equivalently iff the minimum premium on the path is at or below R - N.
    assert entered == (min(path) <= threshold)


# ---------------------------------------------------------------------------
# A non-pending (already-entered) leg is never re-entered by the gate
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(reference=_reference, points_down=_points_down, premium=_premiums)
def test_momentum_gate_false_when_not_pending(
    reference: float, points_down: float, premium: float
):
    leg = _armed_momentum_leg(reference, points_down)
    # Once the engine has placed the leg it clears pending_momentum; the gate
    # must then never ask for another entry, even at/below the threshold.
    leg.pending_momentum = False
    assert core.should_enter_momentum_leg(leg, premium=premium) is False
