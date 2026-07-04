"""Integration tests for effective-mode routing (mocked SDK).

Exercises ``apply_execution_mode`` (Task 17.1) end-to-end against the in-memory
fake OpenAlgo SDK client from ``conftest.py``. These are example-based
integration tests (Task 17.2) focused on the platform-mismatch path of
Requirement 16.6:

    * Live Execution_Mode is explicitly configured, so the engine turns the
      platform analyzer off via ``client.analyzertoggle(mode=False)``; but the
      platform's analyzer state still reports analyze mode on
      (``client.analyzerstatus()`` -> ``analyze_mode == True``), which prevents
      live routing. The engine SHALL log the mismatch and return the platform's
      effective mode (sandbox) so the caller follows the platform (Req 16.6).

Complementary happy-path assertions confirm that when the platform honours the
live switch the effective mode is live, so the mismatch handling is not
vacuously true.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 16.6.
"""

from __future__ import annotations

import logging

import bjp_core as core


def _analyzer_status(analyze_mode: bool) -> dict[str, object]:
    """Build a success-shaped ``analyzerstatus`` envelope."""
    return {"status": "success", "data": {"analyze_mode": analyze_mode}}


def _live_config() -> core.StrategyConfig:
    """Build a minimal NIFTY config explicitly configured for live routing."""
    return core.StrategyConfig(
        strategy_name="NF-MODE-TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:25:00",
        exit_time="15:29:00",
        legs=[
            core.LegConfig(
                option_type=core.OptionType.CE,
                action=core.Action.SELL,
                offset="ITM2",
            ),
        ],
        execution_mode=core.ExecutionMode.LIVE,
    )


def _engine(config: core.StrategyConfig, fake_client: object) -> core.EngineState:
    """Build an ``EngineState`` bound to the fake client."""
    return core.EngineState(config=config, client=fake_client)


def test_live_configured_but_platform_blocks_routes_per_effective_mode(
    fake_client, caplog
) -> None:
    """Live configured, platform analyzer on -> mismatch logged, sandbox routing (Req 16.6)."""
    config = _live_config()
    engine = _engine(config, fake_client)
    # Live was requested, so the engine toggles analyze mode off; but the
    # platform's effective analyzer state still reports analyze mode ON,
    # preventing live routing.
    fake_client.analyzertoggle_response = _analyzer_status(True)
    fake_client.analyzerstatus_response = _analyzer_status(True)

    with caplog.at_level(logging.WARNING, logger="bjp_core"):
        effective = core.apply_execution_mode(engine)

    # The engine follows the platform's effective mode, not the configured one.
    assert effective is core.ExecutionMode.SANDBOX

    # It applied the desired (live) switch by turning analyze mode off ...
    toggle_call = fake_client.last_call("analyzertoggle")
    assert toggle_call is not None
    assert toggle_call["mode"] is False
    # ... and consulted the platform's authoritative analyzer state (Req 16.6).
    assert fake_client.call_count("analyzerstatus") == 1

    # The mismatch is logged so the operator sees configured != effective.
    mismatch_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "mismatch" in record.getMessage()
    ]
    assert len(mismatch_logs) == 1
    assert "NF-MODE-TEST" in mismatch_logs[0]


def test_live_configured_and_platform_honours_switch_routes_live(
    fake_client,
) -> None:
    """Live configured, platform analyzer off -> effective mode is live (Req 16.6)."""
    config = _live_config()
    engine = _engine(config, fake_client)
    # The platform honours the live switch: analyze mode is off after toggle.
    fake_client.analyzertoggle_response = _analyzer_status(False)
    fake_client.analyzerstatus_response = _analyzer_status(False)

    effective = core.apply_execution_mode(engine)

    # No mismatch: the effective mode matches the configured live mode.
    assert effective is core.ExecutionMode.LIVE
    toggle_call = fake_client.last_call("analyzertoggle")
    assert toggle_call is not None
    assert toggle_call["mode"] is False


def test_live_configured_but_platform_indeterminate_defaults_to_sandbox(
    fake_client, caplog
) -> None:
    """Live configured, analyzer status malformed -> conservative sandbox routing (Req 16.6)."""
    config = _live_config()
    engine = _engine(config, fake_client)
    fake_client.analyzertoggle_response = _analyzer_status(False)
    # A malformed status envelope makes the platform state indeterminate.
    fake_client.analyzerstatus_response = {"status": "error"}

    with caplog.at_level(logging.WARNING, logger="bjp_core"):
        effective = core.apply_execution_mode(engine)

    # Indeterminate platform state resolves conservatively to sandbox so no
    # order is ever unexpectedly routed live (Req 16.6).
    assert effective is core.ExecutionMode.SANDBOX
    mismatch_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "mismatch" in record.getMessage()
    ]
    assert len(mismatch_logs) == 1
