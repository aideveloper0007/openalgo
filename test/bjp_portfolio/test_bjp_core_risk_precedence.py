"""Example-based unit tests for the risk-evaluation precedence dispatcher.

Covers ``evaluate_risk_cycle`` (Task 13.1): each monitoring cycle evaluates the
five risk conditions in the strict precedence order
momentum entry -> leg stop loss -> leg trailing stop -> overall stop loss ->
overall trailing stop, and acts on the earliest satisfied condition (Req 14.7).

These are targeted example tests; the universal precedence property across
generated multi-condition states is covered by the Property 23 test (Task 13.2).
They assert that:

    * each single satisfied condition yields the matching decision;
    * when several conditions are simultaneously satisfiable the earliest in the
      precedence order is returned;
    * category precedence is absolute across legs (a leg stop loss on one leg
      outranks a leg trail on another);
    * only open legs fire the leg stop-loss / leg-trail categories;
    * ``evaluate_overall=False`` skips the two portfolio-wide conditions; and
    * a quiet cycle returns ``None``.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 14.7.
"""

from __future__ import annotations

import bjp_core as core


def _engine(**overrides) -> core.EngineState:
    """Build a NIFTY ``EngineState`` with optional config overrides."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:25:00",
        **overrides,
    )
    return core.EngineState(config=config, client=object())


def _short_ce(
    *,
    offset: str = "ITM2",
    stop_loss: core.LegStopLoss | None = None,
    trail_sl: core.LegTrailSL | None = None,
    momentum: core.LegMomentum | None = None,
    is_open: bool = True,
    entry_fill: float | None = 100.0,
    last_ltp: float | None = None,
) -> core.LegState:
    """Build a short CE leg in a chosen runtime state."""
    leg_config = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset=offset,
        stop_loss=stop_loss,
        trail_sl=trail_sl,
        momentum=momentum,
    )
    return core.LegState(
        config=leg_config,
        symbol="NIFTY31DEC2524000CE",
        entry_fill=entry_fill,
        is_open=is_open,
        last_ltp=last_ltp,
    )


def test_quiet_cycle_returns_none() -> None:
    """No satisfied condition yields no decision."""
    leg = _short_ce(
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
        last_ltp=100.0,  # well below the entry+15 threshold
    )
    engine = _engine()
    engine.legs = [leg]

    assert core.evaluate_risk_cycle(engine) is None


def test_leg_stop_loss_alone_is_detected() -> None:
    """A triggered leg stop loss yields a LEG_STOP_LOSS decision for that leg."""
    leg = _short_ce(
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
        last_ltp=120.0,  # >= 100 + 15
    )
    engine = _engine()
    engine.legs = [leg]

    decision = core.evaluate_risk_cycle(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.LEG_STOP_LOSS
    assert decision.leg_state is leg


def test_momentum_entry_outranks_leg_stop_loss() -> None:
    """A pending momentum entry outranks a simultaneously triggered leg SL."""
    # Momentum leg pending and satisfied (LTP fell N points below the reference).
    momentum_leg = _short_ce(
        offset="ITM1",
        momentum=core.LegMomentum(points_down=10.0),
        is_open=False,
        entry_fill=None,
        last_ltp=90.0,
    )
    momentum_leg.momentum_ref = 100.0
    momentum_leg.pending_momentum = True

    # Open leg whose stop loss is also triggered this cycle.
    sl_leg = _short_ce(
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
        last_ltp=120.0,
    )

    engine = _engine()
    engine.legs = [sl_leg, momentum_leg]  # SL leg first to prove ordering by category

    decision = core.evaluate_risk_cycle(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.MOMENTUM_ENTRY
    assert decision.leg_state is momentum_leg


def test_leg_stop_loss_outranks_leg_trail_across_legs() -> None:
    """A leg SL on one leg outranks a leg trail breached on another leg."""
    # Leg A: trailing stop breached.
    trail_leg = _short_ce(offset="ITM2", trail_sl=core.LegTrailSL(10.0, 5.0))
    trail_leg.trail_level = 95.0
    trail_leg.last_ltp = 100.0  # >= trail_level -> trail triggered

    # Leg B: base stop loss triggered.
    sl_leg = _short_ce(
        offset="ITM1",
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
        last_ltp=120.0,
    )

    engine = _engine()
    engine.legs = [trail_leg, sl_leg]

    decision = core.evaluate_risk_cycle(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.LEG_STOP_LOSS
    assert decision.leg_state is sl_leg


def test_leg_trail_outranks_overall_stop_loss() -> None:
    """A breached leg trail outranks a simultaneously breached overall SL."""
    trail_leg = _short_ce(trail_sl=core.LegTrailSL(10.0, 5.0), entry_fill=100.0)
    trail_leg.trail_level = 95.0
    trail_leg.last_ltp = 130.0  # trail breached; also a big MTM loss

    engine = _engine(overall_stop_loss=core.OverallStopLoss(100.0))
    engine.legs = [trail_leg]

    # Short leg loss = (100 - 130) * 65 = -1950 -> overall SL (-100) also breached.
    decision = core.evaluate_risk_cycle(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.LEG_TRAIL
    assert decision.leg_state is trail_leg


def test_overall_stop_loss_outranks_overall_trail() -> None:
    """Overall SL is chosen over overall trail when both are breached."""
    leg = _short_ce(entry_fill=100.0, last_ltp=130.0)  # loss = -1950
    engine = _engine(
        overall_stop_loss=core.OverallStopLoss(100.0),
        overall_trail_sl=core.OverallTrailSL(1000.0, 1000.0),
    )
    engine.legs = [leg]
    # Initialize the trail lock at the overall-SL level (-100) so it is breached.
    core.update_overall_trail(engine)

    decision = core.evaluate_risk_cycle(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.OVERALL_STOP_LOSS
    assert decision.leg_state is None
    assert decision.mtm == -1950.0


def test_overall_trail_detected_when_no_overall_stop_loss() -> None:
    """With only an overall trail configured, its breach yields OVERALL_TRAIL."""
    leg = _short_ce(entry_fill=100.0, last_ltp=110.0)  # loss = -650
    engine = _engine(overall_trail_sl=core.OverallTrailSL(1000.0, 1000.0))
    engine.legs = [leg]
    # No overall SL -> trail lock initializes at breakeven (0.0); MTM -650 <= 0.
    core.update_overall_trail(engine)

    decision = core.evaluate_risk_cycle(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.OVERALL_TRAIL
    assert decision.mtm == -650.0


def test_closed_leg_does_not_fire_leg_conditions() -> None:
    """A leg already closed must not re-trigger its stop loss."""
    leg = _short_ce(
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
        last_ltp=120.0,
        is_open=False,
    )
    engine = _engine()
    engine.legs = [leg]

    assert core.evaluate_risk_cycle(engine) is None


def test_evaluate_overall_false_skips_portfolio_conditions() -> None:
    """evaluate_overall=False skips overall SL/trail but keeps per-leg checks."""
    leg = _short_ce(entry_fill=100.0, last_ltp=130.0)  # big loss -> overall SL
    engine = _engine(overall_stop_loss=core.OverallStopLoss(100.0))
    engine.legs = [leg]

    # Overall condition would fire, but stale-data skip suppresses it.
    assert core.evaluate_risk_cycle(engine, evaluate_overall=False) is None
    # Sanity: with overall evaluation enabled it does fire.
    fired = core.evaluate_risk_cycle(engine, evaluate_overall=True)
    assert fired is not None
    assert fired.action is core.RiskAction.OVERALL_STOP_LOSS


def test_underlying_points_leg_stop_loss_uses_spot() -> None:
    """UnderlyingPoints leg SL is driven by the supplied spot, not premium."""
    leg = _short_ce(
        stop_loss=core.LegStopLoss(core.SLKind.UNDERLYING_POINTS, 100.0),
        last_ltp=100.0,
    )
    leg.entry_spot = 24000.0
    engine = _engine()
    engine.legs = [leg]

    # Spot below the adverse level -> no trigger.
    assert core.evaluate_risk_cycle(engine, spot=24050.0) is None
    # Spot at/above entry_spot + U -> leg SL triggers.
    decision = core.evaluate_risk_cycle(engine, spot=24100.0)
    assert decision is not None
    assert decision.action is core.RiskAction.LEG_STOP_LOSS
    assert decision.leg_state is leg


def test_precedence_constant_matches_action_order() -> None:
    """RISK_PRECEDENCE lists the five actions in the documented order."""
    assert core.RISK_PRECEDENCE == (
        core.RiskAction.MOMENTUM_ENTRY,
        core.RiskAction.LEG_STOP_LOSS,
        core.RiskAction.LEG_TRAIL,
        core.RiskAction.OVERALL_STOP_LOSS,
        core.RiskAction.OVERALL_TRAIL,
    )
