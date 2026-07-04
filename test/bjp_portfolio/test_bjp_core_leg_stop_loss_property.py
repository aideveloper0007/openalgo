"""Property-based test for leg stop-loss and trailing-stop thresholds.

Implements Property 15 from the design: a short (SELL) leg's stop loss and its
active trailing stop each trigger exactly at their configured threshold, with
**inclusive** comparisons (Req 10.1-10.4, 11.4). The four static/underlying
flavors and the active-trailing case are exercised:

    * ``Points`` (P):            premium >= entry_fill + P
    * ``Percentage`` (X):        premium >= entry_fill * (1 + X / 100)
    * ``UnderlyingPoints`` short CE: spot >= entry_spot + U
    * ``UnderlyingPoints`` short PE: spot <= entry_spot - U
    * active trailing stop:      premium >= trail_level

For each case the predicate must equal the direct threshold comparison across
all inputs, and the boundary (value exactly at the threshold) must trigger.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 15: Leg stop loss triggers exactly
# at its threshold. For a short leg configured with each of the three static
# flavors (Points, Percentage, UnderlyingPoints for CE and PE) plus an active
# trailing stop, leg_stop_loss_triggered / leg_trail_triggered returns True iff
# the current premium/spot has reached the (inclusive) threshold: for the
# premium flavors and the trail, value >= threshold; for a short PE
# UnderlyingPoints, spot <= threshold. The boundary value exactly at the
# threshold must trigger.
# Validates: Requirements 10.1, 10.2, 10.3, 10.4, 11.4.

# Positive, finite premium/spot magnitudes bounded well below float precision
# loss so an exact-at-threshold equality remains representable.
_prices = st.floats(
    min_value=1.0,
    max_value=50_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Test premium/spot values may sit anywhere from zero up past the threshold.
_test_values = st.floats(
    min_value=0.0,
    max_value=100_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Stop-loss "value" magnitudes: P points, U underlying points, or S/I moves.
_sl_values = st.floats(
    min_value=0.0,
    max_value=5_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Percentage X: 0%..500% adverse move.
_percentages = st.floats(
    min_value=0.0,
    max_value=500.0,
    allow_nan=False,
    allow_infinity=False,
)


def _short_leg(
    kind: core.SLKind,
    value: float,
    option_type: core.OptionType,
    entry_fill: float | None = None,
    entry_spot: float | None = None,
    with_trail: bool = False,
) -> core.LegState:
    """Build an open short (SELL) ``LegState`` with the given stop-loss flavor."""
    trail = core.LegTrailSL(instrument_move=10.0, stoploss_move=5.0) if with_trail else None
    return core.LegState(
        config=core.LegConfig(
            option_type=option_type,
            action=core.Action.SELL,
            offset="ATM",
            stop_loss=core.LegStopLoss(kind=kind, value=value),
            trail_sl=trail,
        ),
        entry_fill=entry_fill,
        entry_spot=entry_spot,
        is_open=True,
    )


# ---------------------------------------------------------------------------
# Points flavor: premium >= entry_fill + P
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(entry_fill=_prices, points=_sl_values, premium=_test_values)
def test_points_leg_sl_triggers_at_inclusive_threshold(
    entry_fill: float, points: float, premium: float
):
    leg = _short_leg(core.SLKind.POINTS, points, core.OptionType.CE, entry_fill=entry_fill)
    threshold = entry_fill + points
    assert core.leg_stop_loss_triggered(leg, premium=premium) == (premium >= threshold)


@settings(max_examples=200)
@given(entry_fill=_prices, points=_sl_values)
def test_points_leg_sl_triggers_exactly_at_threshold(entry_fill: float, points: float):
    leg = _short_leg(core.SLKind.POINTS, points, core.OptionType.CE, entry_fill=entry_fill)
    threshold = entry_fill + points
    # Premium exactly at threshold triggers (inclusive); just below does not.
    assert core.leg_stop_loss_triggered(leg, premium=threshold) is True
    assert core.leg_stop_loss_triggered(leg, premium=threshold - 1.0) is False


# ---------------------------------------------------------------------------
# Percentage flavor: premium >= entry_fill * (1 + X / 100)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(entry_fill=_prices, pct=_percentages, premium=_test_values)
def test_percentage_leg_sl_triggers_at_inclusive_threshold(
    entry_fill: float, pct: float, premium: float
):
    leg = _short_leg(core.SLKind.PERCENTAGE, pct, core.OptionType.PE, entry_fill=entry_fill)
    threshold = entry_fill * (1.0 + pct / 100.0)
    assert core.leg_stop_loss_triggered(leg, premium=premium) == (premium >= threshold)


@settings(max_examples=200)
@given(entry_fill=_prices, pct=_percentages)
def test_percentage_leg_sl_triggers_exactly_at_threshold(entry_fill: float, pct: float):
    leg = _short_leg(core.SLKind.PERCENTAGE, pct, core.OptionType.PE, entry_fill=entry_fill)
    threshold = entry_fill * (1.0 + pct / 100.0)
    assert core.leg_stop_loss_triggered(leg, premium=threshold) is True


# ---------------------------------------------------------------------------
# UnderlyingPoints short CE: spot >= entry_spot + U
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(entry_spot=_prices, under=_sl_values, spot=_test_values)
def test_underlying_ce_leg_sl_triggers_at_inclusive_threshold(
    entry_spot: float, under: float, spot: float
):
    leg = _short_leg(
        core.SLKind.UNDERLYING_POINTS,
        under,
        core.OptionType.CE,
        entry_spot=entry_spot,
    )
    threshold = entry_spot + under
    assert core.leg_stop_loss_triggered(leg, spot=spot) == (spot >= threshold)


@settings(max_examples=200)
@given(entry_spot=_prices, under=_sl_values)
def test_underlying_ce_leg_sl_triggers_exactly_at_threshold(
    entry_spot: float, under: float
):
    leg = _short_leg(
        core.SLKind.UNDERLYING_POINTS,
        under,
        core.OptionType.CE,
        entry_spot=entry_spot,
    )
    threshold = entry_spot + under
    assert core.leg_stop_loss_triggered(leg, spot=threshold) is True
    assert core.leg_stop_loss_triggered(leg, spot=threshold - 1.0) is False


# ---------------------------------------------------------------------------
# UnderlyingPoints short PE: spot <= entry_spot - U
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(entry_spot=_prices, under=_sl_values, spot=_test_values)
def test_underlying_pe_leg_sl_triggers_at_inclusive_threshold(
    entry_spot: float, under: float, spot: float
):
    leg = _short_leg(
        core.SLKind.UNDERLYING_POINTS,
        under,
        core.OptionType.PE,
        entry_spot=entry_spot,
    )
    threshold = entry_spot - under
    assert core.leg_stop_loss_triggered(leg, spot=spot) == (spot <= threshold)


@settings(max_examples=200)
@given(entry_spot=_prices, under=_sl_values)
def test_underlying_pe_leg_sl_triggers_exactly_at_threshold(
    entry_spot: float, under: float
):
    leg = _short_leg(
        core.SLKind.UNDERLYING_POINTS,
        under,
        core.OptionType.PE,
        entry_spot=entry_spot,
    )
    threshold = entry_spot - under
    assert core.leg_stop_loss_triggered(leg, spot=threshold) is True
    assert core.leg_stop_loss_triggered(leg, spot=threshold + 1.0) is False


# ---------------------------------------------------------------------------
# Active trailing stop: premium >= trail_level
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(trail_level=_prices, premium=_test_values)
def test_active_trail_triggers_at_inclusive_threshold(
    trail_level: float, premium: float
):
    leg = _short_leg(
        core.SLKind.POINTS,
        15.0,
        core.OptionType.CE,
        entry_fill=100.0,
        with_trail=True,
    )
    leg.trail_level = trail_level
    assert core.leg_trail_triggered(leg, premium=premium) == (premium >= trail_level)


@settings(max_examples=200)
@given(trail_level=_prices)
def test_active_trail_triggers_exactly_at_threshold(trail_level: float):
    leg = _short_leg(
        core.SLKind.POINTS,
        15.0,
        core.OptionType.CE,
        entry_fill=100.0,
        with_trail=True,
    )
    leg.trail_level = trail_level
    # Premium exactly at the trail level triggers (inclusive); just below does not.
    assert core.leg_trail_triggered(leg, premium=trail_level) is True
    assert core.leg_trail_triggered(leg, premium=trail_level - 1.0) is False
