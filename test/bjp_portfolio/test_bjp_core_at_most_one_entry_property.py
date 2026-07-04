"""Property-based test for the at-most-one-entry-per-day guard.

Implements Property 10 from the design: *for any* number of entry-trigger
firings within a single trading day, :func:`bjp_core.place_entry` places the
strategy's entry position at most once, honouring the backtest
``MaxPositionInADay`` value of 1 (Req 6.3). Once a position exists for the day
(``EngineState.entered_today`` is set), every subsequent trigger is rejected and
the already-established position is left unchanged.

The test fires ``place_entry`` a generated number of times against a
success-shaped in-memory fake SDK client (so the first firing establishes a
real, filled position) and asserts:

    * exactly one firing — the first — returns ``True``; all later firings
      return ``False`` (the repeat triggers are rejected);
    * the underlying ``optionsmultiorder`` order primitive is invoked at most
      once across all firings (never once per trigger), and not at all when
      every leg is momentum-deferred;
    * ``entered_today`` becomes ``True`` on the first firing and stays ``True``;
    * the per-leg runtime state captured right after the first firing is
      byte-for-byte identical after all remaining firings (the existing
      position is left unchanged).

The generated space mixes SELL (short) and BUY (hedge) legs and momentum-gated
vs. immediate legs so the invariant is exercised across the all-immediate,
mixed, and all-momentum entry shapes.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from conftest import FakeOpenAlgoClient
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 10: At most one entry per trading
# day. For any number of entry-trigger firings within a single trading day,
# place_entry places the entry position at most once (MaxPositionInADay = 1),
# rejecting every trigger after the first and leaving the existing position
# unchanged. Validates: Requirements 6.3.

#: A fixed spot at entry, recorded as each filled leg's UnderlyingPoints
#: baseline; irrelevant to the count invariant but exercises the entry_spot path.
_ENTRY_SPOT = 24000.0

#: A fixed offset for generated legs; strike math is out of scope for Property 10.
_OFFSETS = ("ATM", "OTM6", "ITM2")


#: A single generated leg spec: ``(option_type, action, is_momentum)``. Actions
#: are limited to the two valid sides (SELL for shorts, BUY for hedges) so entry
#: is never rejected for an invalid action — this test is about the
#: one-entry-per-day guard, not action validation. The ``is_momentum`` flag
#: routes a leg down the deferred momentum path so the all-immediate, mixed, and
#: all-momentum entry shapes are all reachable.
_LEG_SPEC = st.tuples(
    st.sampled_from(list(core.OptionType)),
    st.sampled_from([core.Action.SELL, core.Action.BUY]),
    st.booleans(),
)

_LEG_SPECS = st.lists(_LEG_SPEC, min_size=1, max_size=4)


def _build_engine(leg_specs):
    """Build an ``EngineState`` (client attached later) from ``leg_specs``."""
    legs = [
        core.LegConfig(
            option_type=option_type,
            action=action,
            offset=_OFFSETS[idx % len(_OFFSETS)],
            momentum=core.LegMomentum(points_down=10.0) if is_momentum else None,
        )
        for idx, (option_type, action, is_momentum) in enumerate(leg_specs)
    ]
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=2,
        entry_time="09:20:00",
        exit_time="15:25:00",
        legs=legs,
    )
    return core.EngineState(config=config, client=None, expiry="31-DEC-25")


def _success_results(leg_specs):
    """Return an ``optionsmultiorder`` results list for the immediate legs.

    Only non-momentum legs are placed in the entry order, so the results list
    is sized to the immediate legs, each carrying a unique traded symbol and
    order id so the first entry fills cleanly.
    """
    immediate = [spec for spec in leg_specs if not spec[2]]
    return [
        {"symbol": f"NIFTY31DEC25{24000 + i}CE", "orderid": f"oid-{i}"}
        for i, _ in enumerate(immediate)
    ]


def _snapshot(engine):
    """Capture the observable per-leg runtime state for equality comparison."""
    return [
        (
            leg.symbol,
            leg.order_id,
            leg.entry_fill,
            leg.entry_spot,
            leg.is_open,
            leg.pending_momentum,
            leg.reentries_done,
            leg.trail_level,
        )
        for leg in engine.legs
    ]


@settings(max_examples=100)
@given(leg_specs=_LEG_SPECS, num_triggers=st.integers(min_value=2, max_value=8))
# All-immediate two-leg short strangle fired several times.
@example(
    leg_specs=[
        (core.OptionType.CE, core.Action.SELL, False),
        (core.OptionType.PE, core.Action.SELL, False),
    ],
    num_triggers=4,
)
# Every leg momentum-deferred: entry initiated with no optionsmultiorder call.
@example(
    leg_specs=[
        (core.OptionType.CE, core.Action.SELL, True),
        (core.OptionType.PE, core.Action.SELL, True),
    ],
    num_triggers=3,
)
# Mixed hedge BUY + short SELL, one momentum leg.
@example(
    leg_specs=[
        (core.OptionType.CE, core.Action.BUY, False),
        (core.OptionType.PE, core.Action.SELL, True),
    ],
    num_triggers=5,
)
def test_at_most_one_entry_per_trading_day(leg_specs, num_triggers):
    # Build a fresh fake client per generated input so no call state leaks
    # across examples (see the sibling entry property tests for this pattern).
    client = FakeOpenAlgoClient()
    client.optionsmultiorder_response = {
        "status": "success",
        "results": _success_results(leg_specs),
    }
    engine = _build_engine(leg_specs)
    engine.client = client

    all_momentum = all(spec[2] for spec in leg_specs)

    results = []
    snapshot_after_first = None
    for firing in range(num_triggers):
        placed = core.place_entry(engine, spot=_ENTRY_SPOT, sleep=lambda _s: None)
        results.append(placed)
        if firing == 0:
            # The position now exists for the day; capture it for the
            # "left unchanged" comparison against every later firing.
            snapshot_after_first = _snapshot(engine)

    # Exactly one entry was placed, and it was the first trigger; every
    # subsequent trigger was rejected (MaxPositionInADay = 1, Req 6.3).
    assert results[0] is True
    assert all(placed is False for placed in results[1:])
    assert results.count(True) == 1

    # The order primitive fires at most once regardless of trigger count: once
    # when any immediate leg is placed, and never when all legs are deferred.
    expected_order_calls = 0 if all_momentum else 1
    assert client.call_count("optionsmultiorder") == expected_order_calls

    # A position is recorded for the day and stays recorded.
    assert engine.entered_today is True

    # The existing position is left unchanged by every repeat trigger.
    assert _snapshot(engine) == snapshot_after_first
