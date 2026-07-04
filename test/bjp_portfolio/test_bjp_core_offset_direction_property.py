"""Property-based test for apply_offset direction per option type.

Implements Property 7 from the design: offset direction is correct per option
type (Req 4.2, 4.3). The production code under test lives in
``strategies/bjp_portfolio/bjp_core.py`` and is imported as ``bjp_core`` via the
sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 7: Offset direction is correct per
# option type. For any ATM reference strike, strike step, and offset distance n,
# a CE OTM<n> resolves to atm + n*step and CE ITM<n> to atm - n*step, while a PE
# OTM<n> resolves to atm - n*step and PE ITM<n> to atm + n*step; ATM resolves to
# atm for both. Validates: Requirements 4.2, 4.3.


def _expected_strike(atm: int, step: int, direction: str, n: int, opt: core.OptionType) -> int:
    """Return the strike the formulas in Req 4.2/4.3 mandate."""
    if direction == "ATM":
        return atm
    is_otm = direction == "OTM"
    # CE moves up for OTM and down for ITM; PE is the mirror.
    move_up = is_otm if opt is core.OptionType.CE else not is_otm
    delta = n * step
    return atm + delta if move_up else atm - delta


@settings(max_examples=100)
@given(
    step=st.sampled_from([50, 100]),
    atm_multiple=st.integers(min_value=1, max_value=2000),
    n=st.integers(min_value=0, max_value=50),
    direction=st.sampled_from(["OTM", "ITM", "ATM"]),
    option_type=st.sampled_from([core.OptionType.CE, core.OptionType.PE]),
)
def test_offset_direction_is_correct_per_option_type(
    step: int,
    atm_multiple: int,
    n: int,
    direction: str,
    option_type: core.OptionType,
):
    atm = atm_multiple * step  # ATM reference is a multiple of the strike step.

    offset = "ATM" if direction == "ATM" else f"{direction}{n}"

    resolved = core.apply_offset(atm, step, offset, option_type)

    expected = _expected_strike(atm, step, direction, n, option_type)
    assert resolved == expected

    # ATM (or a zero-distance directional offset) always resolves to atm itself.
    if direction == "ATM" or n == 0:
        assert resolved == atm
