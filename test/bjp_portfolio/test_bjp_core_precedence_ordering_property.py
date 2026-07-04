"""Property-based test for risk-evaluation precedence ordering.

Implements Property 23 from the design: for any engine state in which two or
more risk conditions are simultaneously satisfiable, ``evaluate_risk_cycle``
acts on the *earliest* condition in the strict precedence order

    momentum entry -> leg stop loss -> leg trailing stop
        -> overall stop loss -> overall trailing stop

(Req 14.7). The test constructs an engine in which each of the five categories
can be turned on or off independently, then asserts the returned decision always
matches the earliest satisfied category (or ``None`` when none is satisfied).

Each category is made independently controllable:
    * momentum entry — a single pending (unopened) momentum leg whose premium is
      driven below / left above its ``PointsDown`` threshold;
    * leg stop loss — an open leg carrying only a ``Points`` stop loss;
    * leg trailing stop — a distinct open leg carrying only a trailing stop with
      a pre-set ``trail_level``;
    * overall stop loss / overall trailing stop — configured (or omitted) on the
      strategy config, with the signed aggregate MTM supplied explicitly so both
      portfolio-wide levels are decisively breached when present.

The three legs are inserted in a generated order to demonstrate that *category*
precedence is absolute and independent of the configured leg order.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 23: Risk evaluation respects
# precedence order. For any engine state in which two or more risk conditions
# are simultaneously satisfiable, evaluate_risk_cycle returns a decision for the
# earliest satisfied condition in the order momentum entry -> leg stop loss ->
# leg trailing stop -> overall stop loss -> overall trailing stop.
# Validates: Requirements 14.7.

# Positive base premiums / thresholds spanning a realistic option-premium range.
_prices = st.floats(min_value=20.0, max_value=1000.0, allow_nan=False, allow_infinity=False)
# Strictly positive offsets (PointsDown N, leg-SL points P).
_offsets = st.floats(min_value=1.0, max_value=200.0, allow_nan=False, allow_infinity=False)
# Overall stop-loss magnitude L (rupees, positive) and signed locked trail level.
_levels = st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False)
_locked = st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False)
# A strictly positive margin keeps every condition clear of its boundary so the
# on/off state is unambiguous regardless of inclusive-vs-exclusive thresholds.
_margins = st.floats(min_value=1.0, max_value=50.0, allow_nan=False, allow_infinity=False)


def _engine(
    *,
    overall_sl: core.OverallStopLoss | None,
    overall_trail: core.OverallTrailSL | None,
) -> core.EngineState:
    """Build a NIFTY ``EngineState`` carrying the portfolio-wide risk config."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:25:00",
        overall_stop_loss=overall_sl,
        overall_trail_sl=overall_trail,
    )
    return core.EngineState(config=config, client=object())


@settings(max_examples=100)
@given(
    want_momentum=st.booleans(),
    want_leg_sl=st.booleans(),
    want_leg_trail=st.booleans(),
    want_overall_sl=st.booleans(),
    want_overall_trail=st.booleans(),
    ref=_prices,
    n_down=_offsets,
    entry=_prices,
    sl_points=_offsets,
    trail_level=_prices,
    level_l=_levels,
    locked=_locked,
    margin=_margins,
    order=st.permutations([0, 1, 2]),
)
def test_earliest_satisfied_condition_wins(
    want_momentum: bool,
    want_leg_sl: bool,
    want_leg_trail: bool,
    want_overall_sl: bool,
    want_overall_trail: bool,
    ref: float,
    n_down: float,
    entry: float,
    sl_points: float,
    trail_level: float,
    level_l: float,
    locked: float,
    margin: float,
    order: list[int],
) -> None:
    # --- Momentum-entry leg (pending, unopened): satisfied iff premium has
    #     fallen to/below ref - N. A margin keeps it clear of the boundary.
    momentum_threshold = ref - n_down
    momentum_leg = core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="ATM",
            momentum=core.LegMomentum(points_down=n_down),
        ),
        is_open=False,
        entry_fill=None,
        last_ltp=(momentum_threshold - margin) if want_momentum else (momentum_threshold + margin),
    )
    momentum_leg.momentum_ref = ref
    momentum_leg.pending_momentum = True

    # --- Leg stop-loss leg (open, Points SL only): triggered iff premium has
    #     risen to/above entry + P.
    sl_threshold = entry + sl_points
    sl_leg = core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="OTM1",
            stop_loss=core.LegStopLoss(core.SLKind.POINTS, sl_points),
        ),
        is_open=True,
        entry_fill=entry,
        last_ltp=(sl_threshold + margin) if want_leg_sl else (sl_threshold - margin),
    )

    # --- Leg trailing-stop leg (open, trail only): breached iff premium has
    #     risen to/above the pre-set trail_level.
    trail_leg = core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.PE,
            action=core.Action.SELL,
            offset="OTM1",
            trail_sl=core.LegTrailSL(instrument_move=10.0, stoploss_move=5.0),
        ),
        is_open=True,
        entry_fill=trail_level,
        last_ltp=(trail_level + margin) if want_leg_trail else (trail_level - margin),
    )
    trail_leg.trail_level = trail_level

    # --- Portfolio-wide conditions: configure only the ones we want on, and
    #     drive the signed MTM below every configured level so each is breached.
    overall_sl = core.OverallStopLoss(mtm_rupees=level_l) if want_overall_sl else None
    overall_trail = (
        core.OverallTrailSL(instrument_move=100.0, stoploss_move=50.0)
        if want_overall_trail
        else None
    )

    bounds: list[float] = []
    if want_overall_sl:
        bounds.append(-level_l)
    if want_overall_trail:
        bounds.append(locked)
    mtm = (min(bounds) - margin) if bounds else 0.0

    engine = _engine(overall_sl=overall_sl, overall_trail=overall_trail)
    if want_overall_trail:
        engine.locked_mtm_stop = locked

    legs = [momentum_leg, sl_leg, trail_leg]
    engine.legs = [legs[i] for i in order]

    # Expected: earliest satisfied category in the strict precedence order.
    ordered = [
        (core.RiskAction.MOMENTUM_ENTRY, want_momentum, momentum_leg),
        (core.RiskAction.LEG_STOP_LOSS, want_leg_sl, sl_leg),
        (core.RiskAction.LEG_TRAIL, want_leg_trail, trail_leg),
        (core.RiskAction.OVERALL_STOP_LOSS, want_overall_sl, None),
        (core.RiskAction.OVERALL_TRAIL, want_overall_trail, None),
    ]
    expected = next(((action, leg) for action, on, leg in ordered if on), None)

    decision = core.evaluate_risk_cycle(engine, mtm=mtm)

    if expected is None:
        assert decision is None
        return

    expected_action, expected_leg = expected
    assert decision is not None
    assert decision.action is expected_action
    assert decision.leg_state is expected_leg
