"""Unit tests for the bjp_core data models, index registry, and test harness.

These verify Task 1.1 deliverables: the enums, dataclasses, the ``quantity``
property, the NIFTY/SENSEX registry constants, the exception types, and that
the in-memory fake OpenAlgo SDK client fixture is discovered and usable.
"""

from __future__ import annotations

import bjp_core as core


def test_enums_have_expected_values():
    assert core.OptionType.CE.value == "CE"
    assert core.OptionType.PE.value == "PE"
    assert core.Action.BUY.value == "BUY"
    assert core.Action.SELL.value == "SELL"
    assert core.SLKind.POINTS.value == "Points"
    assert core.SLKind.PERCENTAGE.value == "Percentage"
    assert core.SLKind.UNDERLYING_POINTS.value == "UnderlyingPoints"
    assert core.ReentryKind.IMMEDIATE.value == "Immediate"
    assert core.ReentryKind.AT_COST.value == "AtCost"
    assert core.ExecutionMode.SANDBOX.value == "sandbox"
    assert core.ExecutionMode.LIVE.value == "live"


def test_index_registry_constants():
    assert core.NIFTY.name == "NIFTY"
    assert core.NIFTY.index_exchange == "NSE_INDEX"
    assert core.NIFTY.fno_exchange == "NFO"
    assert core.NIFTY.lot_size == 65
    assert core.NIFTY.strike_step == 50

    assert core.SENSEX.name == "SENSEX"
    assert core.SENSEX.index_exchange == "BSE_INDEX"
    assert core.SENSEX.fno_exchange == "BFO"
    assert core.SENSEX.lot_size == 20
    assert core.SENSEX.strike_step == 100

    assert core.INDEX_REGISTRY == {"NIFTY": core.NIFTY, "SENSEX": core.SENSEX}


def test_strategy_config_quantity_property():
    config = core.StrategyConfig(
        strategy_name="NF-TEST",
        index=core.NIFTY,
        lots=2,
        entry_time="09:25:00",
        exit_time="15:29:00",
        legs=[core.LegConfig(core.OptionType.CE, core.Action.SELL, "ITM2")],
    )
    assert config.quantity == 2 * 65 == 130

    sensex_config = core.StrategyConfig(
        strategy_name="SENSEX-TEST",
        index=core.SENSEX,
        lots=5,
        entry_time="09:18:00",
        exit_time="15:23:00",
    )
    assert sensex_config.quantity == 5 * 20 == 100


def test_strategy_config_defaults():
    config = core.StrategyConfig(
        strategy_name="DEFAULTS",
        index=core.NIFTY,
        lots=1,
        entry_time="09:16:00",
        exit_time="15:22:00",
    )
    assert config.legs == []
    assert config.overall_stop_loss is None
    assert config.overall_trail_sl is None
    assert config.square_off_all_legs is False
    assert config.reentry_time_restriction_min is None
    assert config.product == "NRML"
    assert config.execution_mode is core.ExecutionMode.SANDBOX
    assert config.monitoring_interval == 1.0
    assert config.use_websocket is False


def test_config_dataclasses_hold_values():
    leg = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset="ITM2",
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15),
        trail_sl=core.LegTrailSL(70, 40),
        momentum=core.LegMomentum(10),
        reentry=core.LegReentry(core.ReentryKind.AT_COST, 1),
    )
    assert leg.stop_loss.kind is core.SLKind.POINTS
    assert leg.stop_loss.value == 15
    assert leg.trail_sl.instrument_move == 70
    assert leg.trail_sl.stoploss_move == 40
    assert leg.momentum.points_down == 10
    assert leg.reentry.kind is core.ReentryKind.AT_COST
    assert leg.reentry.count == 1

    overall_sl = core.OverallStopLoss(5200)
    overall_trail = core.OverallTrailSL(6000, 9000)
    assert overall_sl.mtm_rupees == 5200
    assert overall_trail.instrument_move == 6000
    assert overall_trail.stoploss_move == 9000


def test_runtime_state_defaults():
    leg_state = core.LegState(config=core.LegConfig(core.OptionType.PE, core.Action.SELL, "ATM"))
    assert leg_state.symbol is None
    assert leg_state.is_open is False
    assert leg_state.reentries_done == 0
    assert leg_state.pending_momentum is False

    config = core.StrategyConfig(
        strategy_name="STATE",
        index=core.SENSEX,
        lots=1,
        entry_time="09:16:00",
        exit_time="15:22:00",
    )
    engine_state = core.EngineState(config=config, client=object())
    assert engine_state.expiry is None
    assert engine_state.legs == []
    assert engine_state.entered_today is False
    assert engine_state.peak_mtm == 0.0
    assert engine_state.locked_mtm_stop is None
    assert engine_state.realized_mtm == 0.0
    assert engine_state.running is True


def test_exception_types_are_exceptions():
    assert issubclass(core.ConfigError, Exception)
    assert issubclass(core.ExpiryError, Exception)


def test_fake_client_fixture_records_calls_and_returns_canned_responses(fake_client):
    resp = fake_client.expiry(symbol="NIFTY", exchange="NFO", instrumenttype="options")
    assert resp["status"] == "success"
    assert fake_client.call_count("expiry") == 1
    assert fake_client.last_call("expiry")["symbol"] == "NIFTY"

    fake_client.quotes_responses.append({"status": "success", "data": {"ltp": 123.5}})
    assert fake_client.quotes(symbol="X", exchange="NFO")["data"]["ltp"] == 123.5
    # Queue exhausted -> falls back to the default single response.
    assert fake_client.quotes(symbol="X", exchange="NFO")["data"]["ltp"] == 100.0


def test_fake_client_factory_builds_configured_clients(fake_client_factory):
    client = fake_client_factory(api_key="k", host="http://h:1", ws_url="ws://w:2")
    assert client.api_key == "k"
    assert client.host == "http://h:1"
    assert client.ws_url == "ws://w:2"
    assert client.calls == []
