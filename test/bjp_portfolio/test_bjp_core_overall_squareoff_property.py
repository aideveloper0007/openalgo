"""Property-based test for the overall (portfolio-level) square-off trigger.

Implements Property 21 from the design: an overall square-off of all open legs
is triggered *if and only if* the aggregate MTM loss reaches or exceeds the
configured Overall_Stop_Loss level ``L`` (signed MTM ``<= -L``, Req 13.1), or
the aggregate MTM falls to or below the locked Overall_Trail_SL level (Req
13.3). Both boundaries are inclusive, so an MTM sitting exactly at ``-L`` (or
exactly at the locked level) must trigger.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 21: Overall square-off triggers
# when MTM breaches the active level. For any aggregate MTM value, the overall
# square-off predicate is True iff the signed MTM is <= -L (the Overall_Stop_Loss
# level, inclusive) OR <= the locked Overall_Trail_SL level (inclusive).
# Validates: Requirements 13.1, 13.3.

# Finite, wide MTM range spanning deep loss through deep profit.
_mtm = st.floats(
    min_value=-1_000_000.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Overall stop-loss level L in rupees (positive loss magnitude).
_levels = st.floats(
    min_value=0.0,
    max_value=500_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Signed locked trailing level (may be negative, zero, or positive).
_locked = st.floats(
    min_value=-500_000.0,
    max_value=500_000.0,
    allow_nan=False,
    allow_infinity=False,
)


def _engine(
    overall_sl: core.OverallStopLoss | None = None,
    overall_trail: core.OverallTrailSL | None = None,
) -> core.EngineState:
    """Build a minimal ``EngineState`` carrying the overall-risk config."""
    config = core.StrategyConfig(
        strategy_name="test",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:26:00",
        legs=[],
        overall_stop_loss=overall_sl,
        overall_trail_sl=overall_trail,
    )
    return core.EngineState(config=config, client=object(), legs=[])


@settings(max_examples=100)
@given(level=_levels, mtm=_mtm)
def test_overall_stop_loss_triggers_iff_mtm_at_or_below_minus_L(
    level: float, mtm: float
):
    # Req 13.1: square off when aggregate MTM loss >= L, i.e. signed mtm <= -L.
    engine = _engine(overall_sl=core.OverallStopLoss(mtm_rupees=level))
    triggered = core.overall_stop_loss_triggered(engine, mtm=mtm)
    assert triggered == (mtm <= -level)


@settings(max_examples=100)
@given(level=_levels)
def test_overall_stop_loss_triggers_exactly_at_L(level: float):
    # Boundary: MTM sitting exactly at the loss threshold (-L) must trigger,
    # while any value just above -L must not.
    engine = _engine(overall_sl=core.OverallStopLoss(mtm_rupees=level))
    assert core.overall_stop_loss_triggered(engine, mtm=-level) is True
    assert core.overall_stop_loss_triggered(engine, mtm=-level + 1.0) is False


@settings(max_examples=100)
@given(locked=_locked, mtm=_mtm)
def test_overall_trail_triggers_iff_mtm_at_or_below_locked(
    locked: float, mtm: float
):
    # Req 13.3: square off when aggregate MTM falls to or below the locked level.
    engine = _engine(overall_trail=core.OverallTrailSL(
        instrument_move=100.0, stoploss_move=50.0
    ))
    engine.locked_mtm_stop = locked
    triggered = core.overall_trail_triggered(engine, mtm=mtm)
    assert triggered == (mtm <= locked)


@settings(max_examples=100)
@given(locked=_locked)
def test_overall_trail_triggers_exactly_at_locked_level(locked: float):
    # Boundary: MTM exactly at the locked level triggers (inclusive breach).
    engine = _engine(overall_trail=core.OverallTrailSL(
        instrument_move=100.0, stoploss_move=50.0
    ))
    engine.locked_mtm_stop = locked
    assert core.overall_trail_triggered(engine, mtm=locked) is True


@settings(max_examples=100)
@given(level=_levels, locked=_locked, mtm=_mtm)
def test_overall_square_off_iff_either_level_breached(
    level: float, locked: float, mtm: float
):
    # Property 21 (combined): an overall square-off happens iff the stop-loss
    # level OR the locked trailing level is breached.
    engine = _engine(
        overall_sl=core.OverallStopLoss(mtm_rupees=level),
        overall_trail=core.OverallTrailSL(instrument_move=100.0, stoploss_move=50.0),
    )
    engine.locked_mtm_stop = locked

    sl = core.overall_stop_loss_triggered(engine, mtm=mtm)
    trail = core.overall_trail_triggered(engine, mtm=mtm)
    square_off = sl or trail

    assert square_off == (mtm <= -level or mtm <= locked)


def test_no_overall_stop_loss_never_triggers():
    # With no Overall_Stop_Loss configured the SL predicate is always False.
    engine = _engine(overall_sl=None)
    assert core.overall_stop_loss_triggered(engine, mtm=-1_000_000.0) is False


def test_uninitialized_trail_lock_never_triggers():
    # A configured trail whose lock has not yet been initialized does not
    # trigger (fail-safe: do not exit on missing data).
    engine = _engine(overall_trail=core.OverallTrailSL(
        instrument_move=100.0, stoploss_move=50.0
    ))
    assert engine.locked_mtm_stop is None
    assert core.overall_trail_triggered(engine, mtm=-1_000_000.0) is False


def test_no_overall_trail_never_triggers():
    # With no Overall_Trail_SL configured the trail predicate is always False,
    # even if a locked level is somehow present.
    engine = _engine(overall_trail=None)
    engine.locked_mtm_stop = 0.0
    assert core.overall_trail_triggered(engine, mtm=-1_000_000.0) is False
