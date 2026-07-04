"""Property-based test for environment resolution precedence.

Implements Property 1 from the design: for any combination of set, empty, and
unset values for the WebSocket/host environment variables, the resolved REST
host is the first non-empty of (HOST_SERVER, OPENALGO_HOST, DEFAULT_HOST) and
the resolved WebSocket endpoint is WEBSOCKET_URL when non-empty, otherwise the
endpoint constructed from WEBSOCKET_HOST and WEBSOCKET_PORT (Req 2.2, 2.3).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``.
Whitespace-only values are treated as empty. A valid ``OPENALGO_API_KEY`` is
always set here because a missing key raises ``ConfigError`` before host/ws
resolution.
"""

from __future__ import annotations

import os

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 1: Environment resolution follows
# precedence. For any set/empty/unset combination of HOST_SERVER,
# OPENALGO_HOST, WEBSOCKET_URL, WEBSOCKET_HOST, and WEBSOCKET_PORT, the resolved
# host equals the first non-empty of (HOST_SERVER, OPENALGO_HOST,
# http://127.0.0.1:5000) and the resolved ws endpoint equals WEBSOCKET_URL when
# non-empty else ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT} when both present else
# None. Validates: Requirements 2.2, 2.3.

# The env vars managed by this test (api key is handled separately).
_MANAGED_VARS = (
    "OPENALGO_API_KEY",
    "HOST_SERVER",
    "OPENALGO_HOST",
    "WEBSOCKET_URL",
    "WEBSOCKET_HOST",
    "WEBSOCKET_PORT",
)

# "empty" values: unset is modeled by None; these all strip to "".
_EMPTY_VALUES = ["", "   ", "\t"]


def _env_value(real_values: list[str]) -> st.SearchStrategy[str | None]:
    """Generate an env-var value: unset (None), empty/whitespace, or a real value."""
    return st.one_of(
        st.none(),
        st.sampled_from(_EMPTY_VALUES),
        st.sampled_from(real_values),
    )


def _nonempty(value: str | None) -> str | None:
    """Mirror bjp_core's rule: strip; whitespace-only or None is treated empty."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _apply_env(values: dict[str, str | None]) -> None:
    """Set or delete each managed var according to ``values`` (None deletes)."""
    for name in _MANAGED_VARS:
        value = values.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@settings(max_examples=200)
@given(
    host_server=_env_value(["http://hs.example:5000", "http://10.0.0.1:8080"]),
    openalgo_host=_env_value(["http://oah.example:5000", "http://10.0.0.2:9090"]),
    websocket_url=_env_value(["ws://explicit.example:8765", "ws://10.0.0.3:9000"]),
    websocket_host=_env_value(["ws.example", "10.0.0.4"]),
    websocket_port=_env_value(["8765", "9001"]),
)
def test_environment_resolution_follows_precedence(
    host_server: str | None,
    openalgo_host: str | None,
    websocket_url: str | None,
    websocket_host: str | None,
    websocket_port: str | None,
):
    saved = {name: os.environ.get(name) for name in _MANAGED_VARS}
    try:
        _apply_env(
            {
                "OPENALGO_API_KEY": "valid-api-key",
                "HOST_SERVER": host_server,
                "OPENALGO_HOST": openalgo_host,
                "WEBSOCKET_URL": websocket_url,
                "WEBSOCKET_HOST": websocket_host,
                "WEBSOCKET_PORT": websocket_port,
            }
        )

        resolved = core.resolve_environment(use_websocket=False)

        # Host precedence: first non-empty of HOST_SERVER, OPENALGO_HOST, default.
        expected_host = (
            _nonempty(host_server)
            or _nonempty(openalgo_host)
            or core.DEFAULT_HOST
        )
        assert resolved.host == expected_host

        # WebSocket endpoint: WEBSOCKET_URL if non-empty, else built from parts.
        ws_url = _nonempty(websocket_url)
        if ws_url is not None:
            expected_ws: str | None = ws_url
        else:
            ws_host = _nonempty(websocket_host)
            ws_port = _nonempty(websocket_port)
            if ws_host is not None and ws_port is not None:
                expected_ws = f"ws://{ws_host}:{ws_port}"
            else:
                expected_ws = None
        assert resolved.ws_url == expected_ws

        # The API key is always the valid, non-empty value.
        assert resolved.api_key == "valid-api-key"
    finally:
        _apply_env(saved)
