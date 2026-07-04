"""Edge-case tests for environment-resolution failures (Task 2.3).

These example tests cover the two failure/fallback branches of
``resolve_environment`` that are not exercised by the precedence property test:

* Req 2.5 - a missing (unset or empty) ``OPENALGO_API_KEY`` raises
  ``ConfigError`` so the caller can log and exit non-zero without placing
  orders.
* Req 2.4 - when WebSocket monitoring is enabled but ``WEBSOCKET_URL`` is unset
  and the ``WEBSOCKET_HOST``/``WEBSOCKET_PORT`` parts are incomplete, the
  resolver logs an error naming the missing variable and returns a
  ``ResolvedEnv`` with ``ws_url=None`` (polling fallback) without raising.

The production code under test lives in ``strategies/bjp_portfolio/
bjp_core.py`` and is imported as ``bjp_core`` via the sys.path setup in
``conftest.py``.
"""

from __future__ import annotations

import logging

import bjp_core as core
import pytest

_ENV_VARS = (
    "OPENALGO_API_KEY",
    "HOST_SERVER",
    "OPENALGO_HOST",
    "WEBSOCKET_URL",
    "WEBSOCKET_HOST",
    "WEBSOCKET_PORT",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable the resolver reads."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Req 2.5 - missing API key raises ConfigError (drives non-zero exit)
# ---------------------------------------------------------------------------


def test_unset_api_key_raises_config_error(monkeypatch, caplog):
    _clear_env(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        with pytest.raises(core.ConfigError):
            core.resolve_environment()

    # An error naming the missing API key is logged before the raise (Req 2.5).
    assert any("OPENALGO_API_KEY" in record.message for record in caplog.records)


def test_empty_api_key_raises_config_error(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "")

    with pytest.raises(core.ConfigError):
        core.resolve_environment()


def test_whitespace_only_api_key_raises_config_error(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "   ")

    with pytest.raises(core.ConfigError):
        core.resolve_environment()


def test_present_api_key_does_not_raise(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "real-key")

    env = core.resolve_environment()

    assert env.api_key == "real-key"


# ---------------------------------------------------------------------------
# Req 2.4 - WebSocket enabled with incomplete parts: log missing var,
# fall back to polling (ws_url=None), do not exit/raise.
# ---------------------------------------------------------------------------


def test_websocket_enabled_missing_port_falls_back_to_polling(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "real-key")
    monkeypatch.setenv("WEBSOCKET_HOST", "127.0.0.1")
    # WEBSOCKET_URL and WEBSOCKET_PORT deliberately unset.

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        env = core.resolve_environment(use_websocket=True)

    # No exit/raise: a ResolvedEnv is returned with polling fallback.
    assert env.ws_url is None
    # The error names the specific missing WebSocket variable (Req 2.4).
    assert any("WEBSOCKET_PORT" in record.message for record in caplog.records)


def test_websocket_enabled_missing_host_falls_back_to_polling(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "real-key")
    monkeypatch.setenv("WEBSOCKET_PORT", "8765")
    # WEBSOCKET_URL and WEBSOCKET_HOST deliberately unset.

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        env = core.resolve_environment(use_websocket=True)

    assert env.ws_url is None
    assert any("WEBSOCKET_HOST" in record.message for record in caplog.records)


def test_websocket_enabled_both_parts_missing_falls_back_to_polling(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "real-key")
    # WEBSOCKET_URL, WEBSOCKET_HOST, and WEBSOCKET_PORT all unset.

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        env = core.resolve_environment(use_websocket=True)

    assert env.ws_url is None
    messages = " ".join(record.message for record in caplog.records)
    assert "WEBSOCKET_HOST" in messages
    assert "WEBSOCKET_PORT" in messages


def test_websocket_enabled_empty_part_treated_as_missing(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "real-key")
    monkeypatch.setenv("WEBSOCKET_HOST", "127.0.0.1")
    monkeypatch.setenv("WEBSOCKET_PORT", "   ")  # whitespace-only == empty

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        env = core.resolve_environment(use_websocket=True)

    assert env.ws_url is None
    assert any("WEBSOCKET_PORT" in record.message for record in caplog.records)


def test_websocket_disabled_incomplete_parts_no_error_logged(monkeypatch, caplog):
    # Sanity check on the fallback semantics: when WebSocket monitoring is not
    # enabled, incomplete parts are not reported as an error and ws_url is None.
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENALGO_API_KEY", "real-key")
    monkeypatch.setenv("WEBSOCKET_HOST", "127.0.0.1")

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        env = core.resolve_environment(use_websocket=False)

    assert env.ws_url is None
    assert not any("WEBSOCKET" in record.message for record in caplog.records)
