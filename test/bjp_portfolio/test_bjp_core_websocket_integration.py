"""Integration tests for WebSocket monitoring and polling fallback (Task 18.3).

Exercises the :func:`monitor` loop from Task 18.1 end-to-end over its *real*
WebSocket transport (:func:`_default_ws_runner`) against an in-memory fake SDK
client that exposes the ``connect`` / ``subscribe_ltp`` / ``connected`` /
``disconnect`` surface. No network access ever occurs and the loop is bounded by
an injected ``should_continue`` predicate and a no-op ``sleep`` so it never runs
forever.

These are example-based integration tests asserting the observable contract of
the two monitoring transports:

    * When WebSocket monitoring is enabled, the loop subscribes to the required
      instruments (the index spot plus every open leg) over the WS feed and
      evaluates the risk-precedence dispatcher on *each received tick*, handing
      any fired decision to the handler (Req 14.4); and
    * When the WebSocket connection fails to establish or drops while a leg is
      open, the loop logs and falls back to polling ``client.quotes()`` at the
      configured interval *without exiting*, and the polling cycles then drive
      risk evaluation (Req 14.5).

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.

Validates: Requirements 14.4, 14.5.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import bjp_core as core

# Symbol of the single monitored short CE leg shared across the scenarios.
_CE_SYMBOL = "NIFTY31DEC2524000CE"


class FakeWsClient:
    """In-memory fake OpenAlgo client exposing the WebSocket surface.

    Records every call in ``self.calls`` and, on ``subscribe_ltp``, delivers any
    queued ticks synchronously to the registered ``on_data_received`` callback
    (standing in for the SDK's feed thread). ``connect`` / ``subscribe_ltp`` can
    be forced to fail, and ``connected`` can be pre-set ``False`` to simulate a
    dropped connection while legs remain open. No network access occurs.
    """

    def __init__(
        self,
        *,
        ticks: list[dict[str, Any]] | None = None,
        connected: bool = True,
        connect_ok: bool = True,
        subscribe_ok: bool = True,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._ticks = list(ticks or [])
        self.connected = connected
        self._connect_ok = connect_ok
        self._subscribe_ok = subscribe_ok
        self._on_tick: Callable[[Any], None] | None = None

    def connect(self) -> bool:
        self.calls.append(("connect", {}))
        return self._connect_ok

    def subscribe_ltp(
        self, instruments: list[dict[str, str]], on_data_received: Any = None
    ) -> bool:
        self.calls.append(("subscribe_ltp", {"instruments": instruments}))
        self._on_tick = on_data_received
        if not self._subscribe_ok:
            return False
        # Deliver queued ticks synchronously, simulating the SDK feed thread.
        if on_data_received is not None:
            for tick in self._ticks:
                on_data_received(tick)
        return True

    def unsubscribe_ltp(self, instruments: list[dict[str, str]]) -> bool:
        self.calls.append(("unsubscribe_ltp", {"instruments": instruments}))
        return True

    def disconnect(self) -> None:
        self.calls.append(("disconnect", {}))

    def call_count(self, name: str) -> int:
        return sum(1 for called, _ in self.calls if called == name)

    def last_call(self, name: str) -> dict[str, Any] | None:
        for called, kwargs in reversed(self.calls):
            if called == name:
                return kwargs
        return None


def _fail_on_poll(symbol: str, exchange: str) -> float:
    """Raise to flag that polling ran when the WebSocket path should own the cycle."""
    raise AssertionError(
        "polling fallback ran but the WebSocket transport should have "
        "handled this cycle"
    )


def _ltp_tick(symbol: str, ltp: float) -> dict[str, Any]:
    """Build one WebSocket LTP tick in the SDK callback payload shape."""
    return {
        "symbol": symbol,
        "exchange": core.NIFTY.fno_exchange,
        "data": {"ltp": ltp},
    }


def _ws_config() -> core.StrategyConfig:
    """Build a WebSocket-enabled single short CE NIFTY config."""
    return core.StrategyConfig(
        strategy_name="NF-WS-TEST",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:25:00",
        use_websocket=True,
    )


def _short_ce_leg() -> core.LegState:
    """Build an open short CE leg (entry 100.0) with a 15-point premium SL.

    A premium above 115.0 (entry + 15) trips the leg stop loss, so a tick or a
    poll reporting an LTP of 200.0 fires a ``LEG_STOP_LOSS`` decision.
    """
    leg_config = core.LegConfig(
        option_type=core.OptionType.CE,
        action=core.Action.SELL,
        offset="ITM2",
        stop_loss=core.LegStopLoss(core.SLKind.POINTS, 15.0),
    )
    return core.LegState(
        config=leg_config,
        symbol=_CE_SYMBOL,
        entry_fill=100.0,
        is_open=True,
        last_ltp=None,
    )


def _engine(client: FakeWsClient) -> core.EngineState:
    """Build a WebSocket-enabled engine with one open short CE leg."""
    engine = core.EngineState(config=_ws_config(), client=client)
    engine.legs = [_short_ce_leg()]
    return engine


# -- Req 14.4: WebSocket ticks drive risk evaluation ------------------------


def test_ws_subscribes_to_required_instruments() -> None:
    """WS monitoring connects and subscribes to the spot plus every leg (Req 14.4)."""
    client = FakeWsClient(ticks=[])
    engine = _engine(client)

    core.monitor(
        engine,
        sleep=lambda _s: None,
        should_continue=lambda: False,  # break the park loop immediately (clean)
        fetch=_fail_on_poll,
    )

    assert client.call_count("connect") == 1
    subscribe = client.last_call("subscribe_ltp")
    assert subscribe is not None
    subscribed = {
        (inst["exchange"], inst["symbol"]) for inst in subscribe["instruments"]
    }
    # The index spot (index exchange) and the option leg (F&O exchange) are both
    # subscribed so UnderlyingPoints legs and the ATM reference stay current.
    assert (core.NIFTY.index_exchange, core.NIFTY.name) in subscribed
    assert (core.NIFTY.fno_exchange, _CE_SYMBOL) in subscribed


def test_ws_tick_drives_risk_evaluation() -> None:
    """A received tick tripping the leg SL fires a decision to the handler (Req 14.4)."""
    client = FakeWsClient(ticks=[_ltp_tick(_CE_SYMBOL, 200.0)])
    engine = _engine(client)

    decisions: list[core.RiskDecision] = []

    core.monitor(
        engine,
        sleep=lambda _s: None,
        should_continue=lambda: False,
        handler=lambda es, decision: decisions.append(decision),
        # Any polling here would mean the WS transport did not run: fail loudly.
        fetch=_fail_on_poll,
    )

    # The tick was applied to the leg's last-known LTP ...
    assert engine.legs[0].last_ltp == 200.0
    # ... and drove exactly one risk decision (the leg stop loss).
    assert len(decisions) == 1
    assert decisions[0].action is core.RiskAction.LEG_STOP_LOSS
    assert decisions[0].leg_state is engine.legs[0]


def test_ws_evaluates_each_received_tick() -> None:
    """Risk is evaluated on every tick: a benign tick then a triggering one (Req 14.4)."""
    client = FakeWsClient(
        ticks=[
            _ltp_tick(_CE_SYMBOL, 100.0),  # at entry: no SL breach, no decision
            _ltp_tick(_CE_SYMBOL, 200.0),  # above threshold: leg SL fires
        ]
    )
    engine = _engine(client)

    decisions: list[core.RiskDecision] = []

    core.monitor(
        engine,
        sleep=lambda _s: None,
        should_continue=lambda: False,
        handler=lambda es, decision: decisions.append(decision),
        fetch=_fail_on_poll,
    )

    # Only the second (triggering) tick produced a decision; the benign first
    # tick was still evaluated (it updated the LTP) but fired nothing.
    assert len(decisions) == 1
    assert decisions[0].action is core.RiskAction.LEG_STOP_LOSS
    assert engine.legs[0].last_ltp == 200.0


# -- Req 14.5: connection drop / failure falls back to polling --------------
#
# See ``_fail_on_poll`` above (defined near the tick helpers).


def test_ws_drop_falls_back_to_polling_without_exiting() -> None:
    """A dropped WS connection falls back to polling; polling drives risk (Req 14.5)."""
    # ``connected`` is already False when the park loop first checks it, standing
    # in for a connection that dropped while the leg is open.
    client = FakeWsClient(ticks=[], connected=False)
    engine = _engine(client)

    decisions: list[core.RiskDecision] = []
    sleeps: list[float] = []
    poll_symbols: list[str] = []

    # should_continue must be True once to let the WS park loop observe the drop,
    # then True for a single polling cycle, then False to stop the polling loop.
    calls = {"n": 0}

    def should_continue() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2

    def fetch(symbol: str, exchange: str) -> float:
        poll_symbols.append(symbol)
        return 200.0  # trips the leg SL on the polling cycle

    core.monitor(
        engine,
        sleep=sleeps.append,
        should_continue=should_continue,
        handler=lambda es, decision: decisions.append(decision),
        fetch=fetch,
    )

    # The WS transport was attempted (connect) and torn down (disconnect) rather
    # than the loop exiting on the drop.
    assert client.call_count("connect") == 1
    assert client.call_count("disconnect") == 1
    # Polling then ran and fetched LTPs (spot + leg), driving one risk decision.
    assert poll_symbols  # at least one quote fetched via the polling fallback
    assert len(decisions) == 1
    assert decisions[0].action is core.RiskAction.LEG_STOP_LOSS
    # The fallback polled at the configured (default) interval.
    assert sleeps == [core.DEFAULT_MONITORING_INTERVAL]


def test_ws_connect_failure_falls_back_to_polling() -> None:
    """A WS connection that fails to establish falls back to polling (Req 14.5)."""
    client = FakeWsClient(ticks=[], connect_ok=False)
    engine = _engine(client)

    decisions: list[core.RiskDecision] = []
    poll_symbols: list[str] = []

    calls = {"n": 0}

    def should_continue() -> bool:
        calls["n"] += 1
        return calls["n"] <= 1  # allow one polling cycle, then stop

    def fetch(symbol: str, exchange: str) -> float:
        poll_symbols.append(symbol)
        return 200.0

    core.monitor(
        engine,
        sleep=lambda _s: None,
        should_continue=should_continue,
        handler=lambda es, decision: decisions.append(decision),
        fetch=fetch,
    )

    # connect() was attempted and failed; subscribe was never reached.
    assert client.call_count("connect") == 1
    assert client.call_count("subscribe_ltp") == 0
    # The loop did not exit: it polled and evaluated risk instead.
    assert poll_symbols
    assert len(decisions) == 1
    assert decisions[0].action is core.RiskAction.LEG_STOP_LOSS
