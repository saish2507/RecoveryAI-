"""Missing signals must read as "unknown", never as "best case".

A first-time customer has no payment history. The previous behaviour defaulted
the missing score to 1.0 — a perfect record asserted from zero evidence — and
returned it at 0.9 confidence, which is above the ambiguity threshold. The case
therefore skipped the model *and* sank below the review queue.

That is the worst possible combination: maximum certainty on minimum evidence.
These tests pin the corrected behaviour.
"""

from __future__ import annotations

import pytest
from conftest import make_event
from recoveryai.core.diagnosis import (
    AMBIGUITY_THRESHOLD,
    diagnose_b2b,
    diagnose_cart,
)
from recoveryai.core.llm.base import ToolCall
from recoveryai.db.models import Case


def any_decision(governed_llm):
    """A model that will answer, for tests that need *a* decision but not a specific one.

    Every case is decided by the model and nothing decides in its absence, so a
    test that wants a step to exist has to supply an answer.
    """
    return governed_llm([ToolCall("send_nudge", {}, "chase it", 0.7)] * 8)


def b2b(**meta):
    return diagnose_b2b(make_event("b2b", amount=80_000, ltv="high", **meta))


def cart(**meta):
    return diagnose_cart(make_event("cart", amount=5_000, **meta))


# ── B2B payment history ────────────────────────────────────────────


@pytest.mark.parametrize(
    "meta",
    [
        {"days_overdue": 3},  # field absent entirely
        {"days_overdue": 3, "payment_history_score": None},  # explicitly null
        {"days_overdue": 3, "payment_history_score": ""},  # empty string
    ],
    ids=["absent", "null", "empty"],
)
def test_unknown_history_is_not_treated_as_a_perfect_record(meta) -> None:
    _diagnosis, confidence, reason = b2b(**meta)

    assert confidence < 0.5, "an unknown customer must not be scored confidently"
    assert "no payment history" in reason


def test_unknown_history_routes_to_the_agent_rather_than_the_rules() -> None:
    """The whole point: not knowing is exactly what the LLM is for."""
    _diagnosis, confidence, _reason = b2b(days_overdue=3)
    assert confidence <= AMBIGUITY_THRESHOLD


def test_a_first_time_customer_is_scored_differently_from_a_proven_one() -> None:
    """The bug in one assertion: these two used to be indistinguishable."""
    _unknown_d, unknown_conf, _ = b2b(days_overdue=3)
    _known_d, known_conf, _ = b2b(days_overdue=3, payment_history_score=0.95)

    assert unknown_conf < known_conf


def test_unknown_history_still_opens_with_the_cheap_safe_action() -> None:
    """Low confidence is not an excuse to escalate a three-day-old invoice."""
    diagnosis, _confidence, _reason = b2b(days_overdue=3)
    assert diagnosis == "forgot"  # → send_nudge in the policy table


def test_a_very_overdue_unknown_customer_leans_toward_distress() -> None:
    diagnosis, confidence, reason = b2b(days_overdue=90)
    assert diagnosis == "cash_flow_trouble"
    assert confidence <= AMBIGUITY_THRESHOLD
    assert "no payment history" in reason


def test_a_known_record_still_drives_the_confident_paths() -> None:
    """The fix must not blunt the rules when evidence actually exists."""
    assert b2b(days_overdue=3, payment_history_score=0.95)[1] == pytest.approx(0.9)
    assert b2b(days_overdue=3, payment_history_score=0.2)[0] == "cash_flow_trouble"
    assert b2b(days_overdue=20, payment_history_score=0.6)[1] == pytest.approx(0.5)


def test_a_dispute_outranks_an_unknown_history() -> None:
    """Escalation on a dispute is not weakened by missing history."""
    diagnosis, confidence, _reason = b2b(days_overdue=3, dispute_flag=True)
    assert diagnosis == "disputed"
    assert confidence == 1.0


# ── Cart price baseline ────────────────────────────────────────────


def test_a_first_time_shopper_has_no_price_baseline() -> None:
    """Same bug class: no `price_vs_customer_avg` means absent, not average."""
    diagnosis, confidence, reason = cart(session_duration_seconds=200)

    assert diagnosis == "distraction"  # cheap, safe opening move
    assert confidence <= AMBIGUITY_THRESHOLD
    assert "no spending baseline" in reason


def test_a_known_price_ratio_still_drives_the_confident_path() -> None:
    diagnosis, confidence, _reason = cart(session_duration_seconds=200, price_vs_customer_avg=1.5)
    assert diagnosis == "price_sensitivity"
    assert confidence == pytest.approx(0.9)


def test_an_unknown_baseline_never_justifies_a_discount() -> None:
    """A coupon on someone whose spending you cannot compare is a guess.

    Asserted against the guardrail rather than a lookup table: the model
    proposes every action now, so the guardrail is the only thing that can
    actually stop this one.
    """
    from recoveryai.core.policy import GuardrailCapacity, check_cart_guardrails

    diagnosis, _confidence, _reason = cart(session_duration_seconds=200)
    allowed, reason = check_cart_guardrails("send_discount", GuardrailCapacity(), diagnosis)
    assert allowed is False
    assert diagnosis in reason


# ── The consequence that motivated the fix ─────────────────────────


def test_a_large_unknown_case_now_outranks_a_small_confident_one_for_review() -> None:
    """It used to sink: (1 − 0.9) × amount put it below trivial escalations."""
    from recoveryai.core.cases import review_priority

    _d, unknown_conf, _r = b2b(days_overdue=3)
    big_unknown = review_priority(80_000, unknown_conf)
    small_certain = review_priority(500, 0.95)

    assert big_unknown > small_certain


def test_the_evidence_for_a_low_confidence_reaches_the_model(session, settings, governed_llm) -> None:
    """The model must be told *why* the prior is weak, not just how weak.

    "forgot / high, confidence 0.35" is a number with no argument behind it;
    "no payment history on file" is the part that lets the model — and the
    reviewer reading the stored snapshot afterwards — judge whether to trust it.
    """
    from recoveryai.core.agent import RecoveryAgent

    llm = any_decision(governed_llm)
    agent = RecoveryAgent(settings=settings, llm=llm)
    case, _decision, _ = agent.handle_event(
        session, make_event("b2b", amount=80_000, ltv="high", days_overdue=3)
    )

    prior = case.steps[0].context_snapshot["rule_based_prior"]
    assert prior["confidence"] < 0.5
    assert "no payment history" in prior["explanation"]
    assert "no payment history" in llm.fake_provider.calls[0]["user_prompt"]


# ── Simulator walk position ────────────────────────────────────────


def test_the_permutation_walk_resumes_instead_of_replaying_lap_zero(db, session) -> None:
    """A restart must not re-mint `cart_perm0_c0` on top of the existing one.

    When the walk position lived only in memory, every restart replayed lap zero:
    duplicate rows in the queue, and a supposedly fresh customer who had already
    spent their one 90-day discount in the previous run — so the guardrail fired
    for reasons the case itself did not explain.
    """
    from recoveryai.api.main import _resume_permutation_index
    from recoveryai.core.agent import RecoveryAgent
    from recoveryai.simulator import build_permutation_event

    assert _resume_permutation_index() == 0

    agent = RecoveryAgent()
    for i in range(3):
        event, _ = build_permutation_event(i)
        agent.handle_event(session, event)
    session.commit()

    assert _resume_permutation_index() == 3

    # The next id the walk would emit must not collide with one already stored.
    stored = {c.customer_id for c in session.query(Case).all()}
    resumed_event, _ = build_permutation_event(_resume_permutation_index())
    assert resumed_event.customer_id not in stored


def test_a_wiped_database_correctly_restarts_the_walk(db, session) -> None:
    """Nothing left to collide with, so lap zero is the right place to begin."""
    from recoveryai.api.main import _resume_permutation_index

    session.query(Case).delete()
    session.commit()
    assert _resume_permutation_index() == 0


def test_manually_injected_scenarios_do_not_shift_the_walk(db, session) -> None:
    """Named demo scenarios use stable ids on purpose; they are not walk output."""
    from recoveryai.api.main import _resume_permutation_index
    from recoveryai.core.agent import RecoveryAgent
    from recoveryai.simulator import build_scenario

    agent = RecoveryAgent()
    agent.handle_event(session, build_scenario("cart_price_sensitive"))
    session.commit()

    assert _resume_permutation_index() == 0
