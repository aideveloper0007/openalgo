"""Table-driven parameter-fidelity smoke test (Task 23.1).

Asserts each script's ``CONFIG`` matches the Per-Strategy Parameter Table from
the design document exactly: offset, lots, quantity, entry/exit, momentum, leg
SL flavor/value, trail I/S, re-entry kind/count, overall SL, overall trail I/S,
square-off-all, and re-entry cutoff.

Production scripts live in ``strategies/bjp_portfolio/`` and are importable
because ``conftest.py`` adds that directory to ``sys.path``.

Validates: Requirements 8.1, 8.2, 17.1, 19.1–19.10.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import bjp_core as core
import pytest

# Ensure the strategy scripts directory is on sys.path so they can be imported.
_BJP_DIR = Path(__file__).resolve().parents[2] / "strategies" / "bjp_portfolio"
if str(_BJP_DIR) not in sys.path:
    sys.path.insert(0, str(_BJP_DIR))


def _import_config(module_name: str) -> core.StrategyConfig:
    """Import a strategy module and return its CONFIG object."""
    mod = importlib.import_module(module_name)
    return mod.CONFIG


# Per-Strategy Parameter Table rows keyed by module name.
# Each row is a dict with the exact expected values.
PARAM_TABLE: list[tuple[str, dict[str, Any]]] = [
    ("nf_hedge", {
        "strategy_name_prefix": "NF",
        "index": core.NIFTY,
        "action": core.Action.BUY,
        "offset": "OTM20",
        "lots": 5,
        "quantity": 325,
        "entry_time": "09:24:00",
        "exit_time": "15:26:00",
        "momentum": None,
        "sl_kind": None,
        "sl_value": None,
        "trail_i": None,
        "trail_s": None,
        "reentry_kind": None,
        "reentry_count": None,
        "overall_sl": None,
        "overall_trail_i": None,
        "overall_trail_s": None,
        "square_off_all": False,
        "reentry_cutoff": None,
    }),
    ("sensex_hedge", {
        "strategy_name_prefix": "SENSEX",
        "index": core.SENSEX,
        "action": core.Action.BUY,
        "offset": "OTM20",
        "lots": 5,
        "quantity": 100,
        "entry_time": "09:24:00",
        "exit_time": "15:26:00",
        "momentum": None,
        "sl_kind": None,
        "sl_value": None,
        "trail_i": None,
        "trail_s": None,
        "reentry_kind": None,
        "reentry_count": None,
        "overall_sl": None,
        "overall_trail_i": None,
        "overall_trail_s": None,
        "square_off_all": False,
        "reentry_cutoff": None,
    }),
    ("nf1", {
        "strategy_name_prefix": "NF1",
        "index": core.NIFTY,
        "action": core.Action.SELL,
        "offset": "ITM2",
        "lots": 2,
        "quantity": 130,
        "entry_time": "09:25:00",
        "exit_time": "15:29:00",
        "momentum": None,
        "sl_kind": core.SLKind.POINTS,
        "sl_value": 15,
        "trail_i": 70,
        "trail_s": 40,
        "reentry_kind": core.ReentryKind.AT_COST,
        "reentry_count": 1,
        "overall_sl": 5200,
        "overall_trail_i": 6000,
        "overall_trail_s": 9000,
        "square_off_all": False,
        "reentry_cutoff": None,
    }),
    ("sensex1", {
        "strategy_name_prefix": "SENSEX1",
        "index": core.SENSEX,
        "action": core.Action.SELL,
        "offset": "ITM2",
        "lots": 2,
        "quantity": 40,
        "entry_time": "09:25:00",
        "exit_time": "15:29:00",
        "momentum": None,
        "sl_kind": core.SLKind.POINTS,
        "sl_value": 50,
        "trail_i": 230,
        "trail_s": 130,
        "reentry_kind": core.ReentryKind.AT_COST,
        "reentry_count": 1,
        "overall_sl": 4600,
        "overall_trail_i": 20000,
        "overall_trail_s": 30000,
        "square_off_all": False,
        "reentry_cutoff": None,
    }),
    ("nf2_mean_reversion", {
        "strategy_name_prefix": "NF2",
        "index": core.NIFTY,
        "action": core.Action.SELL,
        "offset": "ITM1",
        "lots": 2,
        "quantity": 130,
        "entry_time": "09:16:00",
        "exit_time": "15:29:00",
        "momentum": 10,
        "sl_kind": core.SLKind.PERCENTAGE,
        "sl_value": 20,
        "trail_i": 20,
        "trail_s": 2,
        "reentry_kind": core.ReentryKind.AT_COST,
        "reentry_count": 1,
        "overall_sl": 5600,
        "overall_trail_i": 6000,
        "overall_trail_s": 6000,
        "square_off_all": False,
        "reentry_cutoff": None,
    }),
    ("sensex2_mean_reversion", {
        "strategy_name_prefix": "SENSEX2",
        "index": core.SENSEX,
        "action": core.Action.SELL,
        "offset": "ITM1",
        "lots": 2,
        "quantity": 40,
        "entry_time": "09:16:00",
        "exit_time": "15:29:00",
        "momentum": 35,
        "sl_kind": core.SLKind.PERCENTAGE,
        "sl_value": 20,
        "trail_i": 65,
        "trail_s": 5,
        "reentry_kind": core.ReentryKind.AT_COST,
        "reentry_count": 1,
        "overall_sl": 4900,
        "overall_trail_i": 20000,
        "overall_trail_s": 20000,
        "square_off_all": False,
        "reentry_cutoff": None,
    }),
    ("nf3_adjustable_strangle", {
        "strategy_name_prefix": "NF3",
        "index": core.NIFTY,
        "action": core.Action.SELL,
        "offset": "OTM6",
        "lots": 1,
        "quantity": 65,
        "entry_time": "09:16:00",
        "exit_time": "15:22:00",
        "momentum": None,
        "sl_kind": core.SLKind.UNDERLYING_POINTS,
        "sl_value": 100,
        "trail_i": None,
        "trail_s": None,
        "reentry_kind": core.ReentryKind.IMMEDIATE,
        "reentry_count": 3,
        "overall_sl": 1750,
        "overall_trail_i": 2000,
        "overall_trail_s": 2000,
        "square_off_all": True,
        "reentry_cutoff": 284,
    }),
    ("sensex3_adjustable_strangle", {
        "strategy_name_prefix": "SENSEX3",
        "index": core.SENSEX,
        "action": core.Action.SELL,
        "offset": "OTM6",
        "lots": 1,
        "quantity": 20,
        "entry_time": "09:16:00",
        "exit_time": "15:22:00",
        "momentum": None,
        "sl_kind": core.SLKind.UNDERLYING_POINTS,
        "sl_value": 350,
        "trail_i": None,
        "trail_s": None,
        "reentry_kind": core.ReentryKind.IMMEDIATE,
        "reentry_count": 3,
        "overall_sl": 1500,
        "overall_trail_i": 6600,
        "overall_trail_s": 6600,
        "square_off_all": True,
        "reentry_cutoff": 284,
    }),
    ("nifty_1dte", {
        "strategy_name_prefix": "NIFTY",
        "index": core.NIFTY,
        "action": core.Action.SELL,
        "offset": "OTM10",
        "lots": 5,
        "quantity": 325,
        "entry_time": "09:18:00",
        "exit_time": "15:23:00",
        "momentum": None,
        "sl_kind": core.SLKind.UNDERLYING_POINTS,
        "sl_value": 100,
        "trail_i": None,
        "trail_s": None,
        "reentry_kind": core.ReentryKind.IMMEDIATE,
        "reentry_count": 5,
        "overall_sl": 5000,
        "overall_trail_i": None,
        "overall_trail_s": None,
        "square_off_all": True,
        "reentry_cutoff": None,
    }),
    ("sensex_1dte", {
        "strategy_name_prefix": "SENSEX",
        "index": core.SENSEX,
        "action": core.Action.SELL,
        "offset": "OTM18",
        "lots": 5,
        "quantity": 100,
        "entry_time": "09:18:00",
        "exit_time": "15:23:00",
        "momentum": None,
        "sl_kind": core.SLKind.UNDERLYING_POINTS,
        "sl_value": 350,
        "trail_i": None,
        "trail_s": None,
        "reentry_kind": core.ReentryKind.IMMEDIATE,
        "reentry_count": 5,
        "overall_sl": 5000,
        "overall_trail_i": None,
        "overall_trail_s": None,
        "square_off_all": True,
        "reentry_cutoff": None,
    }),
]


@pytest.mark.parametrize(
    "module_name,expected",
    PARAM_TABLE,
    ids=[row[0] for row in PARAM_TABLE],
)
def test_config_matches_parameter_table(
    module_name: str, expected: dict[str, Any]
) -> None:
    """Assert CONFIG matches the Per-Strategy Parameter Table exactly."""
    config = _import_config(module_name)

    # --- Index and lots ---
    assert config.index is expected["index"], f"{module_name}: wrong index"
    assert config.lots == expected["lots"], f"{module_name}: wrong lots"
    assert config.quantity == expected["quantity"], f"{module_name}: wrong quantity"

    # --- Entry/exit times ---
    assert config.entry_time == expected["entry_time"], f"{module_name}: wrong entry"
    assert config.exit_time == expected["exit_time"], f"{module_name}: wrong exit"

    # --- Legs: both CE and PE share the same offset/action/risk config ---
    assert len(config.legs) == 2, f"{module_name}: expected exactly 2 legs"
    ce_leg = next(l for l in config.legs if l.option_type == core.OptionType.CE)
    pe_leg = next(l for l in config.legs if l.option_type == core.OptionType.PE)

    for leg, label in [(ce_leg, "CE"), (pe_leg, "PE")]:
        assert leg.action == expected["action"], f"{module_name}/{label}: wrong action"
        assert leg.offset == expected["offset"], f"{module_name}/{label}: wrong offset"

        # Leg stop loss
        if expected["sl_kind"] is None:
            assert leg.stop_loss is None, f"{module_name}/{label}: expected no SL"
        else:
            assert leg.stop_loss is not None, f"{module_name}/{label}: expected SL"
            assert leg.stop_loss.kind == expected["sl_kind"], (
                f"{module_name}/{label}: wrong SL kind"
            )
            assert leg.stop_loss.value == expected["sl_value"], (
                f"{module_name}/{label}: wrong SL value"
            )

        # Leg trail
        if expected["trail_i"] is None:
            assert leg.trail_sl is None, f"{module_name}/{label}: expected no trail"
        else:
            assert leg.trail_sl is not None, f"{module_name}/{label}: expected trail"
            assert leg.trail_sl.instrument_move == expected["trail_i"], (
                f"{module_name}/{label}: wrong trail I"
            )
            assert leg.trail_sl.stoploss_move == expected["trail_s"], (
                f"{module_name}/{label}: wrong trail S"
            )

        # Momentum
        if expected["momentum"] is None:
            assert leg.momentum is None, f"{module_name}/{label}: expected no momentum"
        else:
            assert leg.momentum is not None, f"{module_name}/{label}: expected momentum"
            assert leg.momentum.points_down == expected["momentum"], (
                f"{module_name}/{label}: wrong momentum points"
            )

        # Re-entry
        if expected["reentry_kind"] is None:
            assert leg.reentry is None, f"{module_name}/{label}: expected no reentry"
        else:
            assert leg.reentry is not None, f"{module_name}/{label}: expected reentry"
            assert leg.reentry.kind == expected["reentry_kind"], (
                f"{module_name}/{label}: wrong reentry kind"
            )
            assert leg.reentry.count == expected["reentry_count"], (
                f"{module_name}/{label}: wrong reentry count"
            )

    # --- Overall stop loss ---
    if expected["overall_sl"] is None:
        assert config.overall_stop_loss is None, f"{module_name}: expected no overall SL"
    else:
        assert config.overall_stop_loss is not None, f"{module_name}: expected overall SL"
        assert config.overall_stop_loss.mtm_rupees == expected["overall_sl"], (
            f"{module_name}: wrong overall SL"
        )

    # --- Overall trail ---
    if expected["overall_trail_i"] is None:
        assert config.overall_trail_sl is None, f"{module_name}: expected no overall trail"
    else:
        assert config.overall_trail_sl is not None, (
            f"{module_name}: expected overall trail"
        )
        assert config.overall_trail_sl.instrument_move == expected["overall_trail_i"], (
            f"{module_name}: wrong overall trail I"
        )
        assert config.overall_trail_sl.stoploss_move == expected["overall_trail_s"], (
            f"{module_name}: wrong overall trail S"
        )

    # --- Square-off-all ---
    assert config.square_off_all_legs == expected["square_off_all"], (
        f"{module_name}: wrong square_off_all"
    )

    # --- Re-entry cutoff ---
    assert config.reentry_time_restriction_min == expected["reentry_cutoff"], (
        f"{module_name}: wrong reentry cutoff"
    )
