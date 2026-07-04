"""Property-based test for weekly-expiry selection.

Implements Property 9 from the design: the selected current-week expiry is the
earliest date on or after the current trading date, and when no such date
exists expiry resolution fails (raising ``ExpiryError``) so no entry orders are
placed (Req 5.2, 5.5). The production code under test lives in
``strategies/bjp_portfolio/bjp_core.py`` and is imported as ``bjp_core`` via the
sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

from datetime import date

import bjp_core as core
import pytest
from conftest import FakeOpenAlgoClient
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 9: Weekly expiry is the earliest
# date on or after today. For any non-empty list of expiry date strings, the
# selected current-week expiry is the earliest date that is on or after the
# current trading date; when no such date exists, expiry resolution fails and
# no entry orders are placed.
# Validates: Requirements 5.2, 5.5.

# The supported formats use a 2-digit year (``%y``). Python's strptime maps
# 00-68 to 2000-2068, so constraining generated dates to that window keeps the
# format round-trip unambiguous.
_MIN_DATE = date(2000, 1, 1)
_MAX_DATE = date(2068, 12, 31)

_FORMATS = ("%d-%b-%y", "%d%b%y", "%d-%B-%y", "%d%B%y")


@st.composite
def _expiry_scenario(draw):
    """Draw (list of unique dates, one format per date, today, permutation).

    Unique dates guarantee a single earliest date (and therefore a single
    correct expiry string), while per-date format choices exercise the
    multi-format parser.
    """
    dates = draw(
        st.lists(
            st.dates(min_value=_MIN_DATE, max_value=_MAX_DATE),
            min_size=1,
            max_size=12,
            unique=True,
        )
    )
    fmts = draw(
        st.lists(
            st.sampled_from(_FORMATS),
            min_size=len(dates),
            max_size=len(dates),
        )
    )
    today = draw(st.dates(min_value=_MIN_DATE, max_value=_MAX_DATE))
    order = draw(st.permutations(list(range(len(dates)))))
    return dates, fmts, today, order


@settings(max_examples=100)
@given(scenario=_expiry_scenario())
def test_weekly_expiry_is_earliest_on_or_after_today(scenario):
    dates, fmts, today, order = scenario

    # Format each date into one of the supported string formats, keeping the
    # date -> string mapping so we can assert the exact returned string.
    formatted = [(d, d.strftime(fmt)) for d, fmt in zip(dates, fmts, strict=True)]

    # Present the strings to the resolver in shuffled order.
    shuffled = [formatted[i][1] for i in order]

    client = FakeOpenAlgoClient()
    client.expiry_response = {"status": "success", "data": list(shuffled)}

    future_or_today = sorted(d for d in dates if d >= today)

    if not future_or_today:
        # No expiry on/after today -> resolution fails, so no entry orders.
        with pytest.raises(core.ExpiryError):
            core.resolve_weekly_expiry(
                client, underlying="NIFTY", fno_exchange="NFO", today=today
            )
        return

    earliest = future_or_today[0]
    expected_str = next(s for d, s in formatted if d == earliest)

    result = core.resolve_weekly_expiry(
        client, underlying="NIFTY", fno_exchange="NFO", today=today
    )

    # The resolver returns the original SDK string unmodified, and it must be
    # the one corresponding to the earliest date on/after today.
    assert result == expected_str
    assert result in shuffled
    assert core._parse_expiry_string(result) == earliest


def test_weekly_expiry_all_past_raises():
    """When every expiry is before today, resolution fails (Req 5.5)."""
    client = FakeOpenAlgoClient()
    client.expiry_response = {
        "status": "success",
        "data": ["01-JAN-20", "15-FEB-20", "10-MAR-20"],
    }

    with pytest.raises(core.ExpiryError):
        core.resolve_weekly_expiry(
            client,
            underlying="NIFTY",
            fno_exchange="NFO",
            today=date(2025, 1, 1),
        )


def test_weekly_expiry_selects_earliest_future_across_formats():
    """Mixed formats: earliest date on/after today is selected (Req 5.2)."""
    client = FakeOpenAlgoClient()
    # Deliberately unsorted and using different supported formats.
    client.expiry_response = {
        "status": "success",
        "data": ["30DEC25", "02-JAN-26", "25-DEC-25", "18December25"],
    }

    result = core.resolve_weekly_expiry(
        client,
        underlying="NIFTY",
        fno_exchange="NFO",
        today=date(2025, 12, 26),
    )

    # 25-DEC is past; earliest on/after 26-DEC is 30-DEC (string "30DEC25").
    assert result == "30DEC25"
