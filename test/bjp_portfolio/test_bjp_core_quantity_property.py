"""Property-based test for the StrategyConfig.quantity computation.

Implements Property 2 from the design: total quantity is lots multiplied by the
index lot size, and is a positive integer multiple of the lot size (Req 3.2,
7.1). The production code under test lives in ``strategies/bjp_portfolio/
bjp_core.py`` and is imported as ``bjp_core`` via the sys.path setup in
``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 2: Total quantity is lots times
# lot size. For any number of lots in 1-100 and either index, the computed
# total quantity equals lots * lot_size (65 NIFTY / 20 SENSEX) and is a
# positive integer multiple of the lot size. Validates: Requirements 3.2, 7.1.


@settings(max_examples=200)
@given(
    lots=st.integers(min_value=1, max_value=100),
    index=st.sampled_from([core.NIFTY, core.SENSEX]),
)
def test_total_quantity_is_lots_times_lot_size(lots: int, index: core.IndexSpec):
    config = core.StrategyConfig(
        strategy_name="PROP2",
        index=index,
        lots=lots,
        entry_time="09:16:00",
        exit_time="15:29:00",
    )

    quantity = config.quantity

    # Quantity equals lots * lot_size exactly.
    assert quantity == lots * index.lot_size

    # Quantity is a positive integer.
    assert isinstance(quantity, int)
    assert quantity > 0

    # Quantity is an exact (positive integer) multiple of the lot size.
    assert quantity % index.lot_size == 0
    assert quantity // index.lot_size == lots
