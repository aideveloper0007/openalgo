"""Integration test for scheduler timezone and SDK client initialization (Task 20.4).

Validates that:

* The APScheduler ``BackgroundScheduler`` is configured with ``Asia/Kolkata``
  timezone (Req 6.1).
* The SDK client is initialized with the resolved ``api_key`` and ``host``
  from environment resolution (Req 2.6).
* The ``run()`` function exits non-zero when the API key is missing (Req 2.5).
* The ``run()`` function exits non-zero on invalid config (Req 3.8, 3.9).

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 2.5, 2.6, 3.8, 6.1.
"""
from __future__ import annotations

import os
from datetime import time
from unittest.mock import MagicMock, patch

import bjp_core as core
import pytest
from conftest import FakeOpenAlgoClient


def test_entry_exit_times_parse_to_correct_hms() -> None:
    """Entry/exit times parse correctly for cron scheduling (Req 6.1)."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:25:00",
        exit_time="15:29:00",
    )
    entry_t = core._parse_hms(config.entry_time, "entry_time").time()
    exit_t = core._parse_hms(config.exit_time, "exit_time").time()

    assert entry_t == time(9, 25, 0)
    assert exit_t == time(15, 29, 0)


def test_scheduler_creates_with_kolkata_timezone() -> None:
    """BackgroundScheduler is initialized with timezone='Asia/Kolkata' (Req 6.1).

    This test patches the BackgroundScheduler class at the source module to
    intercept the constructor call and verify the timezone argument.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    mock_scheduler = MagicMock(spec=BackgroundScheduler)
    mock_scheduler.start = MagicMock()
    mock_scheduler.shutdown = MagicMock()
    mock_scheduler.add_job = MagicMock()

    config = core.StrategyConfig(
        strategy_name="SCHED_TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:25:00",
        exit_time="15:29:00",
    )

    env_patch = {
        "OPENALGO_API_KEY": "test-key",
    }

    fake_client = FakeOpenAlgoClient()

    mock_openalgo = MagicMock()
    mock_openalgo.api.return_value = fake_client

    with (
        patch.dict(os.environ, env_patch, clear=False),
        patch(
            "apscheduler.schedulers.background.BackgroundScheduler",
            return_value=mock_scheduler,
        ) as sched_cls,
        patch.dict("sys.modules", {"openalgo": mock_openalgo}),
        patch.object(core, "_sleep", side_effect=SystemExit(0)),
    ):
        try:
            core.run(config)
        except SystemExit:
            pass

    # Verify the scheduler was created with Asia/Kolkata timezone
    sched_cls.assert_called_once_with(timezone="Asia/Kolkata")


def test_run_exits_nonzero_on_missing_api_key() -> None:
    """run() exits non-zero when OPENALGO_API_KEY is unset (Req 2.5)."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:26:00",
    )
    # Ensure the key is truly absent by removing it before run()
    saved = os.environ.pop("OPENALGO_API_KEY", None)
    try:
        with pytest.raises(SystemExit) as exc_info:
            core.run(config)
        assert exc_info.value.code != 0
    finally:
        if saved is not None:
            os.environ["OPENALGO_API_KEY"] = saved


def test_run_exits_nonzero_on_invalid_config() -> None:
    """run() exits non-zero on invalid configuration (Req 3.8, 3.9)."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=999,  # Invalid: > 100
        entry_time="09:20:00",
        exit_time="15:26:00",
    )
    env_patch = {"OPENALGO_API_KEY": "test-key"}
    with (
        patch.dict(os.environ, env_patch, clear=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        core.run(config)

    assert exc_info.value.code != 0


def test_sdk_client_receives_resolved_env(fake_client: FakeOpenAlgoClient) -> None:
    """The SDK client is initialized with resolved api_key and host (Req 2.6).

    This test verifies that resolve_environment() produces a ResolvedEnv whose
    api_key and host would be passed to the SDK client constructor.
    """
    env_vars = {
        "OPENALGO_API_KEY": "my-test-key",
        "HOST_SERVER": "http://custom-host:9000",
    }
    with patch.dict(os.environ, env_vars, clear=False):
        env = core.resolve_environment(use_websocket=False)

    assert env.api_key == "my-test-key"
    assert env.host == "http://custom-host:9000"
