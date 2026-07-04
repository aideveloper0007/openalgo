"""Property-based test for AtCost re-entry occurring at the original fill price.

Implements Property 18 from the design: for any ``AtCost`` leg that has closed
on its stop loss and still has re-entry budget remaining (and within the
trading window), a re-entry is placed *if and only if* the leg premium has
returned to (fallen back to or below) its original entry fill price, and the
re-entry is placed at that original fill price rather than at the prevailing
premium (Req 12.2).

The engine decomposes this into a pure predicate (:func:`atcost_reentry_ready`,
which reports ``premium <= entry_fill``) and the state-mutating
:func:`maybe_reenter`. Both are exercised here:

* the predicate is checked for the exact biconditional across the input space,
  including the ``premium == entry_fill`` boundary (inclusive "returns to
  cost"); and
* ``maybe_reenter`` is driven end to end against a fresh in-memory fake SDK
  client, asserting the re-entry decision matches the predicate, that a placed
  re-entry submits a LIMIT order priced at the original fill, and that the
  leg's recorded ``entry_fill`` is preserved (AtCost retains cost) while a
  blocked re-entry mutates nothing and places no order.

Integer-valued premiums and fills are generated so the ``<=`` boundary and the
priced-order string comparison are exact and free of floating-point rounding.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 18: AtCost re-entry occurs at the
# original fill price. For any AtCost leg closed on stop loss with remaining
# re-entry budget within the trading window, a re-entry is placed if and only if
# the leg premium returns to (<=) its original entry fill price, and the
# re-entry uses that original fill price.
# Validates: Requirements 12.2.


def _atcost_leg(entry_fill: int, count: int, reentries_done: int) -> core.LegState:
    """Build a closed short (SELL) ``LegState`` with an AtCost re-entry."""
    return core.LegState(
        config=core.LegConfig(
            option_type=core.OptionType.CE,
            action=core.Action.SELL,
            offset="ITM2",
            stop_loss=core.LegStopLoss(kind=core.SLKind.POINTS, value=15),
            trail_sl=core.LegTrailSL(instrument_move=70, stoploss_move=40),
            reentry=core.LegReentry(kind=core.ReentryKind.AT_COST, count=count),
        ),
        symbol="NIFTY31DEC2520000CE",
        entry_fill=entry_fill,
        last_ltp=entry_fill,
        is_open=False,
        reentries_done=reentries_done,
        exit_reason="leg_stop_loss",
    )


def _engine(client, leg: core.LegState) -> core.EngineState:
    """Build an ``EngineState`` (no re-entry time restriction) around ``leg``."""
    config = core.StrategyConfig(
        strategy_name="NF1",
        index=core.NIFTY,
        lots=2,
        entry_time="09:25:00",
        exit_time="15:29:00",
        legs=[leg.config],
        # No reentry_time_restriction_min -> re-entries always time-allowed, so
        # the decision is governed purely by the AtCost price condition.
    )
    return core.EngineState(config=config, client=client)


@st.composite
def _atcost_scenarios(draw):
    """Generate ``(entry_fill, premium, count, reentries_done)``.

    ``premium`` spans both at/below the fill (return-to-cost -> re-enter) and
    above the fill (still adverse -> no re-entry). ``reentries_done`` is kept
    strictly below ``count`` so budget always remains (isolating the price
    condition from the count bound).
    """
    entry_fill = draw(st.integers(min_value=10, max_value=1_000))
    premium = draw(st.integers(min_value=0, max_value=entry_fill + 200))
    count = draw(st.sampled_from(core.VALID_REENTRY_COUNTS))
    reentries_done = draw(st.integers(min_value=0, max_value=count - 1))
    return entry_fill, premium, count, reentries_done


# ---------------------------------------------------------------------------
# Pure predicate: biconditional on the return-to-cost condition
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(scenario=_atcost_scenarios())
# Boundary: premium exactly at the fill is an inclusive "return to cost".
@example(scenario=(100, 100, 1, 0))
# Just above the fill: not yet returned to cost.
@example(scenario=(100, 101, 3, 0))
# Well below the fill: returned to cost.
@example(scenario=(100, 1, 5, 2))
def test_atcost_ready_iff_premium_returns_to_fill(scenario):
    entry_fill, premium, count, reentries_done = scenario
    leg = _atcost_leg(entry_fill, count, reentries_done)

    assert core.atcost_reentry_ready(leg, premium) is (premium <= entry_fill)


# ---------------------------------------------------------------------------
# maybe_reenter: decision matches the price condition, and re-entry uses the
# original fill price while preserving recorded entry_fill.
# ---------------------------------------------------------------------------


# The factory builds a fresh fake client on every call, so client state is reset
# per generated input despite the fixture being function-scoped.
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scenario=_atcost_scenarios())
@example(scenario=(100, 100, 1, 0))
@example(scenario=(100, 101, 3, 0))
@example(scenario=(250, 50, 5, 4))
def test_atcost_reentry_places_at_original_fill_iff_returned(
    scenario, fake_client_factory
):
    entry_fill, premium, count, reentries_done = scenario
    client = fake_client_factory()
    leg = _atcost_leg(entry_fill, count, reentries_done)
    engine = _engine(client, leg)

    placed = core.maybe_reenter(engine, leg, market_price=premium)

    # Placed iff the premium has returned to or below the original fill (Req 12.2).
    assert placed is (premium <= entry_fill)

    if placed:
        # AtCost retains the original entry fill as the re-entry price.
        assert leg.entry_fill == entry_fill
        assert leg.is_open is True
        assert leg.reentries_done == reentries_done + 1

        # The re-entry order is a LIMIT priced at the original fill, not the
        # prevailing premium.
        order = client.last_call("placeorder")
        assert order is not None
        assert order["price_type"] == "LIMIT"
        assert order["price"] == str(entry_fill)
    else:
        # No re-entry: the leg is untouched and no order was submitted.
        assert leg.is_open is False
        assert leg.entry_fill == entry_fill
        assert leg.reentries_done == reentries_done
        assert client.call_count("placeorder") == 0
