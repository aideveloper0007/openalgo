"""Property-based test for environment-variable configuration overrides.

Implements Property 4 from the design: for any configuration key that also has
a corresponding environment variable set to a valid value, the effective
configuration value equals the environment value rather than the in-script
default (Req 3.7).

The production code under test is ``validate_config()`` in
``strategies/bjp_portfolio/bjp_core.py``, imported as ``bjp_core`` via the
sys.path setup in ``conftest.py``. ``validate_config`` applies the environment
overrides in place before validation and returns the normalized config.

Override scheme (from :data:`bjp_core.ENV_OVERRIDES`):
    * ``BJP_LOTS`` -> ``lots`` (int)
    * ``BJP_INDEX`` -> ``index`` (NIFTY/SENSEX, case-insensitive)
    * ``BJP_ENTRY_TIME`` -> ``entry_time`` (HH:MM:SS)
    * ``BJP_EXIT_TIME`` -> ``exit_time`` (HH:MM:SS)
    * ``BJP_EXECUTION_MODE`` -> ``execution_mode`` (sandbox/live)
    * ``BJP_MONITORING_INTERVAL`` -> ``monitoring_interval`` (seconds)
    * ``BJP_PRODUCT`` -> ``product``

Generated override values are always valid and always differ from the fixed
in-script defaults, so a passing assertion proves the environment value took
precedence rather than coinciding with the default. Entry precedes exit and
lots stay in range so validation passes.
"""

from __future__ import annotations

import os

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 4: Environment overrides take
# precedence over in-script defaults. For any configuration key with a
# corresponding environment variable set to a valid value, the effective
# configuration value after validate_config equals the environment value rather
# than the in-script default. Validates: Requirements 3.7.

# Fixed in-script defaults, chosen to differ from every generated override value
# so a match proves the override (not a coincidence with the default).
_BASE_LOTS = 3
_BASE_ENTRY = "08:00:00"
_BASE_EXIT = "16:00:00"
_BASE_INTERVAL = 5.0
_BASE_PRODUCT = "BASEDEFAULT"

# Managed override env vars (the BJP_* scheme). Saved/restored around each run.
_MANAGED_VARS = tuple(core.ENV_OVERRIDES.values())

# Valid entry/exit time strings; every entry hour (<= 11) precedes every exit
# hour (>= 12), so exit_time > entry_time always holds. None collide with the
# base times above.
_ENTRY_TIMES = ["09:16:00", "09:18:30", "10:30:15", "11:45:59"]
_EXIT_TIMES = ["12:00:00", "14:20:10", "15:22:00", "15:29:45"]

# Case-varied valid index names and their expected canonical spec.
_INDEX_NAMES = ["NIFTY", "SENSEX", "nifty", "sensex", "NiFtY", "SeNsEx"]

# Case-varied valid execution-mode names.
_MODE_NAMES = ["sandbox", "live", "SANDBOX", "LIVE", "Sandbox", "Live"]

# Valid monitoring-interval strings within 0.1-60s inclusive; none equal 5.0.
_INTERVAL_VALUES = ["0.1", "0.5", "1.5", "10", "30", "60"]

# Product override values; none equal the base default.
_PRODUCT_VALUES = ["NRML", "MIS", "CNC", "BO"]


def _apply_env(values: dict[str, str | None]) -> None:
    """Set or delete each managed var according to ``values`` (None deletes)."""
    for name in _MANAGED_VARS:
        value = values.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@settings(max_examples=100)
@given(
    env_lots=st.integers(min_value=1, max_value=100).filter(lambda n: n != _BASE_LOTS),
    env_index_name=st.sampled_from(_INDEX_NAMES),
    env_entry=st.sampled_from(_ENTRY_TIMES),
    env_exit=st.sampled_from(_EXIT_TIMES),
    env_mode_name=st.sampled_from(_MODE_NAMES),
    env_interval=st.sampled_from(_INTERVAL_VALUES),
    env_product=st.sampled_from(_PRODUCT_VALUES),
)
def test_environment_overrides_take_precedence(
    env_lots: int,
    env_index_name: str,
    env_entry: str,
    env_exit: str,
    env_mode_name: str,
    env_interval: str,
    env_product: str,
):
    expected_index = core.INDEX_REGISTRY[env_index_name.upper()]
    expected_mode = core.ExecutionMode(env_mode_name.lower())
    expected_interval = float(env_interval)

    # Base defaults deliberately differ from every override value: the opposite
    # index and opposite mode, plus sentinel lots/times/interval/product.
    base_index = core.SENSEX if expected_index is core.NIFTY else core.NIFTY
    base_mode = (
        core.ExecutionMode.LIVE
        if expected_mode is core.ExecutionMode.SANDBOX
        else core.ExecutionMode.SANDBOX
    )

    config = core.StrategyConfig(
        strategy_name="PROP4",
        index=base_index,
        lots=_BASE_LOTS,
        entry_time=_BASE_ENTRY,
        exit_time=_BASE_EXIT,
        execution_mode=base_mode,
        monitoring_interval=_BASE_INTERVAL,
        product=_BASE_PRODUCT,
    )

    saved = {name: os.environ.get(name) for name in _MANAGED_VARS}
    try:
        _apply_env(
            {
                core.ENV_OVERRIDES["lots"]: str(env_lots),
                core.ENV_OVERRIDES["index"]: env_index_name,
                core.ENV_OVERRIDES["entry_time"]: env_entry,
                core.ENV_OVERRIDES["exit_time"]: env_exit,
                core.ENV_OVERRIDES["execution_mode"]: env_mode_name,
                core.ENV_OVERRIDES["monitoring_interval"]: env_interval,
                core.ENV_OVERRIDES["product"]: env_product,
            }
        )

        result = core.validate_config(config)

        # validate_config normalizes in place and returns the same instance.
        assert result is config

        # Each effective value equals the environment override, not the default.
        assert result.lots == env_lots
        assert result.index is expected_index
        assert result.entry_time == env_entry
        assert result.exit_time == env_exit
        assert result.execution_mode == expected_mode
        assert result.monitoring_interval == expected_interval
        assert result.product == env_product

        # And each override genuinely differs from the in-script default, so the
        # equalities above demonstrate precedence rather than coincidence.
        assert result.lots != _BASE_LOTS
        assert result.index is not base_index
        assert result.entry_time != _BASE_ENTRY
        assert result.exit_time != _BASE_EXIT
        assert result.execution_mode != base_mode
        assert result.monitoring_interval != _BASE_INTERVAL
        assert result.product != _BASE_PRODUCT
    finally:
        _apply_env(saved)
