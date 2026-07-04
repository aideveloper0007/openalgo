"""Integration tests for scheduled multi-leg entry placement (mocked SDK).

Exercises ``place_entry`` (Task 16.1) end-to-end against the in-memory fake
OpenAlgo SDK client from ``conftest.py``, with a no-op sleep injected so the
2-second average-fill retry interval never incurs a real wall-clock wait.

These are example-based integration tests (Task 16.4) that assert the observable
contract at the SDK boundary:

    * the single ``optionsmultiorder`` call carries the correct top-level
      ``strategy`` / ``underlying`` / ``exchange`` and, per leg, the resolved
      ``expiry_date``, ``offset``, ``option_type``, ``action``, ``quantity``,
      ``product``, and ``pricetype`` (Req 7.1);
    * a leg's average fill is fetched via ``client.orderstatus()`` and retried
      (up to 3 attempts at a 2-second interval) until a non-null average price
      is returned, with the retry interval delegated to the injected sleep so
      no real delay occurs (Req 7.6); and
    * the whole entry completes well within the 5-second budget from the trigger
      firing, even across the retry path (Req 6.2).

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 6.2, 7.1, 7.6.
"""

from __future__ import annotations

import time as _time

import bjp_core as core

# Resolved weekly expiry shared by every leg of a placement.
_EXPIRY = "31-DEC-25"

# Underlying spot recorded as each filled leg's UnderlyingPoints baseline.
_SPOT = 24000.0


def _noop_sleep(_seconds: float) -> None:
    """Sleep replacement that records nothing and never blocks."""


def _two_leg_config() -> core.StrategyConfig:
    """Build a short CE+PE NIFTY straddle config (SELL both legs)."""
    return core.StrategyConfig(
        strategy_name="NF-ENTRY-TEST",
        index=core.NIFTY,
        lots=2,
        entry_time="09:25:00",
        exit_time="15:29:00",
        legs=[
            core.LegConfig(
                option_type=core.OptionType.CE,
                action=core.Action.SELL,
                offset="ITM2",
            ),
            core.LegConfig(
                option_type=core.OptionType.PE,
                action=core.Action.SELL,
                offset="ITM2",
            ),
        ],
    )


def _single_leg_config() -> core.StrategyConfig:
    """Build a single short CE NIFTY leg config to isolate one fill fetch."""
    return core.StrategyConfig(
        strategy_name="NF-ENTRY-SOLO",
        index=core.NIFTY,
        lots=1,
        entry_time="09:25:00",
        exit_time="15:29:00",
        legs=[
            core.LegConfig(
                option_type=core.OptionType.CE,
                action=core.Action.SELL,
                offset="OTM6",
            ),
        ],
    )


def _engine(config: core.StrategyConfig, fake_client: object) -> core.EngineState:
    """Build an ``EngineState`` with the resolved weekly expiry set."""
    return core.EngineState(config=config, client=fake_client, expiry=_EXPIRY)


def _multi_result(*symbols_and_ids: tuple[str, str]) -> dict[str, object]:
    """Build a success-shaped ``optionsmultiorder`` response with per-leg results."""
    return {
        "status": "success",
        "results": [
            {"symbol": symbol, "orderid": order_id}
            for symbol, order_id in symbols_and_ids
        ],
    }


def test_optionsmultiorder_payload_has_correct_fields(fake_client) -> None:
    """The single entry call carries the correct top-level and per-leg fields (Req 7.1)."""
    config = _two_leg_config()
    engine = _engine(config, fake_client)
    fake_client.optionsmultiorder_response = _multi_result(
        ("NIFTY31DEC2524000CE", "1"),
        ("NIFTY31DEC2524000PE", "2"),
    )

    placed = core.place_entry(engine, spot=_SPOT, sleep=_noop_sleep)

    assert placed is True
    # Exactly one multi-leg order call places both legs together (Req 7.1).
    assert fake_client.call_count("optionsmultiorder") == 1

    payload = fake_client.last_call("optionsmultiorder")
    assert payload is not None
    assert payload["strategy"] == "NF-ENTRY-TEST"
    assert payload["underlying"] == core.NIFTY.name  # "NIFTY"
    assert payload["exchange"] == core.NIFTY.index_exchange  # "NSE_INDEX"

    legs = payload["legs"]
    assert len(legs) == 2

    expected_quantity = config.quantity  # lots * lot_size = 2 * 65 = 130
    expected_types = {"CE", "PE"}
    seen_types = set()
    for leg in legs:
        assert leg["expiry_date"] == _EXPIRY
        assert leg["offset"] == "ITM2"
        assert leg["option_type"] in expected_types
        seen_types.add(leg["option_type"])
        assert leg["action"] == "SELL"
        assert leg["quantity"] == expected_quantity
        assert isinstance(leg["quantity"], int) and leg["quantity"] > 0
        assert leg["quantity"] % core.NIFTY.lot_size == 0
        assert leg["product"] == core.DEFAULT_PRODUCT  # "NRML"
        assert leg["pricetype"] == core.ENTRY_PRICE_TYPE  # "MARKET"

    # Both option rights were placed in the one call (Req 7.1).
    assert seen_types == expected_types


def test_orderstatus_retried_until_average_fill(fake_client) -> None:
    """Average fill is fetched via orderstatus and retried up to 3x @2s (Req 7.6)."""
    config = _single_leg_config()
    engine = _engine(config, fake_client)
    fake_client.optionsmultiorder_response = _multi_result(
        ("NIFTY31DEC2524000CE", "1"),
    )
    # First two orderstatus calls report no average price; the third succeeds,
    # forcing the bounded retry to run all three attempts (Req 7.6).
    fake_client.orderstatus_responses = [
        {"status": "success", "data": {}},
        {"status": "success", "data": {"average_price": None}},
        {"status": "success", "data": {"average_price": 101.5}},
    ]

    sleeps: list[float] = []
    placed = core.place_entry(engine, spot=_SPOT, sleep=sleeps.append)

    assert placed is True
    # Three orderstatus attempts were made for the single leg (Req 7.6).
    assert fake_client.call_count("orderstatus") == 3
    # The retry interval was the configured 2 seconds, delegated to injected sleep.
    assert sleeps == [core.AVG_FILL_RETRY_INTERVAL, core.AVG_FILL_RETRY_INTERVAL]
    # The non-null average fill from the final attempt is adopted for risk calc.
    leg = engine.legs[0]
    assert leg.entry_fill == 101.5
    assert leg.symbol == "NIFTY31DEC2524000CE"
    assert leg.order_id == "1"
    assert leg.is_open is True


def test_orderstatus_call_targets_leg_order_id(fake_client) -> None:
    """The average-fill fetch queries the leg's recorded order id (Req 7.6)."""
    config = _single_leg_config()
    engine = _engine(config, fake_client)
    fake_client.optionsmultiorder_response = _multi_result(
        ("NIFTY31DEC2524000CE", "42"),
    )

    core.place_entry(engine, spot=_SPOT, sleep=_noop_sleep)

    status_call = fake_client.last_call("orderstatus")
    assert status_call is not None
    assert status_call["order_id"] == "42"
    assert status_call["strategy"] == "NF-ENTRY-SOLO"


def test_entry_placed_within_five_seconds_of_trigger(fake_client) -> None:
    """The entry completes well within the 5s budget, even across retries (Req 6.2)."""
    config = _two_leg_config()
    engine = _engine(config, fake_client)
    fake_client.optionsmultiorder_response = _multi_result(
        ("NIFTY31DEC2524000CE", "1"),
        ("NIFTY31DEC2524000PE", "2"),
    )
    # Exercise the retry path on both legs so the timing budget covers it; the
    # injected no-op sleep keeps the (otherwise 2s) retry waits instantaneous.
    fake_client.orderstatus_responses = [
        {"status": "success", "data": {}},
        {"status": "success", "data": {"average_price": 100.0}},
        {"status": "success", "data": {}},
        {"status": "success", "data": {"average_price": 100.0}},
    ]

    start = _time.perf_counter()
    placed = core.place_entry(engine, spot=_SPOT, sleep=_noop_sleep)
    elapsed = _time.perf_counter() - start

    assert placed is True
    assert elapsed < 5.0
