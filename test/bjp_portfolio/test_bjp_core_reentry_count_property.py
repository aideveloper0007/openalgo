"""Property-based test for the re-entry count bound.

Implements Property 17 from the design: across any sequence of stop-loss closes
on a re-entering leg, the number of *completed* re-entries never exceeds the
configured re-entry count (one of 1, 3, or 5, Req 12.3).

The test drives :func:`bjp_core.maybe_reenter` through a simulated monitoring
loop. Each iteration models one stop-loss close (the leg is marked closed with
an ``SL`` reason) followed by the dispatcher's re-entry attempt. Both re-entry
flavors are exercised:

    * ``Immediate`` re-enters unconditionally at the prevailing market price
      (Req 12.1);
    * ``AtCost`` re-enters only when the premium has returned to the original
      fill (Req 12.2) — the market price is held at/below the original fill so
      the flavor is always eligible, isolating the *count* bound under test.

No time restriction is configured (``reentry_time_restriction_min`` is ``None``)
so the 13:59 IST cutoff (Req 12.4) never interferes with the count invariant.

Because the invariant must hold after *every* attempt, the loop asserts the
bound on each cycle and additionally checks the exact settled value: after
``attempts`` closes the completed count equals ``min(attempts, count)`` and any
further attempts leave it pinned at the configured ``count`` (further
:func:`maybe_reenter` calls return ``False`` with no state change). Boundary
paths — zero closes, exactly ``count`` closes, and far more than ``count``
closes — are covered by both the generator ranges and explicit ``@example``
cases.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from conftest import FakeOpenAlgoClient
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 17: Re-entry count is bounded. For
# any sequence of stop-loss closes on a re-entering leg (Immediate or AtCost,
# configured count in {1, 3, 5}), the number of completed re-entries never
# exceeds the configured count. After N closes the completed count settles at
# min(N, count) and stays pinned at count for any additional closes. Zero,
# exactly-count, and far-beyond-count close sequences are all exercised.
# Validates: Requirements 12.3.


def _reentering_leg(
    kind: core.ReentryKind, count: int, entry_fill: int
) -> core.LegState:
    """Build a closed short (SELL) leg configured to re-enter.

    The leg starts closed (``is_open=False``) with its original ``entry_fill``
    recorded, so it is a valid candidate for the first re-entry.
    """
    return core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="ATM",
            reentry=core.LegReentry(kind=kind, count=count),
        ),
        symbol="NIFTY31DEC2500000CE",
        entry_fill=float(entry_fill),
        last_ltp=float(entry_fill),
        is_open=False,
    )


def _engine() -> core.EngineState:
    """Build a minimal engine state with no re-entry time restriction."""
    config = core.StrategyConfig(
        strategy_name="test-reentry",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:10:00",
        product="NRML",
        reentry_time_restriction_min=None,
    )
    return core.EngineState(config=config, client=FakeOpenAlgoClient())


@st.composite
def _reentry_scenarios(draw):
    """Generate ``(kind, count, entry_fill, attempts)`` for the count bound.

    ``count`` is drawn from the permitted set {1, 3, 5} (Req 12.3). ``attempts``
    (number of simulated SL closes) ranges from 0 up to well beyond the largest
    permitted count so the settled-at-``count`` boundary is reached and exceeded.
    """
    kind = draw(
        st.sampled_from([core.ReentryKind.IMMEDIATE, core.ReentryKind.AT_COST])
    )
    count = draw(st.sampled_from(core.VALID_REENTRY_COUNTS))
    entry_fill = draw(st.integers(min_value=10, max_value=1_000))
    attempts = draw(st.integers(min_value=0, max_value=12))
    return kind, count, entry_fill, attempts


@settings(max_examples=100)
@given(scenario=_reentry_scenarios())
# Zero closes: no re-entry ever attempted.
@example(scenario=(core.ReentryKind.IMMEDIATE, 3, 100, 0))
# Exactly-count closes for the smallest count.
@example(scenario=(core.ReentryKind.IMMEDIATE, 1, 100, 1))
# Far beyond the largest count for AtCost.
@example(scenario=(core.ReentryKind.AT_COST, 5, 250, 12))
def test_reentry_count_is_bounded(scenario):
    kind, count, entry_fill, attempts = scenario

    leg = _reentering_leg(kind, count, entry_fill)
    engine = _engine()

    # Hold the market price at/below the original fill so AtCost is always ready
    # (premium returned to cost); Immediate ignores this eligibility gate.
    price = float(entry_fill)

    for _ in range(attempts):
        # Model a stop-loss close ahead of the dispatcher's re-entry attempt.
        leg.is_open = False
        leg.exit_reason = "SL"
        core.maybe_reenter(engine, leg, market_price=price, now=None)
        # Invariant (Req 12.3): completed re-entries never exceed the config.
        assert leg.reentries_done <= count

    # After ``attempts`` closes the completed count settles at min(attempts, count).
    assert leg.reentries_done == min(attempts, count)

    # Once capped, further closes place no additional re-entries.
    if attempts >= count:
        leg.is_open = False
        leg.exit_reason = "SL"
        placed = core.maybe_reenter(engine, leg, market_price=price, now=None)
        assert placed is False
        assert leg.reentries_done == count
