"""Example-based unit tests for Immediate re-entry re-application.

Covers the Immediate re-entry path of ``maybe_reenter`` (Task 12.5): after a
short leg is closed by its stop loss, an Immediate re-entry places a fresh
MARKET order at the prevailing market price (Req 12.1) and re-applies the leg's
stop loss and trailing stop to the re-entered position (Req 12.5).

These are targeted example tests (the bounded-count, AtCost-price, and
time-cutoff behaviors are covered by their own property tests). They assert,
against an in-memory fake OpenAlgo SDK client, that:

    * the re-entry order is a ``MARKET`` order on the index F&O exchange with
      the leg's original action and the strategy quantity (Req 12.1);
    * the leg adopts the prevailing market price as its new ``entry_fill`` when
      the SDK reports no average fill, and the SDK average fill otherwise;
    * the leg's base premium stop loss and trailing-stop ratchet are
      re-initialized against the fresh ``entry_fill`` (Req 12.5);
    * an ``UnderlyingPoints`` baseline is refreshed when the current spot is
      supplied; and
    * the runtime state is flipped back to open with the re-entry counter
      advanced and the prior exit reason cleared.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 12.1, 12.5.
"""

from __future__ import annotations

from datetime import time

import bjp_core as core

# A mid-session IST time comfortably before any re-entry cutoff, so the
# time-restriction guard never blocks these re-entries.
_MIDDAY = time(10, 0, 0)


def _engine(fake_client: object) -> core.EngineState:
    """Build an ``EngineState`` for a single-leg NIFTY strategy."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=2,
        entry_time="09:20:00",
        exit_time="15:25:00",
    )
    return core.EngineState(config=config, client=fake_client)


def _stopped_short_ce(
    entry_fill: float,
    *,
    stop_loss: core.LegStopLoss | None = None,
    trail_sl: core.LegTrailSL | None = None,
    reentries_done: int = 0,
    entry_spot: float | None = None,
) -> core.LegState:
    """Build a short CE leg that has just been closed by its stop loss."""
    leg_config = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset="OTM6",
        stop_loss=stop_loss,
        trail_sl=trail_sl,
        reentry=core.LegReentry(kind=core.ReentryKind.IMMEDIATE, count=3),
    )
    return core.LegState(
        config=leg_config,
        symbol="NIFTY31DEC2524000CE",
        order_id="orig-1",
        entry_fill=entry_fill,
        entry_spot=entry_spot,
        is_open=False,
        exit_reason="leg_stop_loss",
        reentries_done=reentries_done,
    )


def test_immediate_reentry_places_market_order(fake_client) -> None:
    """Immediate re-entry submits a MARKET order with the leg's action/qty (Req 12.1)."""
    engine = _engine(fake_client)
    leg = _stopped_short_ce(entry_fill=120.0)

    reentered = core.maybe_reenter(engine, leg, market_price=150.0, now=_MIDDAY)

    assert reentered is True
    order = fake_client.last_call("placeorder")
    assert order is not None
    assert order["price_type"] == "MARKET"
    assert order["action"] == "SELL"
    assert order["exchange"] == core.NIFTY.fno_exchange
    assert order["symbol"] == "NIFTY31DEC2524000CE"
    assert order["quantity"] == str(engine.config.quantity)


def test_immediate_reentry_adopts_market_price_when_no_avg_fill(fake_client) -> None:
    """With no reported average fill, the new entry_fill is the market price (Req 12.1)."""
    fake_client.orderstatus_response = {"status": "success", "data": {}}
    engine = _engine(fake_client)
    leg = _stopped_short_ce(entry_fill=120.0)

    reentered = core.maybe_reenter(engine, leg, market_price=150.0, now=_MIDDAY)

    assert reentered is True
    assert leg.entry_fill == 150.0


def test_immediate_reentry_adopts_reported_average_fill(fake_client) -> None:
    """When the SDK reports an average fill, the leg adopts it as entry_fill."""
    fake_client.orderstatus_response = {
        "status": "success",
        "data": {"average_price": 152.5},
    }
    engine = _engine(fake_client)
    leg = _stopped_short_ce(entry_fill=120.0)

    reentered = core.maybe_reenter(engine, leg, market_price=150.0, now=_MIDDAY)

    assert reentered is True
    assert leg.entry_fill == 152.5


def test_immediate_reentry_reapplies_base_sl_and_trail(fake_client) -> None:
    """Trail is re-initialized against the fresh fill using the base SL level (Req 12.5)."""
    fake_client.orderstatus_response = {"status": "success", "data": {}}
    engine = _engine(fake_client)
    leg = _stopped_short_ce(
        entry_fill=120.0,
        stop_loss=core.LegStopLoss(kind=core.SLKind.POINTS, value=20.0),
        trail_sl=core.LegTrailSL(instrument_move=10.0, stoploss_move=5.0),
    )

    reentered = core.maybe_reenter(engine, leg, market_price=150.0, now=_MIDDAY)

    assert reentered is True
    # New fill is the market price; base Points SL level is entry_fill + P.
    assert leg.entry_fill == 150.0
    assert leg.trail_level == 170.0  # 150 + 20 (re-applied base SL)
    assert leg.trail_ref == 150.0  # ratchet reference reset to the fresh fill


def test_immediate_reentry_reapplies_trail_without_base_sl(fake_client) -> None:
    """With no base SL, the trail seeds at the fresh entry fill (Req 12.5)."""
    fake_client.orderstatus_response = {"status": "success", "data": {}}
    engine = _engine(fake_client)
    leg = _stopped_short_ce(
        entry_fill=120.0,
        trail_sl=core.LegTrailSL(instrument_move=10.0, stoploss_move=5.0),
    )

    reentered = core.maybe_reenter(engine, leg, market_price=150.0, now=_MIDDAY)

    assert reentered is True
    assert leg.trail_level == 150.0
    assert leg.trail_ref == 150.0


def test_immediate_reentry_refreshes_underlying_baseline(fake_client) -> None:
    """A supplied spot refreshes the UnderlyingPoints baseline on re-entry (Req 12.5)."""
    fake_client.orderstatus_response = {"status": "success", "data": {}}
    engine = _engine(fake_client)
    leg = _stopped_short_ce(
        entry_fill=120.0,
        stop_loss=core.LegStopLoss(kind=core.SLKind.UNDERLYING_POINTS, value=100.0),
        entry_spot=24000.0,
    )

    reentered = core.maybe_reenter(
        engine, leg, market_price=150.0, spot=24120.0, now=_MIDDAY
    )

    assert reentered is True
    assert leg.entry_spot == 24120.0


def test_immediate_reentry_updates_runtime_state(fake_client) -> None:
    """The leg is reopened, counter advanced, exit reason cleared, order id set."""
    engine = _engine(fake_client)
    leg = _stopped_short_ce(entry_fill=120.0, reentries_done=1)

    reentered = core.maybe_reenter(engine, leg, market_price=150.0, now=_MIDDAY)

    assert reentered is True
    assert leg.is_open is True
    assert leg.exit_reason is None
    assert leg.reentries_done == 2
    assert leg.order_id == "1"
