"""Property-based test for default product resolution.

Implements Property 12 from the design: for any configuration, the product type
sent per leg in the ``optionsmultiorder`` call equals the configured product
when one is provided, and equals ``NRML`` when none is configured (Req 7.3).

The test drives :func:`bjp_core.place_entry` with an in-memory fake OpenAlgo SDK
client and inspects the ``legs`` payload passed to ``optionsmultiorder``. The
resolution rule under test is "configured product when provided, else the
``NRML`` default": a truthy ``StrategyConfig.product`` is forwarded verbatim to
every leg, while a falsy one (``None`` or empty string, i.e. "not configured")
falls back to :data:`bjp_core.DEFAULT_PRODUCT`.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from conftest import FakeOpenAlgoClient
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 12: Default product resolution.
# For any configuration, the product sent for every leg in the optionsmultiorder
# payload equals the configured product when one is provided (a truthy
# StrategyConfig.product) and equals NRML when none is configured (None or an
# empty string). Both branches, and every leg in a multi-leg entry, are checked.
# Validates: Requirements 7.3.


def _engine(product: str | None) -> core.EngineState:
    """Build an engine state whose config carries the given ``product``.

    The two SELL CE/PE legs are non-momentum, so both are placed immediately in
    a single ``optionsmultiorder`` call (rather than deferred to the monitoring
    loop), exercising per-leg product resolution across multiple legs.
    """
    config = core.StrategyConfig(
        strategy_name="test-product",
        index=core.NIFTY,
        lots=1,
        entry_time="09:20:00",
        exit_time="15:10:00",
        legs=[
            core.LegConfig(core.OptionType.CE, core.Action.SELL, "ITM2"),
            core.LegConfig(core.OptionType.PE, core.Action.SELL, "ITM2"),
        ],
        product=product,  # type: ignore[arg-type]
    )
    engine = core.EngineState(config=config, client=FakeOpenAlgoClient())
    engine.expiry = "31-DEC-25"
    return engine


@st.composite
def _product_values(draw):
    """Generate a configured product value spanning the provided/absent cases.

    Draws from the "not configured" sentinels (``None`` and empty string) and a
    mix of realistic and arbitrary non-empty product tokens, so both resolution
    branches are exercised.
    """
    return draw(
        st.one_of(
            st.none(),
            st.just(""),
            st.sampled_from(["NRML", "MIS", "CNC", "MARGIN"]),
            st.text(
                alphabet=st.characters(
                    min_codepoint=65, max_codepoint=90
                ),
                min_size=1,
                max_size=8,
            ),
        )
    )


@settings(max_examples=100)
@given(product=_product_values())
# Explicit non-default product is forwarded verbatim to every leg.
@example(product="MIS")
# Absent product (None) falls back to the NRML default.
@example(product=None)
# Empty-string product (not configured) falls back to the NRML default.
@example(product="")
def test_default_product_resolution(product):
    engine = _engine(product)

    placed = core.place_entry(engine, sleep=lambda _seconds: None)
    assert placed is True

    client = engine.client
    assert client.call_count("optionsmultiorder") == 1

    call_kwargs = client.last_call("optionsmultiorder")
    legs_payload = call_kwargs["legs"]
    # Both non-momentum legs are placed in the single call.
    assert len(legs_payload) == len(engine.config.legs)

    expected_product = product or core.DEFAULT_PRODUCT
    for leg_payload in legs_payload:
        assert leg_payload["product"] == expected_product
