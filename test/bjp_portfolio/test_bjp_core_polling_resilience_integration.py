"""Integration tests for polling-loop resilience (mocked SDK).

Exercises the ``monitor``/``_poll_loop`` polling path (Task 18.1) end-to-end
against the in-memory fake OpenAlgo SDK client from ``conftest.py``. These are
example-based integration tests (Task 18.2) that drive the *real* LTP transport
(``_poll_refresh`` -> ``fetch_quote_ltp`` -> ``client.quotes``) rather than an
injected fetcher, so the resilience behaviour is verified across the SDK
boundary. Only ``sleep`` and ``should_continue`` are injected, purely to bound
the loop so it never runs forever.

Covered requirements:

    * Req 14.6: WHEN an LTP fetch fails on a cycle, the loop logs the error,
      retains the last-known LTP and open-leg state, and continues monitoring on
      the next cycle without exiting.
    * Req 13.6: WHEN the current LTP is unavailable for any open leg, the loop
      skips the aggregate-MTM evaluation for that cycle without squaring off and
      surfaces a stale/missing-market-data error.

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 13.6, 14.6.
"""

from __future__ import annotations

import logging
from typing import Any

import bjp_core as core

_SPOT = 24000.0


def _config(**overrides: Any) -> core.StrategyConfig:
    """Build a minimal NIFTY config with optional overrides."""
    return core.StrategyConfig(
        strategy_name="NF-POLL-TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:25:00",
        **overrides,
    )


def _short_ce(
    symbol: str,
    *,
    entry_fill: float = 100.0,
    last_ltp: float | None = None,
    stop_loss: core.LegStopLoss | None = None,
) -> core.LegState:
    """Build an open short-CE leg in a chosen runtime state."""
    leg_config = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset="ITM2",
        stop_loss=stop_loss,
    )
    return core.LegState(
        config=leg_config,
        symbol=symbol,
        entry_fill=entry_fill,
        is_open=True,
        last_ltp=last_ltp,
    )


def _install_symbol_quotes(
    client: Any,
    *,
    ltp_by_symbol: dict[str, float],
    fail_symbols: set[str],
) -> None:
    """Make ``client.quotes`` symbol-aware while preserving the call log.

    The spot symbol and any leg symbol in ``ltp_by_symbol`` return a
    success-shaped envelope with that LTP; any symbol in ``fail_symbols`` returns
    an error-shaped envelope so ``fetch_quote_ltp`` yields ``None`` (simulating a
    failed SDK fetch). Every call is still recorded on ``client.calls`` so the
    integration path through the SDK surface is observable.
    """

    def quotes(**kwargs: Any) -> dict[str, Any]:
        client.calls.append(("quotes", kwargs))
        symbol = kwargs.get("symbol")
        if symbol in fail_symbols:
            return {"status": "error", "message": f"quote unavailable for {symbol}"}
        return {"status": "success", "data": {"ltp": ltp_by_symbol[symbol]}}

    client.quotes = quotes


def _bounded(max_cycles: int) -> tuple[Any, dict[str, int]]:
    """Return a ``should_continue`` predicate bounding the loop and its counter."""
    state = {"n": 0}

    def should_continue() -> bool:
        state["n"] += 1
        return state["n"] <= max_cycles

    return should_continue, state


# -- Req 14.6: fetch failure retains last-known state and continues ---------


def test_ltp_fetch_failure_retains_state_and_continues(fake_client, caplog) -> None:
    """A failed LTP fetch logs, keeps last-known LTP/open state, keeps looping (Req 14.6)."""
    # A single open short leg with no stop loss configured, so nothing should
    # ever square it off; the only thing under test is fetch resilience.
    leg_symbol = "NIFTY31DEC2524000CE"
    engine = core.EngineState(config=_config(), client=fake_client)
    engine.legs = [_short_ce(leg_symbol)]

    # The leg quote succeeds on its first fetch (establishing last_ltp across the
    # SDK boundary) and fails on every subsequent cycle; the spot always resolves.
    leg_fetches = {"n": 0}

    def quotes(**kwargs: Any) -> dict[str, Any]:
        fake_client.calls.append(("quotes", kwargs))
        symbol = kwargs.get("symbol")
        if symbol == core.NIFTY.name:
            return {"status": "success", "data": {"ltp": _SPOT}}
        leg_fetches["n"] += 1
        if leg_fetches["n"] == 1:
            return {"status": "success", "data": {"ltp": 90.0}}
        return {"status": "error", "message": "quote unavailable"}

    fake_client.quotes = quotes

    should_continue, cycles = _bounded(3)
    decisions: list[core.RiskDecision] = []

    with caplog.at_level(logging.ERROR):
        core.monitor(
            engine,
            sleep=lambda _seconds: None,
            should_continue=should_continue,
            handler=lambda _es, decision: decisions.append(decision),
        )

    # The loop ran to the bound instead of exiting on the failed fetch: three
    # evaluation cycles plus the fourth consultation that stops it (Req 14.6).
    assert cycles["n"] == 4
    # Last-known LTP from the first successful fetch is retained through the
    # failing cycles, and the leg stays open.
    assert engine.legs[0].last_ltp == 90.0
    assert engine.legs[0].is_open is True
    # No stop loss configured, so no square-off decision was ever produced.
    assert decisions == []
    # The failure was surfaced with a last-known-retention error, and the fetch
    # actually went through the SDK ``quotes`` surface.
    retain_logs = [
        record.getMessage()
        for record in caplog.records
        if "retaining last-known LTP" in record.getMessage()
    ]
    assert retain_logs, "expected a last-known-LTP retention error to be logged"
    assert fake_client.call_count("quotes") >= 4


# -- Req 13.6: missing LTP for an open leg skips MTM with stale-data error --


def test_missing_ltp_skips_aggregate_mtm_with_stale_error(fake_client, caplog) -> None:
    """An open leg with no LTP skips overall MTM without squaring off (Req 13.6)."""
    fresh_symbol = "NIFTY31DEC2524000CE"
    stale_symbol = "NIFTY31DEC2524100PE"
    # A tiny overall stop loss that WOULD breach immediately if aggregate MTM
    # were evaluated on the fresh leg alone (short: (100 - 200) * 65 = -6500).
    engine = core.EngineState(
        config=_config(overall_stop_loss=core.OverallStopLoss(mtm_rupees=100.0)),
        client=fake_client,
    )
    fresh_leg = _short_ce(fresh_symbol, entry_fill=100.0)
    stale_leg = _short_ce(stale_symbol, entry_fill=100.0)
    engine.legs = [fresh_leg, stale_leg]

    # The stale leg's quote always fails, so it never obtains an LTP; the fresh
    # leg and the spot always resolve.
    _install_symbol_quotes(
        fake_client,
        ltp_by_symbol={core.NIFTY.name: _SPOT, fresh_symbol: 200.0},
        fail_symbols={stale_symbol},
    )

    should_continue, cycles = _bounded(2)
    decisions: list[core.RiskDecision] = []

    with caplog.at_level(logging.ERROR):
        core.monitor(
            engine,
            sleep=lambda _seconds: None,
            should_continue=should_continue,
            handler=lambda _es, decision: decisions.append(decision),
        )

    # Aggregate MTM was skipped every cycle because an open leg lacked an LTP, so
    # the overall stop loss never fired and nothing was squared off (Req 13.6).
    assert decisions == []
    assert fresh_leg.is_open is True
    assert stale_leg.is_open is True
    assert stale_leg.last_ltp is None
    # The fresh leg still updated its LTP across the SDK boundary.
    assert fresh_leg.last_ltp == 200.0
    # A stale/missing-market-data error was surfaced.
    stale_logs = [
        record.getMessage()
        for record in caplog.records
        if "stale" in record.getMessage().lower()
    ]
    assert stale_logs, "expected a stale/missing-market-data error to be logged"
