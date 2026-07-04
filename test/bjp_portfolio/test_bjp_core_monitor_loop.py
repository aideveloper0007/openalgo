"""Unit tests for the monitoring-loop mechanics (Task 18.1).

Covers ``monitor`` and its helpers in ``strategies/bjp_portfolio/bjp_core.py``:
the fixed-cadence loop that, while any leg is open, fetches LTP for every
monitored instrument, advances the trailing ratchets, and invokes the
precedence dispatcher each cycle (Req 14.1, 14.2, 14.7).

These are targeted example/unit tests focused on the loop's *testable
mechanics* — the injectable stop condition and sleep, the LTP-response parsing,
the open-leg/stale-data predicates, and the shared per-cycle evaluation. The
requirement-linked mocked-SDK resilience and WebSocket scenarios (Req 13.6,
14.4-14.6) are covered by the integration tests in Tasks 18.2 and 18.3.

Production code under test is imported as ``bjp_core`` via the ``sys.path``
setup in ``conftest.py``.
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
    symbol: str | None = "NIFTY31DEC2524000CE",
    is_open: bool = True,
    entry_fill: float | None = 100.0,
    last_ltp: float | None = None,
    stop_loss: core.LegStopLoss | None = None,
) -> core.LegState:
    """Build a short CE leg in a chosen runtime state."""
    leg_config = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset="ITM2",
        stop_loss=stop_loss,
    )
    return core.LegState(
        config=leg_config,
        symbol=symbol,
        entry_fill=entry_fill,
        is_open=is_open,
        last_ltp=last_ltp,
    )


# -- LTP response parsing ---------------------------------------------------


def test_extract_quote_ltp_success() -> None:
    """A success envelope with a numeric LTP is parsed to a float."""
    resp = {"status": "success", "data": {"ltp": 123.5}}
    assert core._extract_quote_ltp(resp) == 123.5


def test_extract_quote_ltp_rejects_bad_shapes() -> None:
    """Non-success, missing data, or non-numeric LTP yields ``None``."""
    assert core._extract_quote_ltp({"status": "error"}) is None
    assert core._extract_quote_ltp({"status": "success"}) is None
    assert core._extract_quote_ltp({"status": "success", "data": {}}) is None
    assert (
        core._extract_quote_ltp({"status": "success", "data": {"ltp": "x"}}) is None
    )
    assert (
        core._extract_quote_ltp({"status": "success", "data": {"ltp": True}}) is None
    )
    assert core._extract_quote_ltp("not-a-dict") is None


# -- open-leg / stale-data predicates --------------------------------------


def test_has_open_legs_counts_open_and_pending() -> None:
    """An open leg or a pending-momentum leg both keep the loop alive."""
    engine = _engine()
    engine.legs = [_short_ce(is_open=False)]
    assert core._has_open_legs(engine) is False

    engine.legs[0].is_open = True
    assert core._has_open_legs(engine) is True

    engine.legs[0].is_open = False
    engine.legs[0].pending_momentum = True
    assert core._has_open_legs(engine) is True


def test_has_stale_open_leg_when_open_leg_lacks_ltp() -> None:
    """An open leg with no known LTP marks the cycle stale (Req 13.6)."""
    engine = _engine()
    engine.legs = [_short_ce(last_ltp=None)]
    assert core._has_stale_open_leg(engine) is True

    engine.legs[0].last_ltp = 90.0
    assert core._has_stale_open_leg(engine) is False


# -- shared per-cycle evaluation -------------------------------------------


def test_advance_and_evaluate_skips_overall_when_stale() -> None:
    """Stale data skips the overall checks but still runs per-leg checks."""
    engine = _engine(overall_stop_loss=core.OverallStopLoss(mtm_rupees=1.0))
    # One open leg has an LTP that trips its leg stop loss; another open leg has
    # no LTP, so the aggregate-MTM (overall) checks must be skipped this cycle.
    sl_leg = _short_ce(
        symbol="NIFTY31DEC2524000CE",
        last_ltp=200.0,
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
    )
    stale_leg = _short_ce(symbol="NIFTY31DEC2524100CE", last_ltp=None)
    engine.legs = [sl_leg, stale_leg]

    decision = core._advance_and_evaluate(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.LEG_STOP_LOSS
    assert decision.leg_state is sl_leg


def test_advance_and_evaluate_uses_aggregate_mtm_when_fresh() -> None:
    """With fresh data the overall stop loss fires on an MTM breach (Req 13.1)."""
    engine = _engine(overall_stop_loss=core.OverallStopLoss(mtm_rupees=100.0))
    # Short leg: (entry - ltp) * qty = (100 - 120) * 65 = -1300 <= -100 -> breach.
    engine.legs = [_short_ce(last_ltp=120.0)]

    decision = core._advance_and_evaluate(engine)

    assert decision is not None
    assert decision.action is core.RiskAction.OVERALL_STOP_LOSS


# -- loop control (injectable stop / sleep) --------------------------------


def test_monitor_stops_on_should_continue() -> None:
    """The injected stop condition bounds the loop so it never runs forever."""
    engine = _engine()
    engine.legs = [_short_ce(last_ltp=90.0)]

    cycles = {"n": 0}

    def should_continue() -> bool:
        cycles["n"] += 1
        return cycles["n"] <= 3

    sleeps: list[float] = []

    core.monitor(
        engine,
        sleep=sleeps.append,
        should_continue=should_continue,
        fetch=lambda symbol, exchange: 90.0,
    )

    # should_continue is consulted at the top of each cycle; the 4th call stops
    # the loop, so exactly 3 evaluation cycles ran.
    assert cycles["n"] == 4
    assert len(sleeps) == 3
    assert all(interval == core.DEFAULT_MONITORING_INTERVAL for interval in sleeps)


def test_monitor_stops_when_no_open_legs() -> None:
    """With no live legs the loop exits immediately without sleeping."""
    engine = _engine()
    engine.legs = [_short_ce(is_open=False)]

    sleeps: list[float] = []
    calls: list[core.RiskDecision] = []

    core.monitor(
        engine,
        sleep=sleeps.append,
        fetch=lambda symbol, exchange: 90.0,
        handler=lambda es, decision: calls.append(decision),
    )

    assert sleeps == []
    assert calls == []


def test_monitor_dispatches_decision_to_handler() -> None:
    """Each fired risk decision is handed to the injected handler."""
    engine = _engine()
    engine.legs = [
        _short_ce(last_ltp=200.0, stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0))
    ]

    decisions: list[core.RiskDecision] = []

    def should_continue() -> bool:
        return len(decisions) == 0  # stop after the first decision is captured

    core.monitor(
        engine,
        sleep=lambda _seconds: None,
        should_continue=should_continue,
        fetch=lambda symbol, exchange: 200.0,
        handler=lambda es, decision: decisions.append(decision),
    )

    assert len(decisions) == 1
    assert decisions[0].action is core.RiskAction.LEG_STOP_LOSS
