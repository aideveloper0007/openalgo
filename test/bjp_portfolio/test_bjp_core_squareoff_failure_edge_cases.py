"""Example-based edge-case tests for square-off failure handling.

Covers the two failure paths of the exit manager (Task 19.1) with concrete,
hand-picked scenarios rather than generated inputs:

    * Req 13.5: WHEN a square-off order for any leg fails while closing the
      strategy, the engine retries the square-off for each *remaining* open leg
      and surfaces which legs remain open. A single leg's failure never discards
      another leg's confirmed close, and re-invoking the close on a subsequent
      cycle re-submits only the still-open legs and closes them once the broker
      accepts.
    * Req 15.6: WHEN a scheduled-exit square-off order fails, the engine logs the
      affected leg and re-submits that leg's square-off on each subsequent cycle
      until it is confirmed closed, while leaving already-closed legs untouched.

These drive the *real* SDK boundary via the in-memory fake client from
``conftest.py``: ``placeorder`` is made symbol-aware so chosen legs return an
error envelope (rejected square-off) while others succeed. A no-op ``sleep`` is
injected so the bounded per-leg retry policy runs instantly.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 13.5, 15.6.
"""

from __future__ import annotations

import logging
from typing import Any

import bjp_core as core


def _config(**overrides: Any) -> core.StrategyConfig:
    """Build a minimal NIFTY config with optional overrides."""
    return core.StrategyConfig(
        strategy_name="NF-SQOFF-TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:25:00",
        **overrides,
    )


def _open_short_leg(
    symbol: str,
    option_type: core.OptionType,
    *,
    entry_fill: float = 100.0,
    last_ltp: float = 90.0,
) -> core.LegState:
    """Build an open short leg with a known fill/LTP so its MTM is defined."""
    leg_config = core.LegConfig(
        option_type=option_type,
        action=core.Action.SELL,
        offset="ATM",
    )
    return core.LegState(
        config=leg_config,
        symbol=symbol,
        order_id="ENTRY-1",
        entry_fill=entry_fill,
        last_ltp=last_ltp,
        is_open=True,
    )


def _install_symbol_placeorder(client: Any, fail_state: dict[str, set[str]]) -> None:
    """Make ``client.placeorder`` reject symbols currently in ``fail_state``.

    ``fail_state["symbols"]`` is a mutable set of symbols whose square-off order
    should be rejected (error envelope). Mutating that set between cycles models
    the broker accepting a previously-rejected leg on a later cycle. Every call
    is recorded on ``client.calls`` so re-submission is observable across the SDK
    boundary.
    """

    def placeorder(**kwargs: Any) -> dict[str, Any]:
        client.calls.append(("placeorder", kwargs))
        symbol = kwargs.get("symbol")
        if symbol in fail_state["symbols"]:
            return {"status": "error", "message": f"square-off rejected for {symbol}"}
        return {"status": "success", "orderid": f"SQ-{symbol}"}

    client.placeorder = placeorder


def _placeorder_symbols(client: Any) -> list[str | None]:
    """Return the ordered list of symbols passed to every ``placeorder`` call."""
    return [kwargs.get("symbol") for name, kwargs in client.calls if name == "placeorder"]


# -- Req 13.5: partial overall square-off retries remaining legs -------------


def test_partial_overall_square_off_reports_and_retries_open_leg(
    fake_client, caplog
) -> None:
    """One rejected leg stays open and is reported; a later cycle closes it (Req 13.5)."""
    ce_ok = "NIFTY31DEC2524000CE"
    pe_bad = "NIFTY31DEC2524000PE"
    ce_ok2 = "NIFTY31DEC2524100CE"

    engine = core.EngineState(config=_config(square_off_all_legs=True), client=fake_client)
    leg_ok = _open_short_leg(ce_ok, core.OptionType.CE)
    leg_bad = _open_short_leg(pe_bad, core.OptionType.PE)
    leg_ok2 = _open_short_leg(ce_ok2, core.OptionType.CE)
    engine.legs = [leg_ok, leg_bad, leg_ok2]

    fail_state = {"symbols": {pe_bad}}
    _install_symbol_placeorder(fake_client, fail_state)

    # First cycle: the middle leg is rejected on all 3 bounded attempts while the
    # two other legs are confirmed closed.
    with caplog.at_level(logging.ERROR):
        still_open = core.square_off_all_open(
            engine, reason="overall_stop_loss", sleep=lambda _s: None
        )

    # Only the rejected leg is reported as still open; the successes are retained
    # (not discarded by the sibling failure).
    assert still_open == [leg_bad]
    assert leg_ok.is_open is False
    assert leg_ok2.is_open is False
    assert leg_bad.is_open is True
    # The rejected leg exhausted the bounded retry (3 attempts) this cycle, while
    # each successful leg was submitted exactly once.
    symbols = _placeorder_symbols(fake_client)
    assert symbols.count(ce_ok) == 1
    assert symbols.count(ce_ok2) == 1
    assert symbols.count(pe_bad) == core.MAX_RETRY_ATTEMPTS
    # The engine surfaced which legs remain open.
    assert any(
        pe_bad in record.getMessage() and "remain open" in record.getMessage()
        for record in caplog.records
    )

    # Subsequent cycle: the broker now accepts the previously-rejected leg. Only
    # the still-open leg is re-submitted (the already-closed legs are untouched).
    fail_state["symbols"].clear()
    before = len(_placeorder_symbols(fake_client))

    still_open_2 = core.square_off_all_open(
        engine, reason="overall_stop_loss", sleep=lambda _s: None
    )

    assert still_open_2 == []
    assert leg_bad.is_open is False
    assert leg_bad.exit_reason == "overall_stop_loss"
    resubmitted = _placeorder_symbols(fake_client)[before:]
    # Exactly one re-submission, for the previously-open leg only.
    assert resubmitted == [pe_bad]


def test_partial_overall_square_off_retries_only_still_open_legs(fake_client) -> None:
    """square_off_legs retries only the passed legs, closing them when accepted (Req 13.5)."""
    pe_bad = "NIFTY31DEC2524000PE"
    engine = core.EngineState(config=_config(), client=fake_client)
    leg_bad = _open_short_leg(pe_bad, core.OptionType.PE)
    engine.legs = [leg_bad]

    fail_state = {"symbols": {pe_bad}}
    _install_symbol_placeorder(fake_client, fail_state)

    # First cycle rejects the leg: it is returned as still open.
    still_open = core.square_off_legs(
        engine, [leg_bad], reason="overall_trail_sl", sleep=lambda _s: None
    )
    assert still_open == [leg_bad]
    assert leg_bad.is_open is True

    # Retrying only the still-open subset closes it once the broker accepts.
    fail_state["symbols"].clear()
    still_open_2 = core.square_off_legs(
        engine, still_open, reason="overall_trail_sl", sleep=lambda _s: None
    )
    assert still_open_2 == []
    assert leg_bad.is_open is False


# -- Req 15.6: scheduled-exit square-off re-submits each subsequent cycle -----


def test_scheduled_exit_resubmits_failed_leg_until_closed(fake_client, caplog) -> None:
    """A rejected scheduled-exit leg is re-submitted each cycle until closed (Req 15.6)."""
    ce_ok = "NIFTY31DEC2524000CE"
    pe_bad = "NIFTY31DEC2524000PE"

    engine = core.EngineState(config=_config(), client=fake_client)
    leg_ok = _open_short_leg(ce_ok, core.OptionType.CE)
    leg_bad = _open_short_leg(pe_bad, core.OptionType.PE)
    engine.legs = [leg_ok, leg_bad]

    fail_state = {"symbols": {pe_bad}}
    _install_symbol_placeorder(fake_client, fail_state)

    # Cycle 1: the good leg closes, the bad leg is rejected and reported open.
    with caplog.at_level(logging.ERROR):
        cycle1 = core.perform_scheduled_exit(engine, sleep=lambda _s: None)
    assert cycle1 == [leg_bad]
    assert leg_ok.is_open is False
    assert leg_bad.is_open is True
    assert any(
        pe_bad in record.getMessage() for record in caplog.records
    ), "expected the affected leg to be surfaced on failure"

    # Cycle 2: still rejected. The exit is re-invoked and re-submits ONLY the
    # still-open leg (the closed leg is left unchanged, never re-submitted).
    before_c2 = len(_placeorder_symbols(fake_client))
    cycle2 = core.perform_scheduled_exit(engine, sleep=lambda _s: None)
    assert cycle2 == [leg_bad]
    assert leg_bad.is_open is True
    c2_symbols = _placeorder_symbols(fake_client)[before_c2:]
    assert ce_ok not in c2_symbols
    assert pe_bad in c2_symbols

    # Cycle 3: the broker accepts the leg; the exit finally confirms it closed.
    fail_state["symbols"].clear()
    before_c3 = len(_placeorder_symbols(fake_client))
    cycle3 = core.perform_scheduled_exit(engine, sleep=lambda _s: None)
    assert cycle3 == []
    assert leg_bad.is_open is False
    assert leg_bad.exit_reason == "scheduled_exit"
    c3_symbols = _placeorder_symbols(fake_client)[before_c3:]
    assert c3_symbols == [pe_bad]
