"""Property-based test for execution-mode defaulting and validation.

Implements Property 26 from the design: an unconfigured execution mode defaults
to sandbox (Req 16.1), any supported value (``ExecutionMode`` enum or a
case-insensitive ``"sandbox"``/``"live"`` string) maps to the correct enum, and
an unsupported value causes ``validate_config`` to reject the configuration with
a ``ConfigError`` (Req 16.5).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``. Each
test clears the ``BJP_*`` override environment variables (especially
``BJP_EXECUTION_MODE``) via monkeypatch so an operator's environment cannot
skew the in-script value under test.
"""

from __future__ import annotations

import bjp_core as core
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 26: Execution mode defaults to
# sandbox. When ``execution_mode`` is unconfigured (None), validate_config
# normalizes it to ExecutionMode.SANDBOX; a supported enum or case-insensitive
# "sandbox"/"live" string maps to the matching enum; any unsupported value is
# rejected with a ConfigError naming execution_mode. Validates: Requirements
# 16.1, 16.5.

_SUPPRESS_FIXTURE = [HealthCheck.function_scoped_fixture]


def _clear_bjp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``BJP_*`` override var so in-script values are honored."""
    for env_name in core.ENV_OVERRIDES.values():
        monkeypatch.delenv(env_name, raising=False)


def _cased(word: str) -> st.SearchStrategy[str]:
    """Generate arbitrary upper/lower case variations of ``word``."""
    return st.lists(
        st.booleans(), min_size=len(word), max_size=len(word)
    ).map(
        lambda flags: "".join(
            ch.upper() if flag else ch.lower()
            for ch, flag in zip(word, flags, strict=True)
        )
    )


def _padded(strategy: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    """Optionally surround a generated string with insignificant whitespace."""
    pad = st.sampled_from(["", " ", "  ", "\t"])
    return st.builds(lambda lead, body, trail: f"{lead}{body}{trail}", pad, strategy, pad)


def _make_config(execution_mode: object, lots: int, index: core.IndexSpec) -> core.StrategyConfig:
    """Build an otherwise-valid config with the given ``execution_mode``.

    ``execution_mode`` is typed loosely on purpose: validate_config accepts an
    ``ExecutionMode``, ``None``, or a string and coerces/validates it, so the
    tests deliberately feed it values the dataclass annotation would not.
    """
    config = core.StrategyConfig(
        strategy_name="PROP26",
        index=index,
        lots=lots,
        entry_time="09:16:00",
        exit_time="15:29:00",
    )
    config.execution_mode = execution_mode  # type: ignore[assignment]
    return config


# Supported values that must map to a specific ExecutionMode: the enums
# themselves plus case/whitespace variants of the canonical strings.
_VALID_MODE = st.one_of(
    st.just(core.ExecutionMode.SANDBOX),
    st.just(core.ExecutionMode.LIVE),
    _padded(_cased("sandbox")),
    _padded(_cased("live")),
)

# Unsupported values: strings that do not normalize to a supported mode, plus a
# few non-string types. All of these must be rejected (Req 16.5).
_INVALID_MODE = st.one_of(
    st.text(max_size=12).filter(
        lambda s: s.strip().lower() not in ("sandbox", "live")
    ),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)


@settings(max_examples=100, suppress_health_check=_SUPPRESS_FIXTURE)
@given(
    lots=st.integers(min_value=core.MIN_LOTS, max_value=core.MAX_LOTS),
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
)
def test_execution_mode_defaults_to_sandbox_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    lots: int,
    index: core.IndexSpec,
):
    """(a) An unconfigured (None) execution mode normalizes to SANDBOX (Req 16.1)."""
    _clear_bjp_env(monkeypatch)

    config = _make_config(None, lots, index)
    validated = core.validate_config(config)

    assert validated.execution_mode is core.ExecutionMode.SANDBOX


@settings(max_examples=100, suppress_health_check=_SUPPRESS_FIXTURE)
@given(
    mode=_VALID_MODE,
    lots=st.integers(min_value=core.MIN_LOTS, max_value=core.MAX_LOTS),
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
)
def test_execution_mode_maps_supported_values_to_enum(
    monkeypatch: pytest.MonkeyPatch,
    mode: object,
    lots: int,
    index: core.IndexSpec,
):
    """(b) Supported enums and case-insensitive strings map to the right enum."""
    _clear_bjp_env(monkeypatch)

    config = _make_config(mode, lots, index)
    validated = core.validate_config(config)

    if isinstance(mode, core.ExecutionMode):
        expected = mode
    else:
        expected = core.ExecutionMode(mode.strip().lower())

    assert validated.execution_mode is expected
    assert isinstance(validated.execution_mode, core.ExecutionMode)


@settings(max_examples=100, suppress_health_check=_SUPPRESS_FIXTURE)
@given(
    mode=_INVALID_MODE,
    lots=st.integers(min_value=core.MIN_LOTS, max_value=core.MAX_LOTS),
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
)
def test_execution_mode_invalid_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    mode: object,
    lots: int,
    index: core.IndexSpec,
):
    """(c) An unsupported execution mode is rejected with ConfigError (Req 16.5)."""
    _clear_bjp_env(monkeypatch)

    config = _make_config(mode, lots, index)

    with pytest.raises(core.ConfigError):
        core.validate_config(config)
