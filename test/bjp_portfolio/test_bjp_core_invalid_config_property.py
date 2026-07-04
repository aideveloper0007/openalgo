"""Property-based test for invalid-configuration rejection.

Implements Property 3 from the design: for any configuration in which at least
one field is out of range or malformed (lots outside 1-100, entry/exit not a
valid ``HH:MM:SS`` string, ``exit_time`` not later than ``entry_time``, or
execution mode not one of sandbox/live), validation fails, an error naming the
offending parameter is produced, and no entry orders are placed (Req 3.1, 3.4,
3.8, 3.9, 16.5).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``.

``validate_config`` does not place orders itself, so "no entry orders placed" is
asserted by requiring it to raise ``ConfigError`` (fatal to the caller, which
exits before scheduling/trading) whose message names the offending parameter.

Environment overrides (``BJP_*``) are applied inside ``validate_config`` before
validation, so this test clears those variables to keep generated inputs from
being silently replaced, restoring the prior environment afterward.
"""

from __future__ import annotations

import os

import bjp_core as core
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 3: Invalid configuration is
# rejected and never begins trading. For any config with at least one malformed
# field (lots outside 1-100, entry/exit not valid HH:MM:SS, exit_time not later
# than entry_time, or execution mode not sandbox/live), validate_config raises
# ConfigError naming the offending parameter and places no entry orders.
# Validates: Requirements 3.1, 3.4, 3.8, 3.9, 16.5.

# Env-override vars applied before validation; cleared so they cannot mask the
# malformed field under test.
_OVERRIDE_VARS = tuple(core.ENV_OVERRIDES.values())

# Strings that are not valid 24-hour HH:MM:SS times.
_INVALID_TIME_STRINGS = [
    "",
    "banana",
    "12:00",
    "12",
    "24:00:00",
    "23:60:00",
    "99:99:99",
    "-1:00:00",
    "12:00:00 PM",
    "12:00:00:00",
    "aa:bb:cc",
]

# Values that are not a supported execution mode (None is excluded: it defaults
# to sandbox and is therefore valid).
_INVALID_MODE_VALUES: list[object] = ["paper", "prod", "", "  ", "SANDBOXX", 123, 4.5]


def _baseline_kwargs() -> dict[str, object]:
    """Return kwargs for a fully valid StrategyConfig used as the mutation base."""
    return {
        "strategy_name": "PROP3",
        "index": core.NIFTY,
        "lots": 2,
        "entry_time": "09:16:00",
        "exit_time": "15:29:00",
        "execution_mode": core.ExecutionMode.SANDBOX,
        "monitoring_interval": 1.0,
    }


def _hms(total_seconds: int) -> str:
    """Format a seconds-since-midnight count as a valid HH:MM:SS string."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


_invalid_lots = st.one_of(
    st.integers(max_value=0),
    st.integers(min_value=core.MAX_LOTS + 1, max_value=10_000),
    st.booleans(),  # bool is a subclass of int and is explicitly rejected
    st.sampled_from([0.5, 1.5, 2.5, 50.5]),  # non-integer floats
    st.sampled_from(["5", "abc", "2"]),  # numeric/non-numeric strings
)

_invalid_time = st.one_of(
    st.sampled_from(_INVALID_TIME_STRINGS),
    st.sampled_from([None, 91600, 9.16]),  # non-string values are also invalid
)


@st.composite
def _malformed_case(draw: st.DrawFn) -> tuple[dict[str, object], str]:
    """Build a config with exactly one malformed field and its expected name.

    Exactly one field is mutated so that the field validated first in
    ``validate_config`` (lots -> index -> execution_mode -> entry -> exit) is the
    one under test, letting the test assert the error names that parameter.
    """
    kwargs = _baseline_kwargs()
    category = draw(
        st.sampled_from(
            ["lots", "entry_time", "exit_time", "exit_not_later", "execution_mode"]
        )
    )

    if category == "lots":
        kwargs["lots"] = draw(_invalid_lots)
        return kwargs, "lots"

    if category == "entry_time":
        kwargs["entry_time"] = draw(_invalid_time)
        return kwargs, "entry_time"

    if category == "exit_time":
        kwargs["exit_time"] = draw(_invalid_time)
        return kwargs, "exit_time"

    if category == "exit_not_later":
        # Both valid HH:MM:SS, but exit_time <= entry_time.
        entry_secs = draw(st.integers(min_value=0, max_value=86_399))
        delta = draw(st.integers(min_value=0, max_value=entry_secs))
        kwargs["entry_time"] = _hms(entry_secs)
        kwargs["exit_time"] = _hms(entry_secs - delta)
        return kwargs, "exit_time"

    # execution_mode not one of sandbox/live.
    kwargs["execution_mode"] = draw(st.sampled_from(_INVALID_MODE_VALUES))
    return kwargs, "execution_mode"


@settings(max_examples=100)
@given(case=_malformed_case())
def test_invalid_config_is_rejected_and_names_parameter(
    case: tuple[dict[str, object], str],
):
    kwargs, expected_param = case

    saved = {name: os.environ.get(name) for name in _OVERRIDE_VARS}
    try:
        for name in _OVERRIDE_VARS:
            os.environ.pop(name, None)

        config = core.StrategyConfig(**kwargs)

        # Validation fails: ConfigError is fatal, so no scheduling/trading and
        # thus no entry orders are ever placed.
        with pytest.raises(core.ConfigError) as excinfo:
            core.validate_config(config)

        # The error identifies the offending parameter.
        assert expected_param in str(excinfo.value)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
