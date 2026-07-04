"""Shared fixtures for the BJP portfolio strategy tests.

Production code for these strategies lives in ``strategies/bjp_portfolio/``,
which is excluded from Ruff and is not importable as a package. This module
mirrors the standalone-import mechanics used by the Strategy Host (the script's
own directory is on ``sys.path``) by prepending that directory to ``sys.path``
so tests can ``import bjp_core`` directly.

It also provides an in-memory fake OpenAlgo SDK client so property and
integration tests can run 100+ iterations with no network cost.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Make ``import bjp_core`` resolve to strategies/bjp_portfolio/bjp_core.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BJP_DIR = _REPO_ROOT / "strategies" / "bjp_portfolio"
if str(_BJP_DIR) not in sys.path:
    sys.path.insert(0, str(_BJP_DIR))


class FakeOpenAlgoClient:
    """In-memory stand-in for ``openalgo.api``.

    The fake records every call it receives in ``self.calls`` (a list of
    ``(method_name, kwargs)`` tuples) and returns canned, success-shaped
    responses that mirror the OpenAlgo SDK. Individual method responses can be
    overridden per test by assigning to the matching ``*_response`` attribute
    or by pushing onto the matching ``*_responses`` queue (consumed in order,
    falling back to the single ``*_response`` once exhausted).

    No network access ever occurs.
    """

    def __init__(
        self,
        api_key: str = "fake-key",
        host: str = "http://127.0.0.1:5000",
        ws_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.host = host
        self.ws_url = ws_url

        # Call log: list of (method_name, kwargs).
        self.calls: list[tuple[str, dict[str, Any]]] = []

        # Default single responses (success-shaped SDK envelopes).
        self.quotes_response: dict[str, Any] = {
            "status": "success",
            "data": {"ltp": 100.0},
        }
        self.expiry_response: dict[str, Any] = {
            "status": "success",
            "data": ["31-DEC-25"],
        }
        self.optionsmultiorder_response: dict[str, Any] = {
            "status": "success",
            "results": [],
        }
        self.optionsorder_response: dict[str, Any] = {
            "status": "success",
            "orderid": "1",
        }
        self.placeorder_response: dict[str, Any] = {
            "status": "success",
            "orderid": "1",
        }
        self.orderstatus_response: dict[str, Any] = {
            "status": "success",
            "data": {"average_price": 100.0},
        }
        self.analyzerstatus_response: dict[str, Any] = {
            "status": "success",
            "data": {"analyze_mode": True},
        }
        self.analyzertoggle_response: dict[str, Any] = {"status": "success"}

        # Optional per-method response queues for multi-step scenarios.
        self.quotes_responses: list[dict[str, Any]] = []
        self.expiry_responses: list[dict[str, Any]] = []
        self.optionsmultiorder_responses: list[dict[str, Any]] = []
        self.optionsorder_responses: list[dict[str, Any]] = []
        self.placeorder_responses: list[dict[str, Any]] = []
        self.orderstatus_responses: list[dict[str, Any]] = []
        self.analyzerstatus_responses: list[dict[str, Any]] = []
        self.analyzertoggle_responses: list[dict[str, Any]] = []

    # -- internal helpers --------------------------------------------------

    def _respond(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Record the call and return the next queued or default response."""
        self.calls.append((name, kwargs))
        queue: list[dict[str, Any]] = getattr(self, f"{name}_responses")
        if queue:
            return queue.pop(0)
        return getattr(self, f"{name}_response")

    def call_count(self, name: str) -> int:
        """Return how many times ``name`` has been called."""
        return sum(1 for called, _ in self.calls if called == name)

    def last_call(self, name: str) -> dict[str, Any] | None:
        """Return the kwargs of the most recent call to ``name`` (or None)."""
        for called, kwargs in reversed(self.calls):
            if called == name:
                return kwargs
        return None

    # -- SDK surface used by the engine ------------------------------------

    def quotes(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("quotes", **kwargs)

    def expiry(self, symbol: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if symbol is not None:
            kwargs["symbol"] = symbol
        return self._respond("expiry", **kwargs)

    def optionsmultiorder(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("optionsmultiorder", **kwargs)

    def optionsorder(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("optionsorder", **kwargs)

    def placeorder(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("placeorder", **kwargs)

    def orderstatus(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("orderstatus", **kwargs)

    def analyzerstatus(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("analyzerstatus", **kwargs)

    def analyzertoggle(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("analyzertoggle", **kwargs)


@pytest.fixture
def fake_client() -> FakeOpenAlgoClient:
    """Return a fresh in-memory fake OpenAlgo SDK client."""
    return FakeOpenAlgoClient()


@pytest.fixture
def fake_client_factory():
    """Return a factory that builds configured fake clients."""

    def _factory(**kwargs: Any) -> FakeOpenAlgoClient:
        return FakeOpenAlgoClient(**kwargs)

    return _factory
