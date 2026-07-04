"""Property-based test for hedge no-early-exit behavior.

Implements Property 13 from the design: a ``Hedge_Strategy`` (BUY OTM CE + PE
with **no** leg stop loss, leg trail, re-entry, overall stop loss, or overall
trail) applies none of those risk controls and keeps all its legs open until
Exit_Time (Req 8.3, 8.4).

The engine's per-cycle risk decision is produced by
:func:`bjp_core.evaluate_risk_cycle`, which returns the highest-precedence
:class:`RiskDecision` for the cycle (momentum entry -> leg SL -> leg trail ->
overall SL -> overall trail) or ``None`` when nothing fires. For a hedge config
every one of those categories is unconfigured, so the strategy must never
generate an early-exit decision no matter how the premiums or the underlying
spot move between entry and exit.

The test:
    * builds a hedge :class:`bjp_core.EngineState` for either index (BUY CE + PE
      at ``OTM20``, no per-leg or portfolio-wide risk controls, matching the
      ``nf_hedge`` / ``sensex_hedge`` parameter rows);
    * drives an arbitrary sequence of ``(ce_ltp, pe_ltp, spot)`` monitoring
      cycles through :func:`evaluate_risk_cycle` and asserts it returns ``None``
      every cycle (no early exit) and both legs stay open (Req 8.4); and
    * asserts, for any wall-clock time strictly between Entry_Time and Exit_Time,
      that the scheduled exit has *not* fired
      (:func:`is_at_or_after_exit_time` is ``False``), while it *does* fire at the
      Exit_Time boundary — the legs are held open until Exit_Time and no earlier
      (Req 8.4).

Production code under test lives in ``strategies/bjp_portfolio/bjp_core.py`` and
is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

from datetime import time

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 13: Hedge strategies never exit
# early. For any sequence of LTP and spot values between entry and exit, a
# Hedge_Strategy applies no leg stop loss, leg trail, re-entry, overall stop
# loss, or overall trail, and keeps all its legs open until Exit_Time.
# Validates: Requirements 8.3, 8.4.

# Realistic positive option premiums and a wide underlying spot band so the
# generated price paths exercise both large adverse and favorable moves.
_ltp = st.floats(min_value=0.05, max_value=5000.0, allow_nan=False, allow_infinity=False)
_spot = st.floats(min_value=1000.0, max_value=90000.0, allow_nan=False, allow_infinity=False)
# One monitoring cycle's observed (CE premium, PE premium, underlying spot).
_cycle = st.tuples(_ltp, _ltp, _spot)


def _hedge_engine(index: core.IndexSpec, entry_fill: float) -> core.EngineState:
    """Build a two-leg BUY hedge engine with both legs open and no risk controls.

    Mirrors the ``nf_hedge`` / ``sensex_hedge`` parameter rows: BUY CE + PE at
    ``OTM20``, no leg SL/trail/momentum/re-entry, and no overall SL/trail
    (Req 8.1-8.3).
    """
    config = core.StrategyConfig(
        strategy_name="HEDGE",
        index=index,
        lots=5,
        entry_time="09:24:00",
        exit_time="15:26:00",
        legs=[
            core.LegConfig(core.OptionType.CE, core.Action.BUY, "OTM20"),
            core.LegConfig(core.OptionType.PE, core.Action.BUY, "OTM20"),
        ],
        # No overall_stop_loss, no overall_trail_sl, square_off_all_legs default.
    )
    engine = core.EngineState(config=config, client=object())
    engine.legs = [
        core.LegState(
            config=leg_cfg,
            symbol=f"HEDGE-{leg_cfg.option_type.value}",
            order_id="1",
            entry_fill=entry_fill,
            is_open=True,
        )
        for leg_cfg in config.legs
    ]
    return engine


def _assert_no_hedge_risk_controls(engine: core.EngineState) -> None:
    """Assert the hedge config carries none of the five risk controls (Req 8.3)."""
    assert engine.config.overall_stop_loss is None
    assert engine.config.overall_trail_sl is None
    for leg in engine.legs:
        assert leg.config.stop_loss is None
        assert leg.config.trail_sl is None
        assert leg.config.momentum is None
        assert leg.config.reentry is None
        assert not leg.pending_momentum


@settings(max_examples=100)
@given(
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
    entry_fill=_ltp,
    cycles=st.lists(_cycle, min_size=1, max_size=40),
)
def test_hedge_never_produces_an_early_exit_decision(
    index: core.IndexSpec,
    entry_fill: float,
    cycles: list[tuple[float, float, float]],
) -> None:
    """No monitoring cycle yields a risk decision and both legs stay open."""
    engine = _hedge_engine(index, entry_fill)
    _assert_no_hedge_risk_controls(engine)

    ce_leg, pe_leg = engine.legs

    for ce_ltp, pe_ltp, spot in cycles:
        # Feed the cycle's observed prices to the open legs, exactly as the
        # monitoring loop would before evaluating risk.
        ce_leg.last_ltp = ce_ltp
        pe_leg.last_ltp = pe_ltp

        decision = core.evaluate_risk_cycle(engine, spot=spot)

        # No leg SL / trail / momentum and no overall SL / trail -> nothing fires.
        assert decision is None
        # With no decision acted on, both hedge legs remain open (Req 8.4).
        assert ce_leg.is_open
        assert pe_leg.is_open

    # The strategy is still holding both legs after the whole price path.
    assert not core.all_legs_closed(engine)


@settings(max_examples=100)
@given(
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
    hour=st.integers(min_value=9, max_value=15),
    minute=st.integers(min_value=0, max_value=59),
    second=st.integers(min_value=0, max_value=59),
)
def test_hedge_scheduled_exit_only_fires_at_exit_time(
    index: core.IndexSpec,
    hour: int,
    minute: int,
    second: int,
) -> None:
    """Legs are held until Exit_Time: no scheduled exit fires before it (Req 8.4)."""
    engine = _hedge_engine(index, entry_fill=100.0)
    entry = time(9, 24, 0)
    exit_time = time(15, 26, 0)
    now = time(hour, minute, second)

    fired = core.is_at_or_after_exit_time(engine.config, now=now)

    if now < exit_time:
        # Strictly before Exit_Time (including the entire entry->exit window):
        # the scheduled exit has not fired, so the hedge keeps its legs open.
        assert fired is False
    else:
        # At or after the Exit_Time boundary the scheduled exit fires.
        assert fired is True

    # Sanity: the boundary itself counts as "at Exit_Time" and the window start
    # (Entry_Time) does not trigger an early exit.
    assert core.is_at_or_after_exit_time(engine.config, now=exit_time) is True
    assert core.is_at_or_after_exit_time(engine.config, now=entry) is False
