"""Property-based test for per-leg and aggregate mark-to-market P&L.

Implements Property 20 from the design: the aggregate MTM sums each open leg's
per-leg MTM with the correct sign convention (Req 13.4). For a ``SELL`` (short)
leg the P&L is ``(entry_fill - price) * quantity`` — it profits as the premium
falls; for a ``BUY`` (long) leg it is ``(price - entry_fill) * quantity`` — it
profits as the premium rises. The production code under test lives in
``strategies/bjp_portfolio/bjp_core.py`` and is imported as ``bjp_core`` via the
sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 20: Aggregate MTM has correct
# per-leg sign. For any mix of SELL and BUY legs with arbitrary entry fills,
# LTPs, and quantity, each leg's MTM equals (entry - price) * qty for a short
# leg and (price - entry) * qty for a long leg, and aggregate_mtm equals the
# sum of the per-leg MTM over the OPEN legs (skipping legs with undefined MTM).
# Validates: Requirements 13.4.

# Tolerance for float arithmetic in the summation comparisons.
_REL = 1e-9
_ABS = 1e-6

# Prices are bounded but wide enough to exercise both profit and loss regions.
_prices = st.floats(
    min_value=0.0,
    max_value=100_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# A single leg spec: action, option right, entry fill, current LTP, open flag.
_leg_specs = st.fixed_dictionaries(
    {
        "action": st.sampled_from([core.Action.SELL, core.Action.BUY]),
        "option_type": st.sampled_from([core.OptionType.CE, core.OptionType.PE]),
        "entry_fill": _prices,
        "last_ltp": _prices,
        "is_open": st.booleans(),
    }
)


def _make_leg(spec: dict) -> core.LegState:
    """Build a ``LegState`` from a generated spec dict."""
    return core.LegState(
        config=core.LegConfig(
            option_type=spec["option_type"],
            action=spec["action"],
            offset="ATM",
        ),
        entry_fill=spec["entry_fill"],
        last_ltp=spec["last_ltp"],
        is_open=spec["is_open"],
    )


def _make_engine(legs: list[core.LegState], lots: int, index: core.IndexSpec):
    """Build an ``EngineState`` wrapping ``legs`` with the given lots/index."""
    config = core.StrategyConfig(
        strategy_name="test",
        index=index,
        lots=lots,
        entry_time="09:20:00",
        exit_time="15:26:00",
        legs=[leg.config for leg in legs],
    )
    return core.EngineState(config=config, client=object(), legs=legs)


@settings(max_examples=100)
@given(
    specs=st.lists(_leg_specs, min_size=0, max_size=6),
    lots=st.integers(min_value=1, max_value=100),
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
)
def test_aggregate_mtm_has_correct_per_leg_sign(
    specs: list[dict], lots: int, index: core.IndexSpec
):
    legs = [_make_leg(spec) for spec in specs]
    engine = _make_engine(legs, lots, index)
    quantity = engine.config.quantity

    expected_total = 0.0
    for leg in legs:
        value = core.leg_mtm(leg, quantity)

        # Per-leg MTM matches the sign convention exactly (Req 13.4).
        action = core.Action(leg.config.action)
        if action is core.Action.SELL:
            expected = (leg.entry_fill - leg.last_ltp) * quantity
        else:
            expected = (leg.last_ltp - leg.entry_fill) * quantity
        assert value == pytest.approx(expected, rel=_REL, abs=_ABS)

        # Only OPEN legs contribute to the aggregate.
        if leg.is_open:
            expected_total += expected

    assert core.aggregate_mtm(engine) == pytest.approx(
        expected_total, rel=_REL, abs=_ABS
    )


@settings(max_examples=100)
@given(
    entry=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
    drop=st.floats(min_value=0.01, max_value=500.0, allow_nan=False),
    quantity=st.integers(min_value=1, max_value=10_000),
)
def test_short_leg_profits_when_ltp_below_entry(
    entry: float, drop: float, quantity: int
):
    # A short leg profits when the premium falls below the entry fill.
    ltp = entry - drop
    leg = _make_leg(
        {
            "action": core.Action.SELL,
            "option_type": core.OptionType.CE,
            "entry_fill": entry,
            "last_ltp": ltp,
            "is_open": True,
        }
    )
    value = core.leg_mtm(leg, quantity)
    assert value is not None
    assert value > 0
    assert value == pytest.approx((entry - ltp) * quantity, rel=_REL, abs=_ABS)


@settings(max_examples=100)
@given(
    entry=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
    rise=st.floats(min_value=0.01, max_value=500.0, allow_nan=False),
    quantity=st.integers(min_value=1, max_value=10_000),
)
def test_long_leg_profits_when_ltp_above_entry(
    entry: float, rise: float, quantity: int
):
    # A long leg profits when the premium rises above the entry fill.
    ltp = entry + rise
    leg = _make_leg(
        {
            "action": core.Action.BUY,
            "option_type": core.OptionType.PE,
            "entry_fill": entry,
            "last_ltp": ltp,
            "is_open": True,
        }
    )
    value = core.leg_mtm(leg, quantity)
    assert value is not None
    assert value > 0
    assert value == pytest.approx((ltp - entry) * quantity, rel=_REL, abs=_ABS)


def test_aggregate_skips_closed_and_undefined_legs():
    # A short leg profiting, plus a closed leg and an unpriced leg that must
    # not contribute to the aggregate.
    open_short = _make_leg(
        {
            "action": core.Action.SELL,
            "option_type": core.OptionType.CE,
            "entry_fill": 100.0,
            "last_ltp": 80.0,
            "is_open": True,
        }
    )
    closed_leg = _make_leg(
        {
            "action": core.Action.BUY,
            "option_type": core.OptionType.PE,
            "entry_fill": 50.0,
            "last_ltp": 90.0,
            "is_open": False,
        }
    )
    unpriced_open = core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="ATM",
        ),
        entry_fill=100.0,
        last_ltp=None,  # undefined MTM -> skipped
        is_open=True,
    )
    engine = _make_engine([open_short, closed_leg, unpriced_open], lots=1, index=core.NIFTY)
    quantity = engine.config.quantity

    # Only the open short leg contributes: (100 - 80) * quantity.
    assert core.aggregate_mtm(engine) == pytest.approx((100.0 - 80.0) * quantity)
    assert core.leg_mtm(unpriced_open, quantity) is None
    assert core.aggregate_mtm(_make_engine([], lots=1, index=core.NIFTY)) == 0.0
