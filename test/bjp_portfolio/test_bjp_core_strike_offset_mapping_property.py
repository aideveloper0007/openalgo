"""Property-based test for strike-type to offset mapping.

Implements Property 6 from the design: for any integer distance n from 0 to 50
and any direction token, ``map_strike_type_to_offset`` maps
``StrikeType.OTM<n>`` to ``OTM<n>``, ``StrikeType.ITM<n>`` to ``ITM<n>``, and
``StrikeType.ATM`` to ``ATM``, preserving both the direction token and the
integer distance (Req 4.1).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

_SETTINGS = settings(max_examples=100)

# Feature: bjp-portfolio-strategies, Property 6: Strike-type to offset mapping
# preserves token and distance. For any integer distance n in 0-50 and any
# direction token, map_strike_type_to_offset(StrikeType.OTM<n>) yields OTM<n>,
# StrikeType.ITM<n> yields ITM<n>, and StrikeType.ATM yields ATM, preserving
# both the direction token and the integer distance. Validates: Requirements
# 4.1.

# Directional tokens carry an integer distance; ATM carries none.
_directions = st.sampled_from(["OTM", "ITM"])
_distances = st.integers(min_value=0, max_value=core.MAX_OFFSET_DISTANCE)


@_SETTINGS
@given(direction=_directions, distance=_distances)
def test_directional_mapping_preserves_token_and_distance(direction, distance):
    expected = f"{direction}{distance}"
    prefixed = f"StrikeType.{direction}{distance}"
    bare = f"{direction}{distance}"

    # Both the prefixed backtest form and the bare form map to the canonical
    # upper-case offset, preserving direction token and integer distance.
    assert core.map_strike_type_to_offset(prefixed) == expected
    assert core.map_strike_type_to_offset(bare) == expected


@_SETTINGS
@given(direction=_directions, distance=_distances)
def test_directional_mapping_is_case_insensitive(direction, distance):
    expected = f"{direction}{distance}"
    prefixed = f"StrikeType.{direction.lower()}{distance}"
    bare = f"{direction.lower()}{distance}"

    assert core.map_strike_type_to_offset(prefixed) == expected
    assert core.map_strike_type_to_offset(bare) == expected


@_SETTINGS
@given(token=st.sampled_from(["ATM", "atm", "StrikeType.ATM", "StrikeType.atm"]))
def test_atm_maps_to_atm(token):
    assert core.map_strike_type_to_offset(token) == "ATM"
