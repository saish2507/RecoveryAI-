"""Priority scoring — the two formulas, and the ordering they produce.

Both exist to replace FIFO, which is the default ordering of any queue nobody
thought about and is wrong in two different ways here: it works cheap cases
ahead of expensive ones, and it shows reviewers confident decisions ahead of
uncertain ones.
"""

from __future__ import annotations

import pytest
from conftest import make_event
from recoveryai.core.cases import (
    NO_CONFIDENCE,
    CaseStore,
    effective_confidence,
    intake_priority,
    review_priority,
)
from recoveryai.core.diagnosis import urgency_multiplier
from recoveryai.core.llm.base import ToolCall

# ── Intake priority: amount × urgency ──────────────────────────────


def any_decision(governed_llm):
    """A model that will answer, for tests that need *a* decision but not a specific one.

    Every case is decided by the model and nothing decides in its absence, so a
    test that wants a step to exist has to supply an answer.
    """
    return governed_llm([ToolCall("send_nudge", {}, "chase it", 0.7)] * 8)


def test_larger_amount_outranks_smaller_all_else_equal() -> None:
    big = make_event("cart", amount=40_000, payment_gateway_error_code="card_declined")
    small = make_event("cart", amount=200, payment_gateway_error_code="card_declined")
    assert intake_priority(big) > intake_priority(small)


def test_urgency_can_outrank_a_larger_amount() -> None:
    """The reason the multiplier exists: value alone is not the same as urgency."""
    fresh_dispute = make_event("b2b", amount=10_000, dispute_flag=True, days_overdue=2)
    stale_invoice = make_event("b2b", amount=12_000, days_overdue=110, payment_history_score=0.9)
    assert intake_priority(fresh_dispute) > intake_priority(stale_invoice)


def test_high_ltv_outranks_low_ltv_at_equal_value() -> None:
    high = make_event("cart", amount=5_000, ltv="high", payment_gateway_error_code="card_declined")
    low = make_event("cart", amount=5_000, ltv="low", payment_gateway_error_code="card_declined")
    assert intake_priority(high) > intake_priority(low)


def test_b2b_urgency_decays_with_age() -> None:
    """Collectability falls with age, so a fresher invoice is the better use of effort."""
    recent = make_event("b2b", amount=5_000, days_overdue=5, payment_history_score=0.9)
    old = make_event("b2b", amount=5_000, days_overdue=100, payment_history_score=0.9)
    assert intake_priority(recent) > intake_priority(old)


def test_autopay_urgency_rises_with_failed_retries() -> None:
    """Each failure raises the odds this becomes churn rather than a late payment."""
    first = make_event("autopay", amount=1_000, bank_error_code="INSUFFICIENT_FUNDS", retry_count=0)
    fourth = make_event("autopay", amount=1_000, bank_error_code="INSUFFICIENT_FUNDS", retry_count=3)
    assert intake_priority(fourth) > intake_priority(first)


@pytest.mark.parametrize("vertical", ["cart", "b2b", "autopay"])
def test_urgency_multiplier_stays_within_sane_bounds(vertical: str) -> None:
    """Bounded so urgency can tilt the ordering but never swamp the amount."""
    for tier in ("low", "medium", "high"):
        for extreme in ({}, {"days_overdue": 9999, "retry_count": 99, "dispute_flag": True}):
            event = make_event(vertical, amount=1_000, ltv=tier, **extreme)
            assert 0.5 <= urgency_multiplier(event) <= 2.0


# ── Review priority: (1 − confidence) × amount ─────────────────────


def test_uncertain_large_case_outranks_confident_small_one() -> None:
    """The scenario the formula was written for."""
    assert review_priority(amount=40_000, confidence=0.5) > review_priority(amount=200, confidence=0.2)


def test_uncertainty_breaks_ties_at_equal_value() -> None:
    assert review_priority(10_000, 0.3) > review_priority(10_000, 0.9)


def test_certainty_drives_review_priority_to_zero() -> None:
    """A decision the agent is sure about does not need a reviewer's attention."""
    assert review_priority(50_000, 1.0) == 0.0


def test_review_priority_never_goes_negative() -> None:
    assert review_priority(1_000, 1.5) == 0.0


def test_guardrail_ceiling_case_ranks_below_a_genuine_judgement_call() -> None:
    """A case that escalated only because a limit was hit is not a hard question.

    The agent is confident about those, so they sink; a big uncertain call rises.
    """
    hit_a_limit = review_priority(amount=200, confidence=0.95)
    genuinely_unsure = review_priority(amount=40_000, confidence=0.45)
    assert genuinely_unsure > hit_a_limit


# ── A decision nobody scored ───────────────────────────────────────
#
# A step can carry no confidence at all: a forced escalation, or a guardrail
# that left exactly one legal move. That is categorically different from a
# decider that looked and was unsure, and the queue has to treat it as the more
# urgent of the two rather than crashing or quietly assuming a middling 0.5.


def test_absent_confidence_scores_as_maximum_uncertainty() -> None:
    assert effective_confidence(None) == NO_CONFIDENCE
    assert review_priority(1_000, None) == 1_000.0


def test_absent_confidence_is_not_confused_with_a_confident_zero() -> None:
    """`confidence or 0.0` would collapse these two; they must stay distinct."""
    assert effective_confidence(0.0) == 0.0
    assert effective_confidence(None) == NO_CONFIDENCE
    # Same score, different provenance — the point is that neither raises.
    assert review_priority(500, 0.0) == review_priority(500, None)


def test_unscored_case_outranks_a_larger_confident_one() -> None:
    """The ordering that matters: nobody judged it, so a human must."""
    unscored = review_priority(amount=10_000, confidence=None)
    large_and_confident = review_priority(amount=90_000, confidence=0.95)
    assert unscored > large_and_confident


def test_review_queue_puts_an_unscored_step_on_top(session, settings) -> None:
    """End to end through the stored column and the queue's real ordering."""
    from recoveryai.core.models import ActionIntent, ExecutionResult, Vertical
    from recoveryai.db.models import Case
    from sqlalchemy import select

    store = CaseStore(session)

    def decided(amount: float, confidence: float | None) -> Case:
        case, _ = store.create_from_event(
            make_event("cart", amount=amount, payment_gateway_error_code="card_declined")
        )
        store.record_step(
            case,
            ActionIntent(
                case_id=case.id,
                step_number=1,
                vertical=Vertical.cart,
                customer_id=case.customer_id,
                amount=case.amount,
                proposed_action="send_nudge",
                final_action="send_nudge",
                confidence=confidence,
            ),
            ExecutionResult(status="executed"),
            {},
            False,
        )
        return case

    confident_whale = decided(90_000.0, 0.95)
    unscored = decided(10_000.0, None)
    session.flush()

    ordered = list(session.scalars(select(Case).order_by(Case.priority_score.desc())))
    assert ordered[0].id == unscored.id
    assert ordered[1].id == confident_whale.id
    assert unscored.latest_confidence is None


# ── The stored column, end to end ──────────────────────────────────


def test_new_case_is_scored_on_intake_then_reswitched_on_decision(session, settings, governed_llm):
    """One column, two formulas, switched by whether a decision exists yet."""
    from recoveryai.core.agent import RecoveryAgent

    store = CaseStore(session)
    event = make_event("cart", amount=1_000, payment_gateway_error_code="card_declined")

    case, _ = store.create_from_event(event)
    assert case.priority_score == pytest.approx(intake_priority(event))
    assert case.latest_confidence is None

    RecoveryAgent(settings=settings, llm=any_decision(governed_llm)).advance_case(session, case)

    assert case.latest_confidence is not None
    assert case.priority_score == pytest.approx(review_priority(case.amount, case.latest_confidence))


def test_work_queue_orders_undecided_cases_by_value_at_risk(session, settings):
    """More open work than throughput is the normal case; FIFO wastes it."""
    store = CaseStore(session)
    for amount in (200.0, 40_000.0, 3_500.0):
        store.create_from_event(
            make_event("cart", amount=amount, payment_gateway_error_code="card_declined")
        )
    session.flush()

    from recoveryai.db.models import Case
    from sqlalchemy import select

    ordered = list(
        session.scalars(
            select(Case).where(Case.status == "new").order_by(Case.priority_score.desc())
        )
    )
    assert [c.amount for c in ordered] == [40_000.0, 3_500.0, 200.0]
