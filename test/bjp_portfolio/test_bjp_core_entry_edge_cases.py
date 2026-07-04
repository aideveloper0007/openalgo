"""Edge-case tests for entry-placement failure modes (Task 16.5).

These example tests cover the entry executor branches in ``bjp_core`` that the
entry integration/property tests do not exercise:

* Req 7.5 - when an entry leg's order response omits either the traded symbol or
  the order id, that leg is treated as failed (left closed) while any partial
  data it *did* supply and all *other* legs' recorded data are retained.
* Req 7.7 - when ``orderstatus`` never returns a non-null average fill after 3
  attempts, the leg is excluded from risk calculations (``entry_fill`` stays
  ``None``) but its recorded symbol and order id are preserved.
* Req 7.2 - when a leg's resolved action is neither SELL nor BUY, the entire
  entry is rejected with a logged error and no order is placed.

The production code under test lives in ``strategies/bjp_portfolio/
bjp_core.py`` and is imported as ``bjp_core`` via the sys.path setup in
``conftest.py``. The in-memory ``FakeOpenAlgoClient`` fixture stands in for the
SDK and a no-op ``sleep`` is injected so the bounded retries add no real delay.
"""

from __future__ import annotations

import logging

import bjp_core as core


def _no_sleep(_seconds: float) -> None:
    """Injected sleep that never blocks, so retry intervals add no delay."""
    return None


def _leg(
    option_type: core.OptionType,
    action: core.Action | str,
    offset: str,
) -> core.LegConfig:
    """Build a plain (non-momentum) short/hedge leg config."""
    return core.LegConfig(option_type=option_type, action=action, offset=offset)


def _engine(client: object, legs: list[core.LegConfig]) -> core.EngineState:
    """Build a NIFTY ``EngineState`` around ``legs`` with a resolved expiry."""
    config = core.StrategyConfig(
        strategy_name="TEST",
        index=core.NIFTY,
        lots=2,
        entry_time="09:20:00",
        exit_time="15:25:00",
        legs=legs,
    )
    return core.EngineState(config=config, client=client, expiry="31-DEC-25")


# ---------------------------------------------------------------------------
# Req 7.5 - missing symbol/orderid marks the leg failed, retains other data.
# ---------------------------------------------------------------------------


def test_missing_symbol_marks_leg_failed_and_retains_other_leg_data(
    fake_client, caplog
):
    engine = _engine(
        fake_client,
        [
            _leg(core.OptionType.CE, core.Action.SELL, "ITM2"),
            _leg(core.OptionType.PE, core.Action.SELL, "ITM2"),
        ],
    )
    # Leg 0 fully populated; leg 1's response omits the traded symbol.
    fake_client.optionsmultiorder_response = {
        "status": "success",
        "results": [
            {"symbol": "NIFTY31DEC25CE", "orderid": "A1"},
            {"orderid": "B2"},
        ],
    }
    fake_client.orderstatus_response = {
        "status": "success",
        "data": {"average_price": 120.0},
    }

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        placed = core.place_entry(engine, spot=25000.0, sleep=_no_sleep)

    assert placed is True
    good_leg, bad_leg = engine.legs

    # The good leg is fully recorded and open (Req 7.8 - other leg data intact).
    assert good_leg.symbol == "NIFTY31DEC25CE"
    assert good_leg.order_id == "A1"
    assert good_leg.entry_fill == 120.0
    assert good_leg.is_open is True

    # The leg missing its symbol is treated as failed and left closed (Req 7.5).
    assert bad_leg.is_open is False
    assert bad_leg.symbol is None
    # Partial data it did supply (the order id) is retained.
    assert bad_leg.order_id == "B2"
    # No average fill was fetched for the failed leg.
    assert bad_leg.entry_fill is None

    # The failure is logged naming the missing field.
    assert any(
        "missing symbol" in record.message and "TEST" in record.message
        for record in caplog.records
    )


def test_missing_orderid_marks_leg_failed_and_retains_symbol(fake_client, caplog):
    engine = _engine(
        fake_client,
        [
            _leg(core.OptionType.CE, core.Action.SELL, "ITM2"),
            _leg(core.OptionType.PE, core.Action.SELL, "ITM2"),
        ],
    )
    # Leg 1's response omits the order id but supplies the traded symbol.
    fake_client.optionsmultiorder_response = {
        "status": "success",
        "results": [
            {"symbol": "NIFTY31DEC25CE", "orderid": "A1"},
            {"symbol": "NIFTY31DEC25PE"},
        ],
    }
    fake_client.orderstatus_response = {
        "status": "success",
        "data": {"average_price": 95.0},
    }

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        core.place_entry(engine, spot=25000.0, sleep=_no_sleep)

    _good_leg, bad_leg = engine.legs

    # Leg treated as failed but retains the symbol it did supply (Req 7.5).
    assert bad_leg.is_open is False
    assert bad_leg.symbol == "NIFTY31DEC25PE"
    assert bad_leg.order_id is None
    assert bad_leg.entry_fill is None

    assert any(
        "missing orderid" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Req 7.7 - no avg fill after 3 tries excludes leg from risk but keeps ids.
# ---------------------------------------------------------------------------


def test_no_average_fill_after_three_attempts_excludes_leg_but_keeps_ids(
    fake_client, caplog
):
    engine = _engine(
        fake_client,
        [_leg(core.OptionType.CE, core.Action.SELL, "ITM2")],
    )
    fake_client.optionsmultiorder_response = {
        "status": "success",
        "results": [{"symbol": "NIFTY31DEC25CE", "orderid": "A1"}],
    }
    # orderstatus always returns a null average price -> retries are exhausted.
    fake_client.orderstatus_response = {
        "status": "success",
        "data": {"average_price": None},
    }

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        placed = core.place_entry(engine, spot=25000.0, sleep=_no_sleep)

    assert placed is True
    (leg,) = engine.legs

    # The leg was placed (a position exists) and stays open...
    assert leg.is_open is True
    # ...its symbol and order id are preserved (Req 7.7)...
    assert leg.symbol == "NIFTY31DEC25CE"
    assert leg.order_id == "A1"
    # ...but it is excluded from risk calculations (no entry fill / no trail).
    assert leg.entry_fill is None
    assert leg.trail_level is None

    # The average fill was attempted the bounded maximum of 3 times (Req 7.6).
    assert fake_client.call_count("orderstatus") == core.MAX_RETRY_ATTEMPTS

    assert any(
        "no average fill" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Req 7.2 - invalid resolved action rejects the entry with a logged error.
# ---------------------------------------------------------------------------


def test_invalid_action_rejects_entry_and_places_no_order(fake_client, caplog):
    engine = _engine(
        fake_client,
        [
            _leg(core.OptionType.CE, core.Action.SELL, "ITM2"),
            _leg(core.OptionType.PE, "HOLD", "ITM2"),  # invalid resolved action
        ],
    )

    with caplog.at_level(logging.ERROR, logger="bjp_portfolio"):
        placed = core.place_entry(engine, spot=25000.0, sleep=_no_sleep)

    # Entry rejected (Req 7.2): no position taken, no order placed.
    assert placed is False
    assert engine.entered_today is False
    assert fake_client.call_count("optionsmultiorder") == 0

    # The error names the invalid action.
    assert any(
        "invalid action" in record.message and "HOLD" in record.message
        for record in caplog.records
    )


def test_resolve_action_accepts_only_buy_and_sell():
    # Valid sides resolve to the enum (accepting members and strings).
    assert core._resolve_action(core.Action.SELL) is core.Action.SELL
    assert core._resolve_action(core.Action.BUY) is core.Action.BUY
    assert core._resolve_action("SELL") is core.Action.SELL
    assert core._resolve_action("BUY") is core.Action.BUY

    # Anything else resolves to None so the caller can reject the entry.
    assert core._resolve_action("HOLD") is None
    assert core._resolve_action("") is None
    assert core._resolve_action(None) is None
    assert core._resolve_action(123) is None
