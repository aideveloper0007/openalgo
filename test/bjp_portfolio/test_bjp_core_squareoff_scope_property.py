"""Property-based test for the overall square-off scope.

Implements Property 24 from the design: when an overall stop-loss or
overall-trailing trigger fires, the set of legs that get squared off is
determined by ``square_off_all_legs``:

    * **true** — every open leg is squared off within that cycle (Req 15.1);
      the ``designated`` legs are ignored.
    * **false** — only the legs designated for the trigger are squared off and
      every other open leg is left unchanged (Req 15.2).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``. A
fresh in-memory ``FakeOpenAlgoClient`` is built per generated example so that
square-off orders succeed (via ``placeorder``) with no network cost, and a
no-op ``sleep`` is injected so bounded retries add no real delay.
"""

from __future__ import annotations

import bjp_core as core
from conftest import FakeOpenAlgoClient
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 24: Square-off scope matches
# configuration. For any strategy with open legs, an overall SL/trail trigger
# squares off every open leg when square_off_all_legs is true, and only the
# designated legs (leaving all other open legs unchanged) when it is false.
# Validates: Requirements 15.1, 15.2.

_actions = st.sampled_from([core.Action.SELL, core.Action.BUY])


def _no_sleep(_seconds: float) -> None:
    """Injected sleep that never blocks (bounded retries add no delay)."""


@st.composite
def _scenarios(draw: st.DrawFn) -> tuple[list[dict], list[bool], bool]:
    """Generate (leg specs, designated flags, square_off_all) tuples.

    Each leg spec carries an ``is_open`` flag and a BUY/SELL action; the
    designated-flag list is aligned one-to-one with the legs, marking which
    legs the trigger designates. ``square_off_all`` selects the config branch.
    """
    n = draw(st.integers(min_value=1, max_value=8))
    specs = [
        {"is_open": draw(st.booleans()), "action": draw(_actions)}
        for _ in range(n)
    ]
    designated = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    square_off_all = draw(st.booleans())
    return specs, designated, square_off_all


def _build_engine(
    specs: list[dict], *, square_off_all: bool
) -> core.EngineState:
    """Build an ``EngineState`` whose legs match ``specs``.

    Every leg is given a distinct recorded ``symbol`` so square-off orders can
    be placed, and ``last_ltp``/``entry_fill`` are left ``None`` so MTM is
    simply skipped (irrelevant to the scope property under test).
    """
    legs: list[core.LegState] = []
    for i, spec in enumerate(specs):
        leg = core.LegState(
            config=core.LegConfig(
                option_type=core.OptionType.CE,
                action=spec["action"],
                offset="ATM",
            ),
            symbol=f"SYM{i}",
            order_id=f"OID{i}",
            is_open=spec["is_open"],
        )
        legs.append(leg)
    config = core.StrategyConfig(
        strategy_name="test",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:26:00",
        legs=[leg.config for leg in legs],
        square_off_all_legs=square_off_all,
    )
    return core.EngineState(
        config=config, client=FakeOpenAlgoClient(), legs=legs
    )


@settings(max_examples=100)
@given(scenario=_scenarios())
def test_square_off_scope_matches_configuration(
    scenario: tuple[list[dict], list[bool], bool],
) -> None:
    specs, designated_flags, square_off_all = scenario
    engine = _build_engine(specs, square_off_all=square_off_all)

    open_before = {id(leg) for leg in engine.legs if leg.is_open}
    designated = [
        leg
        for leg, flag in zip(engine.legs, designated_flags, strict=True)
        if flag
    ]
    designated_ids = {id(leg) for leg in designated}

    still_open = core.perform_overall_square_off(
        engine, designated=designated, reason="overall", sleep=_no_sleep
    )

    placeorder_calls = engine.client.call_count("placeorder")

    if square_off_all:
        # Req 15.1: every previously-open leg is closed this cycle; the
        # designated list is ignored. One square-off order per open leg.
        assert all(not leg.is_open for leg in engine.legs)
        assert placeorder_calls == len(open_before)
    else:
        # Req 15.2: only open designated legs are closed; every other open leg
        # is left unchanged, and legs already closed stay closed.
        expected_closed = open_before & designated_ids
        for leg in engine.legs:
            if id(leg) in expected_closed:
                assert leg.is_open is False
            elif id(leg) in open_before:
                assert leg.is_open is True  # untouched
            else:
                assert leg.is_open is False  # was already closed
        assert placeorder_calls == len(expected_closed)

    # Square-off orders succeed (default fake response), so nothing lingers.
    assert still_open == []


def test_all_legs_true_closes_every_open_leg() -> None:
    """square_off_all_legs=true closes all open legs, ignoring designation."""
    engine = _build_engine(
        [
            {"is_open": True, "action": core.Action.SELL},
            {"is_open": True, "action": core.Action.BUY},
            {"is_open": False, "action": core.Action.SELL},
        ],
        square_off_all=True,
    )
    # Designate only one leg; it must be ignored when all-legs is true.
    still_open = core.perform_overall_square_off(
        engine, designated=[engine.legs[0]], reason="overall", sleep=_no_sleep
    )
    assert all(not leg.is_open for leg in engine.legs)
    assert still_open == []
    assert engine.client.call_count("placeorder") == 2


def test_all_legs_false_closes_only_designated() -> None:
    """square_off_all_legs=false closes only designated legs; others stay open."""
    engine = _build_engine(
        [
            {"is_open": True, "action": core.Action.SELL},
            {"is_open": True, "action": core.Action.SELL},
            {"is_open": True, "action": core.Action.BUY},
        ],
        square_off_all=False,
    )
    designated = [engine.legs[0]]
    still_open = core.perform_overall_square_off(
        engine, designated=designated, reason="overall", sleep=_no_sleep
    )
    assert engine.legs[0].is_open is False  # designated → closed
    assert engine.legs[1].is_open is True  # untouched
    assert engine.legs[2].is_open is True  # untouched
    assert still_open == []
    assert engine.client.call_count("placeorder") == 1


def test_all_legs_false_with_no_designated_closes_nothing() -> None:
    """False scope with no designated legs leaves every open leg unchanged."""
    engine = _build_engine(
        [
            {"is_open": True, "action": core.Action.SELL},
            {"is_open": True, "action": core.Action.BUY},
        ],
        square_off_all=False,
    )
    still_open = core.perform_overall_square_off(
        engine, designated=None, reason="overall", sleep=_no_sleep
    )
    assert all(leg.is_open for leg in engine.legs)
    assert still_open == []
    assert engine.client.call_count("placeorder") == 0
