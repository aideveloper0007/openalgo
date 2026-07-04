"""Property-based test for overall trailing-lock monotonicity and stepping.

Implements Property 22 from the design: for any path of aggregate MTM values,
the locked overall-trailing stop is initialized at the Overall_Stop_Loss level
(``-L`` in the signed-MTM convention, or ``0.0`` when no overall SL is
configured), is non-decreasing over the path (never loosened, Req 13.2), and is
raised by (number of complete ``I``-point improvements above the prior peak
MTM) times ``S``.

The ratchet is path-independent for the *maximum* MTM seen: because the peak
reference starts at ``0.0`` and only ever advances upward by whole ``I``
increments (retaining the sub-``I`` remainder), the total number of advances
after processing an arbitrary MTM path depends only on the highest MTM value
observed:

    steps == floor(max(0, highest_mtm) / I)

so the locked level ends at ``initial_locked + steps * S`` and the peak
reference at ``steps * I``. Integer-valued inputs are generated so the
``floor`` arithmetic is exact and free of floating-point rounding, while the
boundary paths required by the design (no-improvement: MTM stays at/below the
reference; large-improvement: MTM jumps far above, producing many steps) are
covered by both the generator ranges and explicit ``@example`` cases.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 22: Overall trailing lock is
# monotone and correctly stepped. For any path of aggregate MTM values, the
# locked overall-trailing stop is initialized at the Overall_Stop_Loss level,
# is non-decreasing over time, and is raised by (number of complete I-point
# improvements above the prior peak MTM) * S. No-improvement (MTM never rises a
# full I above the reference) and large-improvement (many steps in one cycle)
# paths are both exercised.
# Validates: Requirements 13.2.


def _engine(
    instrument_move: int,
    stoploss_move: int,
    overall_sl: int | None,
) -> core.EngineState:
    """Build an ``EngineState`` with an overall trail (and optional overall SL).

    ``overall_sl`` is the Overall_Stop_Loss ``L`` in rupees, or ``None`` for a
    strategy that trails without a configured overall SL. No client is needed
    because every ratchet call passes the aggregate MTM explicitly.
    """
    overall_stop_loss = (
        core.OverallStopLoss(mtm_rupees=overall_sl) if overall_sl is not None else None
    )
    config = core.StrategyConfig(
        strategy_name="TRAIL_TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:16:00",
        exit_time="15:29:00",
        legs=[],
        overall_stop_loss=overall_stop_loss,
        overall_trail_sl=core.OverallTrailSL(
            instrument_move=instrument_move,
            stoploss_move=stoploss_move,
        ),
    )
    return core.EngineState(config=config, client=None)


@st.composite
def _trail_scenarios(draw):
    """Generate ``(I, S, L, mtm_path)`` for an aggregate-MTM path.

    ``L`` is ``None`` for ~half of cases (no overall SL). ``mtm_path`` ranges
    from below the initial reference (no-improvement leading cycles) up to well
    above it (large improvements / many steps) and may rise and fall in any
    order. ``0 < S`` and ``I > 0`` per the ratchet contract.
    """
    instrument_move = draw(st.integers(min_value=1, max_value=5_000))
    stoploss_move = draw(st.integers(min_value=1, max_value=instrument_move))
    overall_sl = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)))
    mtm_path = draw(
        st.lists(
            st.integers(min_value=-20_000, max_value=50_000),
            min_size=1,
            max_size=12,
        )
    )
    return instrument_move, stoploss_move, overall_sl, mtm_path


def _expected_steps(highest_mtm: int, instrument_move: int) -> int:
    """Total complete ``I``-improvements of the highest MTM above ``0``."""
    return highest_mtm // instrument_move if highest_mtm > 0 else 0


# ---------------------------------------------------------------------------
# Monotonicity + stepping + initialization
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(scenario=_trail_scenarios())
# No improvement: MTM stays at or below the reference (0) -> no advance.
@example(scenario=(100, 50, 1_750, [-500, -1_000, -200, 0]))
# Large improvement in one cycle -> many steps at once.
@example(scenario=(1, 1, 5_000, [50_000]))
# Improvement exactly on a complete-step boundary with no overall SL.
@example(scenario=(2_000, 2_000, None, [6_000]))
def test_overall_trail_is_monotone_and_correctly_stepped(scenario):
    instrument_move, stoploss_move, overall_sl, mtm_path = scenario

    engine = _engine(instrument_move, stoploss_move, overall_sl)

    # Before any update the lock is uninitialized.
    assert engine.locked_mtm_stop is None
    assert engine.peak_mtm == 0.0

    # Initialization (Req 13.2): the first update locks in at the overall-SL
    # level (-L), or at 0.0 (breakeven) when no overall SL is configured.
    expected_initial = -float(overall_sl) if overall_sl is not None else 0.0

    prev_locked: float | None = None
    prev_peak = 0.0
    for mtm in mtm_path:
        core.update_overall_trail(engine, mtm=mtm)
        # Monotonicity (Req 13.2): neither the lock nor the peak reference ever
        # decreases across the path.
        assert engine.locked_mtm_stop is not None
        if prev_locked is not None:
            assert engine.locked_mtm_stop >= prev_locked
        assert engine.peak_mtm >= prev_peak
        prev_locked = engine.locked_mtm_stop
        prev_peak = engine.peak_mtm

    highest = max(mtm_path)
    steps = _expected_steps(highest, instrument_move)

    # Stepping (Req 13.2): the peak reference advances by steps * I from 0 and
    # the locked level rises by steps * S from its initial (-L) level.
    assert engine.peak_mtm == steps * instrument_move
    assert engine.locked_mtm_stop == expected_initial + steps * stoploss_move


# ---------------------------------------------------------------------------
# Focused: strictly increasing path advances on the running maximum
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(scenario=_trail_scenarios())
def test_overall_trail_advances_track_running_maximum(scenario):
    instrument_move, stoploss_move, overall_sl, mtm_path = scenario

    engine = _engine(instrument_move, stoploss_move, overall_sl)
    expected_initial = -float(overall_sl) if overall_sl is not None else 0.0

    # Feed the sorted-ascending path so each cycle is a fresh running maximum;
    # the end state must match the closed-form based on the highest MTM.
    for mtm in sorted(mtm_path):
        core.update_overall_trail(engine, mtm=mtm)

    highest = max(mtm_path)
    steps = _expected_steps(highest, instrument_move)
    assert engine.peak_mtm == steps * instrument_move
    assert engine.locked_mtm_stop == expected_initial + steps * stoploss_move
    # The lock is never below where it started (never loosened).
    assert engine.locked_mtm_stop >= expected_initial
