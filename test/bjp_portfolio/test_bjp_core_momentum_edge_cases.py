"""Edge-case tests for momentum-gated entry boundaries (Task 11.3).

These example tests cover the two momentum boundary branches of the pure
evaluators in ``bjp_core`` that are not exercised by the momentum gating
property test:

* Req 9.4 - when no valid reference LTP is available at Entry_Time the leg's
  reference premium cannot be recorded: ``record_momentum_reference`` logs an
  error, leaves ``momentum_ref`` unset, marks the leg *not* pending, and returns
  ``False`` so the leg is never entered.
* Req 9.5 - when a momentum leg's ``PointsDown`` condition is still unmet at
  Exit_Time the leg is never entered (``momentum_pending_at_exit`` reports it as
  still pending), so the exit manager squares off the other open legs.

The production code under test lives in ``strategies/bjp_portfolio/
bjp_core.py`` and is imported as ``bjp_core`` via the sys.path setup in
``conftest.py``.
"""

from __future__ import annotations

import logging

import bjp_core as core


def _momentum_leg(points_down: float = 10.0) -> core.LegState:
    """Build a fresh short-CE momentum leg (PointsDown N) in its initial state."""
    config = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset="ITM1",
        momentum=core.LegMomentum(points_down=points_down),
    )
    return core.LegState(config=config)


def _plain_leg() -> core.LegState:
    """Build a fresh non-momentum short-PE leg (an already-entered immediate leg)."""
    config = core.LegConfig(
        option_type=core.OptionType.PE,
        action=core.Action.SELL,
        offset="ITM2",
    )
    return core.LegState(config=config, is_open=True)


# ---------------------------------------------------------------------------
# Req 9.4 - reference LTP unavailable at entry: no entry, error surfaced.
# ---------------------------------------------------------------------------


def test_reference_unavailable_returns_false_and_logs_error(caplog):
    leg = _momentum_leg()
    # No explicit LTP passed and no stored last_ltp -> reference cannot be set.
    assert leg.last_ltp is None

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        recorded = core.record_momentum_reference(leg, ltp=None)

    # Reference could not be recorded (Req 9.4).
    assert recorded is False
    # An error indicating the reference premium is unavailable is surfaced.
    assert any(
        "reference premium unavailable" in record.message
        for record in caplog.records
    )


def test_reference_unavailable_leaves_leg_unentered():
    leg = _momentum_leg()

    core.record_momentum_reference(leg, ltp=None)

    # momentum_ref stays unset and the leg is not marked pending, so the
    # monitoring loop will never place it (Req 9.4).
    assert leg.momentum_ref is None
    assert leg.pending_momentum is False
    # The entry threshold is undefined and the leg must not be entered even if a
    # very low premium later appears.
    assert core.momentum_entry_threshold(leg) is None
    assert core.should_enter_momentum_leg(leg, premium=0.0) is False


def test_reference_unavailable_leg_is_not_pending_at_exit():
    # A leg whose reference could not be recorded is never entered *and* is not
    # "pending at exit": it was never armed, so it does not itself trigger the
    # Req 9.5 square-off-others path.
    leg = _momentum_leg()

    core.record_momentum_reference(leg, ltp=None)

    assert core.momentum_pending_at_exit(leg) is False


# ---------------------------------------------------------------------------
# Req 9.5 - momentum unmet at Exit_Time: leg not entered, others squared off.
# ---------------------------------------------------------------------------


def test_momentum_unmet_leg_still_pending_at_exit():
    leg = _momentum_leg(points_down=10.0)

    # Reference recorded at Entry_Time (LTP 100 -> threshold 90).
    assert core.record_momentum_reference(leg, ltp=100.0) is True
    assert leg.pending_momentum is True
    assert core.momentum_entry_threshold(leg) == 90.0

    # Premium never falls to the 90 threshold within the window -> not entered.
    leg.last_ltp = 95.0
    assert core.should_enter_momentum_leg(leg, premium=95.0) is False

    # At Exit_Time the leg is still pending and unopened (Req 9.5): the exit
    # manager uses this to leave it un-entered and square off the other legs.
    assert core.momentum_pending_at_exit(leg) is True


def test_momentum_met_leg_not_pending_after_entry():
    # Contrast case: once a momentum leg is entered it is no longer pending, so
    # it is not reported as pending at exit.
    leg = _momentum_leg(points_down=10.0)
    core.record_momentum_reference(leg, ltp=100.0)

    # Premium reaches the threshold -> should enter.
    assert core.should_enter_momentum_leg(leg, premium=90.0) is True

    # Simulate the monitoring loop placing the leg: it is opened and no longer
    # pending.
    leg.pending_momentum = False
    leg.is_open = True

    assert core.momentum_pending_at_exit(leg) is False


def test_other_open_legs_are_squared_off_when_momentum_unmet():
    # Req 9.5: with the momentum leg still pending at exit, the *other* open
    # legs remain open (is_open True) and are therefore the ones the exit
    # manager squares off. This asserts the state the exit manager keys on.
    momentum_leg = _momentum_leg(points_down=10.0)
    core.record_momentum_reference(momentum_leg, ltp=100.0)
    momentum_leg.last_ltp = 99.0  # never fell to the 90 threshold

    other_leg = _plain_leg()

    # The momentum leg is pending (never entered); the other leg is open.
    assert core.momentum_pending_at_exit(momentum_leg) is True
    assert other_leg.is_open is True
    # The un-entered momentum leg is not itself an open position to square off.
    assert momentum_leg.is_open is False
