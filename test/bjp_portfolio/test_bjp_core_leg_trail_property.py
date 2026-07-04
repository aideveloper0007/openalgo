"""Property-based test for leg trailing-stop monotonicity and stepping.

Implements Property 16 from the design: for any declining price path of a
profitable short (SELL) leg, the trailing stop level is non-increasing over
time (never loosened, Req 11.3), the number of downward advances equals the
number of complete ``I``-point falls below the last advance reference, the
trailing level equals its initial level minus ``advances * S`` (Req 11.2), and
when a base stop loss also applies the effective stop equals the more
protective (lower) of the base and trailing levels (Req 11.5).

The ratchet is path-independent for a monotone non-increasing premium path:
because the reference only ever lands on ``entry_fill - k*I`` for integer ``k``
and moves strictly downward, the total advances after processing such a path
depend only on the lowest (final) premium seen:

    advances == floor((entry_fill - final_premium) / I)   when that is positive

Integer-valued inputs are generated so the ``floor`` arithmetic is exact and
free of floating-point rounding, while the boundary paths required by the
design (zero-fall: premium at/above the reference; large-fall: premium far
below, producing many steps) are covered by both the generator ranges and
explicit ``@example`` cases.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 16: Leg trailing stop is monotone
# and correctly stepped. For any declining premium path of a profitable short
# leg with a Points LegTrailSL(I, S), the trail level is non-increasing over
# the path, the number of downward advances equals the count of complete
# I-point falls below the last advance reference, the trail level equals its
# initial level minus (advances * S), and when a base LegStopLoss also applies
# the effective stop equals the more protective (lower) of the base and trail
# levels. Zero-fall (premium >= reference) and large-fall (many steps) paths
# are both exercised.
# Validates: Requirements 11.2, 11.3, 11.5.


def _short_trail_leg(
    entry_fill: int,
    instrument_move: int,
    stoploss_move: int,
    points: int | None,
) -> core.LegState:
    """Build an open short (SELL) ``LegState`` with a Points trailing stop.

    A base ``Points`` ``LegStopLoss`` of ``points`` is attached when ``points``
    is not ``None`` (so its base level is ``entry_fill + points``); otherwise
    the leg has only the trailing stop.
    """
    stop_loss = (
        core.LegStopLoss(kind=core.SLKind.POINTS, value=points)
        if points is not None
        else None
    )
    return core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="ATM",
            stop_loss=stop_loss,
            trail_sl=core.LegTrailSL(
                instrument_move=instrument_move,
                stoploss_move=stoploss_move,
            ),
        ),
        entry_fill=entry_fill,
        is_open=True,
    )


@st.composite
def _trail_scenarios(draw):
    """Generate ``(entry, I, S, P, premiums)`` for a declining premium path.

    ``P`` is ``None`` for ~half of cases (no base SL). ``premiums`` is a
    non-increasing (declining) path; its range extends above ``entry`` (to hit
    zero-fall leading cycles) and down to ``0`` (to hit large falls / many
    steps). ``0 < S <= I`` and ``I > 0`` per Req 11.1.
    """
    entry = draw(st.integers(min_value=50, max_value=1_000))
    instrument_move = draw(st.integers(min_value=1, max_value=100))
    stoploss_move = draw(st.integers(min_value=1, max_value=instrument_move))
    points = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=500)))
    # Premiums range above the entry reference (zero-fall) down to 0 (large fall).
    premiums = draw(
        st.lists(
            st.integers(min_value=0, max_value=entry + 50),
            min_size=1,
            max_size=12,
        )
    )
    premiums.sort(reverse=True)  # declining path: final element is the minimum
    return entry, instrument_move, stoploss_move, points, premiums


def _expected_advances(entry: int, instrument_move: int, final_premium: int) -> int:
    """Total complete ``I``-point falls of the lowest premium below ``entry``."""
    fall = entry - final_premium
    return fall // instrument_move if fall > 0 else 0


# ---------------------------------------------------------------------------
# Monotonicity + stepping + min-selection
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(scenario=_trail_scenarios())
# Zero-fall: every premium sits at or above the entry reference -> no advance.
@example(scenario=(100, 10, 5, 15, [130, 120, 110, 100]))
# Large fall: premium collapses to zero in one cycle -> many advances.
@example(scenario=(1_000, 1, 1, 0, [0]))
# Single premium exactly on a complete-step boundary.
@example(scenario=(100, 10, 4, None, [70]))
def test_leg_trail_is_monotone_and_correctly_stepped(scenario):
    entry, instrument_move, stoploss_move, points, premiums = scenario

    leg = _short_trail_leg(entry, instrument_move, stoploss_move, points)
    core.init_leg_trail(leg)

    initial_level = leg.trail_level
    # init sets the trail level to the base SL level (entry + P) or entry fill,
    # and the reference to the entry fill (Req 11.1).
    expected_initial_level = entry + points if points is not None else entry
    assert initial_level == expected_initial_level
    assert leg.trail_ref == entry

    # Monotonicity (Req 11.3): the level never rises across the declining path.
    prev_level = initial_level
    for premium in premiums:
        core.update_leg_trail(leg, premium=premium)
        assert leg.trail_level <= prev_level
        prev_level = leg.trail_level

    advances = _expected_advances(entry, instrument_move, premiums[-1])

    # Stepping (Req 11.2): reference advances down by advances * I and the trail
    # level drops by advances * S from its initial level.
    assert leg.trail_ref == entry - advances * instrument_move
    assert leg.trail_level == initial_level - advances * stoploss_move

    # Min-selection (Req 11.5): with a base SL, the effective stop is the lower
    # (more protective) of base and trail; with no base SL it is the trail.
    effective = core.effective_leg_stop_level(leg)
    if points is not None:
        base_level = entry + points
        assert effective == min(base_level, leg.trail_level)
    else:
        assert effective == leg.trail_level


# ---------------------------------------------------------------------------
# Focused min-selection: base SL vs advancing trail
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(scenario=_trail_scenarios())
def test_effective_stop_is_more_protective_of_base_and_trail(scenario):
    entry, instrument_move, stoploss_move, _points, premiums = scenario
    # Force a base SL so both levels are always defined.
    points = entry // 10  # non-negative Points base SL

    leg = _short_trail_leg(entry, instrument_move, stoploss_move, points)
    core.init_leg_trail(leg)
    for premium in premiums:
        core.update_leg_trail(leg, premium=premium)

    base_level = entry + points
    assert core.effective_leg_stop_level(leg) == min(base_level, leg.trail_level)
    # The trail starts at the base level and only ratchets down, so it is never
    # less protective than the base SL.
    assert leg.trail_level <= base_level
