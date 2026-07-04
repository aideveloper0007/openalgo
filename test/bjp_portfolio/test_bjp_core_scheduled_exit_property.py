"""Property-based test for the scheduled (Exit_Time) force-close.

Implements Property 25 from the design: when the current system time is at or
after the configured ``Exit_Time``, the scheduled exit squares off *every* open
leg of the strategy regardless of ``square_off_all_legs``, ``Overall_Stop_Loss``,
or ``Overall_Trail_SL`` state, and abandons any momentum leg still awaiting its
entry trigger — so the strategy is left completely flat (Req 15.3, and Req 9.5
for the never-entered momentum leg).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``. A
fresh in-memory fake OpenAlgo SDK client (whose ``placeorder`` returns a
success-shaped envelope) is built per generated example so square-offs confirm
without network cost.
"""

from __future__ import annotations

import datetime

import bjp_core as core
from conftest import FakeOpenAlgoClient
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 25: Scheduled exit closes
# everything. For any mix of open/closed/pending-momentum legs and any
# square_off_all_legs / overall stop-loss / overall trailing-lock state, once
# the system time reaches Exit_Time (boundary inclusive) perform_scheduled_exit
# closes every open leg and abandons every pending momentum leg, leaving the
# strategy flat with no legs reported still open.
# Validates: Requirements 15.3.

_EXIT_TIME = "15:26:00"
_EXIT_SECONDS = 15 * 3600 + 26 * 60  # seconds-of-day for 15:26:00
_LAST_SECOND = 24 * 3600 - 1  # 23:59:59

# Times at or after Exit_Time (the boundary second itself is included).
_at_or_after_exit = st.integers(
    min_value=_EXIT_SECONDS, max_value=_LAST_SECOND
).map(
    lambda secs: datetime.time(secs // 3600, (secs % 3600) // 60, secs % 60)
)

_option_types = st.sampled_from([core.OptionType.CE, core.OptionType.PE])
_actions = st.sampled_from([core.Action.SELL, core.Action.BUY])
_prices = st.one_of(
    st.none(),
    st.floats(min_value=0.05, max_value=5_000.0, allow_nan=False,
              allow_infinity=False),
)


@st.composite
def _leg_specs(draw: st.DrawFn) -> dict:
    """Draw one leg's runtime state: open/closed and/or pending momentum."""
    is_open = draw(st.booleans())
    # A pending-momentum leg is one awaiting entry, so it is not yet open.
    pending_momentum = False if is_open else draw(st.booleans())
    return {
        "option_type": draw(_option_types),
        "action": draw(_actions),
        "is_open": is_open,
        "pending_momentum": pending_momentum,
        "entry_fill": draw(_prices),
        "last_ltp": draw(_prices),
    }


def _build_leg(index: int, spec: dict) -> core.LegState:
    """Build a ``LegState`` from a drawn spec, giving it a unique symbol."""
    leg_config = core.LegConfig(
        option_type=spec["option_type"],
        action=spec["action"],
        offset="ATM",
    )
    return core.LegState(
        config=leg_config,
        symbol=f"NIFTY31DEC25{24000 + index}{spec['option_type'].value}",
        order_id=str(index),
        entry_fill=spec["entry_fill"],
        last_ltp=spec["last_ltp"],
        is_open=spec["is_open"],
        pending_momentum=spec["pending_momentum"],
    )


def _engine(
    legs: list[core.LegState],
    *,
    square_off_all_legs: bool,
    overall_sl: core.OverallStopLoss | None,
    overall_trail: core.OverallTrailSL | None,
    locked_mtm_stop: float | None,
    peak_mtm: float,
) -> core.EngineState:
    """Build an ``EngineState`` with a success-shaped fake client."""
    config = core.StrategyConfig(
        strategy_name="test",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time=_EXIT_TIME,
        legs=[leg.config for leg in legs],
        overall_stop_loss=overall_sl,
        overall_trail_sl=overall_trail,
        square_off_all_legs=square_off_all_legs,
    )
    engine = core.EngineState(config=config, client=FakeOpenAlgoClient(), legs=legs)
    engine.locked_mtm_stop = locked_mtm_stop
    engine.peak_mtm = peak_mtm
    return engine


@settings(max_examples=100)
@given(
    specs=st.lists(_leg_specs(), min_size=0, max_size=6),
    now=_at_or_after_exit,
    square_off_all_legs=st.booleans(),
    overall_sl=st.one_of(
        st.none(),
        st.floats(min_value=0.0, max_value=500_000.0, allow_nan=False,
                  allow_infinity=False).map(core.OverallStopLoss),
    ),
    overall_trail=st.booleans(),
    locked_mtm_stop=st.one_of(
        st.none(),
        st.floats(min_value=-500_000.0, max_value=500_000.0, allow_nan=False,
                  allow_infinity=False),
    ),
    peak_mtm=st.floats(min_value=-500_000.0, max_value=500_000.0,
                       allow_nan=False, allow_infinity=False),
)
def test_scheduled_exit_closes_every_open_leg(
    specs: list[dict],
    now: datetime.time,
    square_off_all_legs: bool,
    overall_sl: core.OverallStopLoss | None,
    overall_trail: bool,
    locked_mtm_stop: float | None,
    peak_mtm: float,
):
    legs = [_build_leg(i, spec) for i, spec in enumerate(specs)]
    trail = (
        core.OverallTrailSL(instrument_move=100.0, stoploss_move=50.0)
        if overall_trail
        else None
    )
    engine = _engine(
        legs,
        square_off_all_legs=square_off_all_legs,
        overall_sl=overall_sl,
        overall_trail=trail,
        locked_mtm_stop=locked_mtm_stop,
        peak_mtm=peak_mtm,
    )

    open_before = [leg for leg in legs if leg.is_open]

    # Req 15.3: the boundary itself (>= Exit_Time) must fire the scheduled exit.
    assert core.is_at_or_after_exit_time(engine.config, now) is True

    still_open = core.perform_scheduled_exit(engine)

    # Every open leg is squared off regardless of square_off_all_legs / overall
    # state, so none are reported still open and none remain open.
    assert still_open == []
    assert all(not leg.is_open for leg in legs)
    # Each previously open leg was force-closed with the scheduled-exit reason.
    for leg in open_before:
        assert leg.exit_reason == "scheduled_exit"
    # Pending momentum legs are abandoned (never entered), so the strategy is
    # left completely flat and the monitoring loop can wind down.
    assert all(not leg.pending_momentum for leg in legs)
    assert core.all_legs_closed(engine) is True
    # A square-off (placeorder) was submitted for each leg that was open.
    assert engine.client.call_count("placeorder") == len(open_before)


@settings(max_examples=100)
@given(
    square_off_all_legs=st.booleans(),
    overall_sl_level=st.floats(min_value=0.0, max_value=500_000.0,
                               allow_nan=False, allow_infinity=False),
)
def test_scheduled_exit_ignores_overall_state_and_scope(
    square_off_all_legs: bool, overall_sl_level: float
):
    # Even when square_off_all_legs is False (which would normally close only
    # designated legs on an overall trigger) and an overall stop loss is
    # configured, the scheduled exit still closes every open leg (Req 15.3).
    specs = [
        {"option_type": core.OptionType.CE, "action": core.Action.SELL,
         "is_open": True, "pending_momentum": False,
         "entry_fill": 100.0, "last_ltp": 120.0},
        {"option_type": core.OptionType.PE, "action": core.Action.SELL,
         "is_open": True, "pending_momentum": False,
         "entry_fill": 90.0, "last_ltp": 80.0},
    ]
    legs = [_build_leg(i, spec) for i, spec in enumerate(specs)]
    engine = _engine(
        legs,
        square_off_all_legs=square_off_all_legs,
        overall_sl=core.OverallStopLoss(mtm_rupees=overall_sl_level),
        overall_trail=core.OverallTrailSL(instrument_move=100.0, stoploss_move=50.0),
        locked_mtm_stop=-overall_sl_level,
        peak_mtm=0.0,
    )

    still_open = core.perform_scheduled_exit(engine)

    assert still_open == []
    assert all(not leg.is_open for leg in legs)


def test_is_at_or_after_exit_time_boundary():
    # Boundary: exactly at Exit_Time fires; one second before does not (Req 15.3).
    config = core.StrategyConfig(
        strategy_name="test",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time=_EXIT_TIME,
    )
    assert core.is_at_or_after_exit_time(config, datetime.time(15, 26, 0)) is True
    assert core.is_at_or_after_exit_time(config, datetime.time(15, 25, 59)) is False
    assert core.is_at_or_after_exit_time(config, datetime.time(15, 26, 1)) is True


@given(now=_at_or_after_exit)
@settings(max_examples=100)
@example(now=datetime.time(15, 26, 0))
def test_scheduled_exit_on_empty_strategy_is_flat(now: datetime.time):
    # With no legs at all the scheduled exit is a no-op that leaves the
    # strategy flat and reports nothing still open (Req 15.3, 15.4).
    engine = _engine(
        [],
        square_off_all_legs=False,
        overall_sl=None,
        overall_trail=None,
        locked_mtm_stop=None,
        peak_mtm=0.0,
    )
    assert core.is_at_or_after_exit_time(engine.config, now) is True
    assert core.perform_scheduled_exit(engine) == []
    assert core.all_legs_closed(engine) is True
