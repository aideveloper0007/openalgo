"""Property-based test for monitoring-interval validation and defaulting.

Implements Property 5 from the design: for any configured monitoring interval,
``validate_config`` leaves the effective interval equal to the configured value
when it is numeric and within 0.1-60s inclusive, and coerces it to the 1.0s
default otherwise (numeric out-of-range or non-numeric input) (Req 3.6, 14.2,
14.3).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``. All
``BJP_*`` override environment variables are cleared so they never shadow the
in-script configured interval.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# monkeypatch is a function-scoped fixture; clearing the BJP_* env vars is a
# deterministic, idempotent operation, so suppress the function-scoped-fixture
# health check that would otherwise flag reusing it across examples.
_SETTINGS = settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Feature: bjp-portfolio-strategies, Property 5: Monitoring interval is
# validated and defaulted. For any configured monitoring_interval, after
# validate_config the effective interval equals the configured value when it is
# numeric within [0.1, 60.0] inclusive, and equals the 1.0s default otherwise
# (numeric out-of-range or non-numeric). Validates: Requirements 3.6, 14.2,
# 14.3.


def _clear_bjp_env(monkeypatch) -> None:
    """Remove every BJP_* override so it cannot shadow the configured value."""
    for env_name in core.ENV_OVERRIDES.values():
        monkeypatch.delenv(env_name, raising=False)


def _build_config(monitoring_interval: object) -> core.StrategyConfig:
    """Return a fully valid StrategyConfig varying only monitoring_interval.

    Entry precedes exit, lots are in range, and the index is a supported one so
    that only the monitoring interval governs the assertion outcome.
    """
    return core.StrategyConfig(
        strategy_name="prop5",
        index=core.NIFTY,
        lots=2,
        entry_time="09:20:00",
        exit_time="15:25:00",
        monitoring_interval=monitoring_interval,  # type: ignore[arg-type]
    )


# In-range numeric values, including the inclusive boundaries 0.1 and 60.0.
_in_range = st.one_of(
    st.floats(
        min_value=core.MIN_MONITORING_INTERVAL,
        max_value=core.MAX_MONITORING_INTERVAL,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.sampled_from([0.1, 60.0, 1.0, 5, 30, 0.5]),
)

# Out-of-range numeric values (below 0.1 or above 60.0), including 0, negatives.
_out_of_range = st.one_of(
    st.floats(
        min_value=-1000.0,
        max_value=0.09,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.floats(
        min_value=60.01,
        max_value=10_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.sampled_from([0, 0.05, 60.1, 1000, -5]),
)

# Non-numeric values that cannot be interpreted as a valid interval.
_non_numeric = st.sampled_from(["abc", "", "   ", None, "1.2.3", [1.0], {}, object()])


@_SETTINGS
@given(interval=_in_range)
def test_in_range_interval_is_preserved(monkeypatch, interval):
    _clear_bjp_env(monkeypatch)
    config = _build_config(interval)

    validated = core.validate_config(config)

    assert validated.monitoring_interval == float(interval)


@_SETTINGS
@given(interval=_out_of_range)
def test_out_of_range_interval_coerces_to_default(monkeypatch, interval):
    _clear_bjp_env(monkeypatch)
    config = _build_config(interval)

    validated = core.validate_config(config)

    assert validated.monitoring_interval == core.DEFAULT_MONITORING_INTERVAL


@_SETTINGS
@given(interval=_non_numeric)
def test_non_numeric_interval_coerces_to_default(monkeypatch, interval):
    _clear_bjp_env(monkeypatch)
    config = _build_config(interval)

    validated = core.validate_config(config)

    assert validated.monitoring_interval == core.DEFAULT_MONITORING_INTERVAL
