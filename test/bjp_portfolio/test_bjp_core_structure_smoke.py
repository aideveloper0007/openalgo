"""Structure and import smoke test (Task 23.2).

Verifies:

* All 10 strategy scripts exist in ``strategies/bjp_portfolio/``.
* Each script exposes a ``CONFIG`` attribute of type ``StrategyConfig``.
* Each ``CONFIG`` has exactly 2 legs (CE + PE).
* Each script's ``CONFIG`` passes ``validate_config()`` without error.
* ``bjp_core`` exports the required public API surface (``run``, ``StrategyConfig``,
  ``StrategyEngine`` if defined, ``validate_config``, etc.).

Production code lives in ``strategies/bjp_portfolio/bjp_core.py`` and is
imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 1.1, 1.3, 22.1–22.5.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import bjp_core as core
import pytest

# Ensure strategy scripts directory is on sys.path.
_BJP_DIR = Path(__file__).resolve().parents[2] / "strategies" / "bjp_portfolio"
if str(_BJP_DIR) not in sys.path:
    sys.path.insert(0, str(_BJP_DIR))

# The canonical list of the 10 per-strategy scripts.
STRATEGY_MODULES = [
    "nf_hedge",
    "sensex_hedge",
    "nf1",
    "sensex1",
    "nf2_mean_reversion",
    "sensex2_mean_reversion",
    "nf3_adjustable_strangle",
    "sensex3_adjustable_strangle",
    "nifty_1dte",
    "sensex_1dte",
]


@pytest.mark.parametrize("module_name", STRATEGY_MODULES)
def test_script_file_exists(module_name: str) -> None:
    """Each strategy script file exists in the bjp_portfolio directory."""
    script = _BJP_DIR / f"{module_name}.py"
    assert script.exists(), f"Missing strategy script: {script}"


@pytest.mark.parametrize("module_name", STRATEGY_MODULES)
def test_script_has_config_attribute(module_name: str) -> None:
    """Each strategy script exposes a CONFIG of type StrategyConfig."""
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "CONFIG"), f"{module_name} has no CONFIG attribute"
    assert isinstance(mod.CONFIG, core.StrategyConfig), (
        f"{module_name}.CONFIG is not a StrategyConfig"
    )


@pytest.mark.parametrize("module_name", STRATEGY_MODULES)
def test_config_has_two_legs_ce_pe(module_name: str) -> None:
    """Each CONFIG has exactly 2 legs: one CE and one PE."""
    mod = importlib.import_module(module_name)
    config = mod.CONFIG

    assert len(config.legs) == 2, f"{module_name}: expected 2 legs, got {len(config.legs)}"
    option_types = {leg.option_type for leg in config.legs}
    assert option_types == {core.OptionType.CE, core.OptionType.PE}, (
        f"{module_name}: expected CE+PE, got {option_types}"
    )


@pytest.mark.parametrize("module_name", STRATEGY_MODULES)
def test_config_passes_validation(module_name: str) -> None:
    """Each CONFIG passes validate_config() without raising ConfigError."""
    mod = importlib.import_module(module_name)
    config = mod.CONFIG

    # Should not raise
    validated = core.validate_config(config)
    assert validated is not None


def test_bjp_core_exports_run() -> None:
    """bjp_core exposes the run() entry-point."""
    assert callable(getattr(core, "run", None)), "bjp_core.run is not callable"


def test_bjp_core_exports_strategy_config() -> None:
    """bjp_core exposes the StrategyConfig dataclass."""
    assert hasattr(core, "StrategyConfig")


def test_bjp_core_exports_validate_config() -> None:
    """bjp_core exposes validate_config()."""
    assert callable(getattr(core, "validate_config", None))


def test_bjp_core_exports_resolve_environment() -> None:
    """bjp_core exposes resolve_environment()."""
    assert callable(getattr(core, "resolve_environment", None))


def test_bjp_core_exports_configure_logging() -> None:
    """bjp_core exposes configure_logging()."""
    assert callable(getattr(core, "configure_logging", None))


def test_bjp_core_exports_log_event() -> None:
    """bjp_core exposes log_event()."""
    assert callable(getattr(core, "log_event", None))


def test_bjp_core_exports_monitor() -> None:
    """bjp_core exposes monitor()."""
    assert callable(getattr(core, "monitor", None))


def test_bjp_core_exports_place_entry() -> None:
    """bjp_core exposes place_entry()."""
    assert callable(getattr(core, "place_entry", None))


def test_bjp_core_exports_index_registry() -> None:
    """bjp_core exposes NIFTY, SENSEX, and INDEX_REGISTRY."""
    assert hasattr(core, "NIFTY")
    assert hasattr(core, "SENSEX")
    assert hasattr(core, "INDEX_REGISTRY")
    assert "NIFTY" in core.INDEX_REGISTRY
    assert "SENSEX" in core.INDEX_REGISTRY


def test_exactly_10_strategy_scripts_exist() -> None:
    """The bjp_portfolio directory contains exactly 10 strategy scripts."""
    scripts = sorted(
        p.stem
        for p in _BJP_DIR.glob("*.py")
        if p.stem not in ("bjp_core", "__init__", "conftest")
        and not p.stem.startswith("_")
    )
    assert len(scripts) == 10, (
        f"Expected 10 strategy scripts, found {len(scripts)}: {scripts}"
    )
    assert set(scripts) == set(STRATEGY_MODULES), (
        f"Unexpected mismatch between expected and found scripts"
    )
