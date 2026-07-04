"""Edge-case tests for weekly-expiry failure modes (Task 5.3).

These example tests cover the failure branches of ``resolve_weekly_expiry``
that are not exercised by the selection property test:

* Req 5.3 - an unsupported index (anything other than NIFTY/SENSEX) raises
  ``ExpiryError`` *without* calling ``client.expiry()`` and therefore places no
  entry orders.
* Req 5.4 - a non-success status, zero expiries, and a call that exceeds the
  timeout budget each raise ``ExpiryError`` so no entry orders are placed.

All failure paths raise ``ExpiryError``; since entry placement is gated on a
resolved expiry, raising guarantees no entry orders are placed downstream.

The production code under test lives in ``strategies/bjp_portfolio/
bjp_core.py`` and is imported as ``bjp_core`` via the sys.path setup in
``conftest.py``. The ``fake_client`` fixture comes from ``conftest.py``.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import bjp_core as core
import pytest

# The F&O exchange the resolver would pass through to the SDK for NIFTY.
_NIFTY_FNO = "NFO"


# ---------------------------------------------------------------------------
# Req 5.3 - unsupported index: error, no client.expiry() call, no orders.
# ---------------------------------------------------------------------------


def test_unsupported_index_raises_without_calling_expiry(fake_client, caplog):
    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        with pytest.raises(core.ExpiryError):
            core.resolve_weekly_expiry(fake_client, "BANKNIFTY", _NIFTY_FNO)

    # The SDK expiry endpoint must never be touched for an unsupported index.
    assert fake_client.call_count("expiry") == 0
    # An error naming the unsupported index is logged before the raise (Req 5.3).
    assert any("BANKNIFTY" in record.message for record in caplog.records)


def test_unsupported_index_empty_string_does_not_call_expiry(fake_client):
    with pytest.raises(core.ExpiryError):
        core.resolve_weekly_expiry(fake_client, "", _NIFTY_FNO)

    assert fake_client.call_count("expiry") == 0


# ---------------------------------------------------------------------------
# Req 5.4 - non-success status raises ExpiryError (no entry orders).
# ---------------------------------------------------------------------------


def test_non_success_status_raises_expiry_error(fake_client, caplog):
    fake_client.expiry_response = {"status": "error", "message": "boom"}

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        with pytest.raises(core.ExpiryError):
            core.resolve_weekly_expiry(fake_client, "NIFTY", _NIFTY_FNO)

    # The SDK was consulted exactly once; the failure is in the response.
    assert fake_client.call_count("expiry") == 1


# ---------------------------------------------------------------------------
# Req 5.4 - zero expiries raises ExpiryError (no entry orders).
# ---------------------------------------------------------------------------


def test_zero_expiries_raises_expiry_error(fake_client):
    fake_client.expiry_response = {"status": "success", "data": []}

    with pytest.raises(core.ExpiryError):
        core.resolve_weekly_expiry(fake_client, "NIFTY", _NIFTY_FNO)

    assert fake_client.call_count("expiry") == 1


def test_missing_data_key_raises_expiry_error(fake_client):
    # A success envelope with no "data" key behaves like zero expiries.
    fake_client.expiry_response = {"status": "success"}

    with pytest.raises(core.ExpiryError):
        core.resolve_weekly_expiry(fake_client, "NIFTY", _NIFTY_FNO)


# ---------------------------------------------------------------------------
# Req 5.4 - a call that exceeds the timeout budget raises ExpiryError.
#
# EXPIRY_TIMEOUT_SECONDS is 10s in production. To keep the test fast we
# monkeypatch it down to 0.2s and make the fake client's expiry() block for
# 0.5s (> the shrunk budget) so the worker-thread timeout path fires quickly.
# ---------------------------------------------------------------------------


def test_slow_expiry_call_times_out_and_raises(fake_client, monkeypatch, caplog):
    monkeypatch.setattr(core, "EXPIRY_TIMEOUT_SECONDS", 0.2)

    def _slow_expiry(*args, **kwargs):
        time.sleep(0.5)  # longer than the shrunk 0.2s budget
        return {"status": "success", "data": ["31-DEC-25"]}

    monkeypatch.setattr(fake_client, "expiry", _slow_expiry)

    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        with pytest.raises(core.ExpiryError, match="timed out"):
            core.resolve_weekly_expiry(fake_client, "NIFTY", _NIFTY_FNO)
    elapsed = time.monotonic() - started

    # The failure surfaces near the budget, well before the 10s production one.
    assert elapsed < 5.0
    # A timeout-cause error is logged before the raise (Req 5.4).
    assert any(
        "did not complete within" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Sanity: a healthy response still resolves (guards against over-strict tests).
# ---------------------------------------------------------------------------


def test_success_response_resolves_expiry(fake_client):
    fake_client.expiry_response = {"status": "success", "data": ["31-DEC-25"]}

    result = core.resolve_weekly_expiry(
        fake_client, "NIFTY", _NIFTY_FNO, today=date(2025, 12, 1)
    )

    assert result == "31-DEC-25"
    assert fake_client.call_count("expiry") == 1
