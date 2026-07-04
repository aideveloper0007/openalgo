"""Property-based test for the bounded-retry helper.

Implements Property 11 from the design: across any sequence of failing SDK
operations — entry-leg placement (Req 6.5), average-fill retrieval (Req 7.6),
or square-off (Req 10.6) — :func:`bjp_core.run_with_retry`:

    * never makes more than :data:`bjp_core.MAX_RETRY_ATTEMPTS` (3) attempts;
    * stops retrying on the first success (no further attempts once one
      attempt satisfies the success predicate);
    * preserves the successful value and captures failures without discarding
      an earlier success, since the helper is state-free and only decides
      *whether to attempt again*.

The three real call sites share this one helper, so exercising it directly with
a generated outcome sequence covers all three requirements at once. The
``operation`` under test is a fake SDK call whose per-attempt outcome is driven
by a generated list of ``success`` / ``fail`` / ``raise`` tokens, mirroring a
flaky ``placeorder`` / ``orderstatus`` / square-off call. A caller-managed call
log verifies the "stop on first success" and "never exceed 3" invariants, and an
injected ``sleep`` keeps the inter-attempt ``delay`` (e.g. the 2s average-fill
wait) instant while still being counted.

The production code under test lives in ``strategies/bjp_portfolio/bjp_core.py``
and is imported as ``bjp_core`` via the ``sys.path`` setup in ``conftest.py``.
"""

from __future__ import annotations

import bjp_core as core
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Feature: bjp-portfolio-strategies, Property 11: Retry attempts are bounded by
# three. For any sequence of failing SDK operations (entry-leg placement,
# average-fill retrieval, or square-off), run_with_retry makes at most
# MAX_RETRY_ATTEMPTS (3) attempts, returns on the first success without making
# further attempts, and preserves the successful/recorded value. The
# per-attempt outcome sequence, the caller-supplied max_attempts (including
# out-of-range values that must clamp to [1, 3]), and the inter-attempt delay
# are all generated. Validates: Requirements 6.5, 7.6, 10.6.

_OUTCOMES = ("success", "fail", "raise")

#: Labels tying the generated scenario back to the three real call sites so the
#: retry-exhausted log message is exercised with each requirement's wording.
_DESCRIPTIONS = ("entry-leg placement", "average-fill retrieval", "square-off")


def _make_operation(outcomes, calls):
    """Build a fake SDK operation driven by ``outcomes``.

    Each call consumes the next token: ``success`` returns a unique truthy
    value (the 1-based attempt number, mirroring a captured order id / fill),
    ``fail`` returns ``None`` (rejected by the default non-``None`` predicate),
    and ``raise`` raises ``RuntimeError``. Calls beyond the generated list
    default to ``fail`` so the operation is safe to invoke any number of times.
    Every invocation appends its attempt number to ``calls`` so the test can
    assert the helper stopped on first success and never exceeded the cap.
    """

    def operation():
        attempt = len(calls) + 1
        calls.append(attempt)
        token = outcomes[attempt - 1] if attempt - 1 < len(outcomes) else "fail"
        if token == "raise":
            raise RuntimeError(f"attempt {attempt} boom")
        if token == "success":
            return attempt
        return None

    return operation


@st.composite
def _retry_scenarios(draw):
    """Generate ``(outcomes, max_attempts, delay, description)``.

    ``max_attempts`` intentionally spans below 1 and above the cap so the
    ``[1, MAX_RETRY_ATTEMPTS]`` clamp is exercised. ``delay`` is either 0 (no
    wait) or a positive value (e.g. the 2s average-fill interval), always fed a
    fake ``sleep`` so tests stay instant.
    """
    outcomes = draw(
        st.lists(st.sampled_from(_OUTCOMES), min_size=0, max_size=8)
    )
    max_attempts = draw(st.integers(min_value=-2, max_value=8))
    delay = draw(st.sampled_from([0.0, 2.0]))
    description = draw(st.sampled_from(_DESCRIPTIONS))
    return outcomes, max_attempts, delay, description


@settings(max_examples=100)
@given(scenario=_retry_scenarios())
# Every attempt fails: attempts pinned at the 3-attempt cap.
@example(scenario=(["fail", "fail", "fail", "fail"], 3, 0.0, "square-off"))
# Immediate success: exactly one attempt, no retries.
@example(scenario=(["success"], 3, 0.0, "entry-leg placement"))
# Success on the third (last permitted) attempt.
@example(scenario=(["fail", "fail", "success"], 3, 2.0, "average-fill retrieval"))
# Raises then succeeds: exception captured, success still returned.
@example(scenario=(["raise", "success"], 3, 0.0, "square-off"))
# max_attempts far above the cap must clamp to 3.
@example(scenario=(["fail", "fail", "fail", "success"], 99, 0.0, "square-off"))
def test_retry_attempts_are_bounded_by_three(scenario):
    outcomes, max_attempts, delay, description = scenario

    calls: list[int] = []
    sleeps: list[float] = []
    operation = _make_operation(outcomes, calls)

    result = core.run_with_retry(
        operation,
        max_attempts=max_attempts,
        delay=delay,
        sleep=sleeps.append,
        description=description,
    )

    # The cap the helper actually enforces: caller value clamped to [1, 3].
    cap = max(1, min(int(max_attempts), core.MAX_RETRY_ATTEMPTS))
    assert 1 <= cap <= core.MAX_RETRY_ATTEMPTS

    # Recompute the expected outcome from the per-attempt tokens, considering
    # only the attempts the helper is permitted to make.
    first_success = None
    for i in range(cap):
        token = outcomes[i] if i < len(outcomes) else "fail"
        if token == "success":
            first_success = i + 1  # 1-based attempt number
            break

    if first_success is not None:
        expected_attempts = first_success
        expected_succeeded = True
    else:
        expected_attempts = cap
        expected_succeeded = False

    # Core Property 11 invariants.
    # 1. Attempts never exceed the three-attempt bound.
    assert result.attempts <= core.MAX_RETRY_ATTEMPTS
    # 2. Attempts match the clamp-and-first-success computation exactly.
    assert result.attempts == expected_attempts
    # 3. The operation was invoked exactly ``attempts`` times: it stopped on the
    #    first success and never ran past the cap (stop-on-first-success bound).
    assert calls == list(range(1, expected_attempts + 1))
    # 4. Success flag matches whether a success occurred within the cap.
    assert result.succeeded is expected_succeeded

    if expected_succeeded:
        # The successful value is preserved (the attempt number it returned),
        # never discarded by a later attempt (there is no later attempt).
        assert result.value == first_success
    else:
        # No attempt satisfied the predicate: the final value is the last
        # non-raising return (``None`` here) and nothing was recorded as success.
        assert result.value is None

    # Captured errors correspond exactly to the ``raise`` tokens among the
    # attempts actually made, preserved in attempt order.
    expected_error_count = sum(
        1
        for i in range(expected_attempts)
        if (outcomes[i] if i < len(outcomes) else "fail") == "raise"
    )
    assert len(result.errors) == expected_error_count

    # The inter-attempt delay is applied once between consecutive made attempts
    # when positive, and never after the final attempt or a success.
    expected_sleeps = (expected_attempts - 1) if delay > 0 else 0
    assert len(sleeps) == expected_sleeps
    assert all(s == delay for s in sleeps)
