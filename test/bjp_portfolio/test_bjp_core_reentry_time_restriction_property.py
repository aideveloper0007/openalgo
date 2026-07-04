"""Property-based test for the re-entry time restriction.

Implements Property 19 from the design: when a strategy configures a
``Reentry_Time_Restriction`` (NF3/SENSEX3 use 284 minutes past the 09:15 IST
market open, i.e. a 13:59 IST cutoff), no re-entry may be placed at or after
that cutoff. The boundary is inclusive-disallowed: exactly at 13:59 a re-entry
is *not* permitted, while any instant strictly before it is (Req 12.4).

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the sys.path setup in ``conftest.py``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import bjp_core as core
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 19: Re-entry time restriction is
# enforced. For a strategy with reentry_time_restriction_min set, a re-entry is
# permitted iff the current IST time-of-day is strictly before the cutoff of
# 09:15 IST + restriction minutes; the cutoff instant itself and everything
# after it is disallowed. Strategies leaving the restriction unset are always
# allowed.
# Validates: Requirements 12.4.

#: The confirmed NF3/SENSEX3 restriction: 284 minutes past 09:15 IST = 13:59.
_CUTOFF_284 = time(13, 59)

# Every possible time-of-day at one-second resolution.
_times = st.times()

# The re-entry counts the backtest permits (Req 12.3); the restriction applies
# regardless of the count, so we range over all of them.
_restrictions = st.integers(min_value=1, max_value=600)


def _config(restriction_min: int | None) -> core.StrategyConfig:
    """Build a minimal ``StrategyConfig`` carrying the re-entry restriction."""
    return core.StrategyConfig(
        strategy_name="test",
        index=core.NIFTY,
        lots=1,
        entry_time="09:16:00",
        exit_time="15:22:00",
        legs=[],
        reentry_time_restriction_min=restriction_min,
    )


def _cutoff_of(restriction_min: int) -> time:
    """Independent reference computation of 09:15 + restriction minutes."""
    base = datetime.combine(date.min, time(9, 15)) + timedelta(minutes=restriction_min)
    return base.time()


def test_cutoff_for_284_is_1359():
    # The confirmed decision A5: 09:15 + 284 min == 13:59 IST.
    assert core.reentry_cutoff_time(284) == _CUTOFF_284


def test_reentry_disallowed_exactly_at_1359_boundary():
    # Boundary: a re-entry is NOT permitted at exactly the 13:59 cutoff.
    config = _config(284)
    assert core.reentry_time_allowed(config, now=_CUTOFF_284) is False


def test_reentry_allowed_one_second_before_1359():
    # One second before the cutoff a re-entry is still permitted.
    config = _config(284)
    assert core.reentry_time_allowed(config, now=time(13, 58, 59)) is True


def test_reentry_disallowed_one_second_after_1359():
    # One second past the cutoff a re-entry is disallowed.
    config = _config(284)
    assert core.reentry_time_allowed(config, now=time(13, 59, 1)) is False


@settings(max_examples=100)
@given(now=_times, restriction=_restrictions)
def test_reentry_allowed_iff_strictly_before_cutoff(now: time, restriction: int):
    # Core property: allowed iff the current time-of-day is strictly before the
    # 09:15 + restriction cutoff (inclusive-disallowed boundary).
    config = _config(restriction)
    cutoff = _cutoff_of(restriction)
    allowed = core.reentry_time_allowed(config, now=now)
    assert allowed == (now < cutoff)


@settings(max_examples=100)
@given(now=_times)
def test_no_restriction_always_allows(now: time):
    # A strategy that leaves the restriction unset is never time-blocked.
    config = _config(None)
    assert core.reentry_time_allowed(config, now=now) is True


@settings(max_examples=100)
@given(restriction=_restrictions)
def test_cutoff_instant_always_disallowed(restriction: int):
    # For any restriction, the exact cutoff instant is disallowed and the
    # instant one second earlier is allowed (when it stays the same minute-day).
    config = _config(restriction)
    cutoff = _cutoff_of(restriction)
    assert core.reentry_time_allowed(config, now=cutoff) is False


@settings(max_examples=100)
@given(now=_times, restriction=_restrictions)
def test_datetime_and_time_inputs_agree(now: time, restriction: int):
    # Passing a datetime (its time-of-day is used) must agree with passing the
    # bare time, so callers may inject either form.
    config = _config(restriction)
    as_time = core.reentry_time_allowed(config, now=now)
    as_datetime = core.reentry_time_allowed(
        config, now=datetime.combine(date(2024, 1, 1), now)
    )
    assert as_time == as_datetime
