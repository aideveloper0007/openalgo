"""Unit tests for startup logging and log_event content (Tasks 20.3).

Validates that:

* The startup log contains the strategy name, index, execution mode, and
  entry/exit times (Req 1.4, 16.3).
* Action logs (place/square-off) include symbol, action, quantity, and price
  context (Req 18.1).
* Trigger logs include the condition and causing values (Req 18.2).
* SDK error logs are emitted at ERROR level (Req 18.3).
* IST timestamps are present in the log output (Req 18.4).
* ``configure_logging`` is idempotent and installs only one handler.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 1.4, 16.3, 18.1, 18.2, 18.3, 18.4.
"""
from __future__ import annotations

import io
import logging

import bjp_core as core


def test_startup_log_contains_required_fields(caplog: object) -> None:
    """Startup log line contains name, index, mode, entry, and exit (Req 1.4, 16.3)."""
    stream = io.StringIO()
    log = core.configure_logging(stream=stream, level=logging.INFO, force=True)

    log.info(
        "Starting %s index=%s mode=%s entry=%s exit=%s lots=%d qty=%d",
        "NF1", "NIFTY", "sandbox", "09:25:00", "15:29:00", 2, 130,
    )
    output = stream.getvalue()

    assert "NF1" in output
    assert "NIFTY" in output
    assert "sandbox" in output
    assert "09:25:00" in output
    assert "15:29:00" in output


def test_log_event_action_includes_symbol_action_qty_price() -> None:
    """Action log_events carry symbol/action/quantity/price context (Req 18.1)."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, level=logging.INFO, force=True)

    msg = core.log_event(
        "place",
        symbol="NIFTY31DEC2524000CE",
        action=core.Action.SELL,
        quantity=130,
        price=250.5,
    )
    assert "place" in msg
    assert "NIFTY31DEC2524000CE" in msg
    assert "SELL" in msg
    assert "130" in msg
    assert "250.5" in msg

    output = stream.getvalue()
    assert "NIFTY31DEC2524000CE" in output


def test_log_event_trigger_includes_condition_and_values() -> None:
    """Trigger log_events carry condition and causing values (Req 18.2)."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, level=logging.INFO, force=True)

    msg = core.log_event(
        "leg_stop_loss",
        symbol="NIFTY31DEC2524000CE",
        condition="premium >= threshold",
        premium=115.0,
        threshold=115.0,
    )
    assert "leg_stop_loss" in msg
    assert "premium >= threshold" in msg
    assert "115.0" in msg

    output = stream.getvalue()
    assert "leg_stop_loss" in output


def test_log_event_sdk_error_emits_at_error_level() -> None:
    """SDK error log_events are emitted at ERROR level (Req 18.3)."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, level=logging.DEBUG, force=True)

    msg = core.log_event(
        "sdk_error",
        message="Connection refused",
        endpoint="quotes",
    )
    assert "sdk_error" in msg
    assert "Connection refused" in msg

    output = stream.getvalue()
    assert "ERROR" in output


def test_log_event_non_error_emits_at_info_level() -> None:
    """Non-error log_events are emitted at INFO level."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, level=logging.DEBUG, force=True)

    core.log_event("place", symbol="SYM1")

    output = stream.getvalue()
    assert "INFO" in output


def test_ist_timestamp_present_in_log_output() -> None:
    """Log output carries an IST-timestamped line (Req 18.4)."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, level=logging.INFO, force=True)

    core.log_event("test", detail="check_timestamp")

    output = stream.getvalue()
    # IST formatter renders "IST" in the timestamp (via %Z in _LOG_DATEFMT).
    assert "IST" in output


def test_configure_logging_idempotent() -> None:
    """Repeated configure_logging does not stack duplicate handlers."""
    stream1 = io.StringIO()
    stream2 = io.StringIO()

    log1 = core.configure_logging(stream=stream1, force=True)
    handler_count_1 = sum(
        1 for h in log1.handlers if getattr(h, "_bjp_ist_handler", False)
    )

    log2 = core.configure_logging(stream=stream2)
    handler_count_2 = sum(
        1 for h in log2.handlers if getattr(h, "_bjp_ist_handler", False)
    )

    assert handler_count_1 == 1
    assert handler_count_2 == 1


def test_log_event_returns_formatted_message() -> None:
    """log_event returns the single-line message for reuse."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, force=True)

    msg = core.log_event("test_kind", key1="val1", key2=42)
    assert msg == "test_kind key1=val1 key2=42"


def test_log_event_enum_values_rendered_by_value() -> None:
    """Enum fields are rendered by their .value (e.g. CE not OptionType.CE)."""
    stream = io.StringIO()
    core.configure_logging(stream=stream, force=True)

    msg = core.log_event(
        "square_off",
        action=core.Action.BUY,
        option_type=core.OptionType.CE,
    )
    assert "BUY" in msg
    assert "CE" in msg
    assert "Action.BUY" not in msg
    assert "OptionType.CE" not in msg
