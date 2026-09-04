"""Optimistic concurrency on `Case`, and what callers do when it fires.

Two writers genuinely race in this system. The follow-up scheduler advances a
case on an APScheduler thread while a host reports the outcome of an earlier
action on a request thread, and both read-modify-write the same row through
different sessions. Under last-write-wins one of them silently loses — and the
loser can be the recovery report, which is the number the whole product exists
to produce.

These tests drive the race directly with two sessions rather than with threads:
the failure mode is about *stale reads*, not about timing, and a thread-based
test of it would be slow and flaky while proving less.
"""

from __future__ import annotations

import pytest
from conftest import make_event
from recoveryai.core.cases import CaseStore, ConcurrentModificationError
from recoveryai.core.models import (
    ActionIntent,
    ActionStatus,
    CaseStatus,
    ExecutionResult,
    Vertical,
)


def _intent(case, step_number: int = 1, action: str = "send_nudge") -> ActionIntent:
    return ActionIntent(
        case_id=case.id,
        step_number=step_number,
        vertical=Vertical(case.vertical),
        customer_id=case.customer_id,
        amount=case.amount,
        proposed_action=action,
        final_action=action,
        confidence=0.6,
    )


def _seed(session) -> str:
    case, _ = CaseStore(session).create_from_event(
        make_event("cart", 4_000, payment_gateway_error_code="card_declined")
    )
    session.commit()
    return case.id


# ── The version column itself ──────────────────────────────────────


def test_a_new_case_starts_versioned(session) -> None:
    case, _ = CaseStore(session).create_from_event(make_event("cart", 500))
    session.flush()
    assert case.version == 1


def test_the_version_advances_on_every_write(session) -> None:
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", 500))
    session.flush()
    first = case.version

    store.record_step(case, _intent(case), ExecutionResult(status="executed"), {}, False)
    assert case.version > first


# ── The race ───────────────────────────────────────────────────────


def test_the_second_writer_is_refused_rather_than_silently_winning(db) -> None:
    """The whole point: a lost update becomes loud instead of invisible."""
    factory = db.get_session_factory()
    setup = factory()
    case_id = _seed(setup)
    setup.close()

    a, b = factory(), factory()
    try:
        case_a = CaseStore(a).get(case_id)
        case_b = CaseStore(b).get(case_id)  # both hold the same version

        CaseStore(a).record_step(
            case_a, _intent(case_a), ExecutionResult(status="executed"), {}, False
        )
        a.commit()

        with pytest.raises(ConcurrentModificationError) as exc:
            CaseStore(b).record_step(
                case_b, _intent(case_b, step_number=2), ExecutionResult(status="executed"), {}, False
            )
        assert exc.value.case_id == case_id
    finally:
        a.close()
        b.rollback()
        b.close()


def test_the_winner_s_write_survives_the_conflict(db) -> None:
    """A refusal is only useful if the write that got there first is intact."""
    factory = db.get_session_factory()
    setup = factory()
    case_id = _seed(setup)
    setup.close()

    a, b = factory(), factory()
    try:
        case_a = CaseStore(a).get(case_id)
        case_b = CaseStore(b).get(case_id)

        CaseStore(a).record_step(
            case_a,
            _intent(case_a, action="send_payment_update_link"),
            ExecutionResult(status="executed", recovered_amount=4_000.0),
            {},
            False,
        )
        a.commit()

        with pytest.raises(ConcurrentModificationError):
            CaseStore(b).record_step(
                case_b, _intent(case_b, step_number=2), ExecutionResult(status="executed"), {}, False
            )
        b.rollback()
    finally:
        a.close()
        b.close()

    check = factory()
    try:
        reloaded = CaseStore(check).get(case_id)
        assert reloaded.amount_recovered == 4_000.0
        assert reloaded.step_count == 1
        assert len(reloaded.steps) == 1
    finally:
        check.close()


def test_a_sequential_writer_that_reloads_succeeds(db) -> None:
    """The conflict is about stale state, not about the row being locked."""
    factory = db.get_session_factory()
    setup = factory()
    case_id = _seed(setup)
    setup.close()

    for step_number in (1, 2):
        s = factory()
        try:
            case = CaseStore(s).get(case_id)
            CaseStore(s).record_step(
                case, _intent(case, step_number=step_number), ExecutionResult(status="executed"), {}, False
            )
            s.commit()
        finally:
            s.close()

    check = factory()
    try:
        assert CaseStore(check).get(case_id).step_count == 2
    finally:
        check.close()


# ── What the scheduler does about it ───────────────────────────────


def test_the_scheduler_skips_a_case_that_changed_underneath(db, settings, monkeypatch) -> None:
    """Skip, not retry: the other writer's state is the current truth, and the
    case is picked up by the next sweep against a fresh read."""
    from recoveryai.core.agent import RecoveryAgent
    from recoveryai.core.scheduler import FollowupScheduler

    factory = db.get_session_factory()
    setup = factory()
    store = CaseStore(setup)
    case, _ = store.create_from_event(make_event("cart", 4_000, payment_gateway_error_code="card_declined"))
    store.set_status(case, CaseStatus.in_progress)
    store.schedule_followup(case, -1.0)  # already due
    setup.commit()
    setup.close()

    agent = RecoveryAgent(settings=settings)
    scheduler = FollowupScheduler(agent=agent, session_scope=db.session_scope)

    def conflict(self, session, case):  # noqa: ANN001, ARG001
        raise ConcurrentModificationError(case.id)

    monkeypatch.setattr(RecoveryAgent, "advance_case", conflict)

    decisions = scheduler.run_due_followups()

    assert decisions == []
    assert scheduler.skipped_conflicts == 1
    # A conflict is the safety net working, not a fault.
    assert scheduler.errors == 0


# ── Guardrail capacity scope (the counting rules these locks protect) ──
#
# Scope is per-lever and deliberately not uniform; `policy.GuardrailCapacity`
# documents why. These pin the two halves against each other so a future
# "simplification" to one shared rule fails loudly.


def test_discount_budget_follows_the_customer_across_their_cases(session) -> None:
    """Margin is given to a person. Two abandoned carts must not buy two coupons."""
    store = CaseStore(session)
    signals = {"session_duration_seconds": 200, "price_vs_customer_avg": 1.4}

    first, _ = store.create_from_event(make_event("cart", 4_000, "high", "same_person", **signals))
    store.record_step(
        first, _intent(first, action="send_discount"), ExecutionResult(status="executed"), {}, False
    )
    session.flush()

    second, _ = store.create_from_event(make_event("cart", 5_000, "high", "same_person", **signals))
    assert store.capacity_for(second).discounts_in_90d == 1


def test_retry_budget_stays_inside_its_own_case(session) -> None:
    """A retry re-presents one mandate; another mandate's failures say nothing
    about whether this one will clear."""
    store = CaseStore(session)

    first, _ = store.create_from_event(
        make_event("autopay", 1_000, customer_id="same_person", bank_error_code="INSUFFICIENT_FUNDS")
    )
    store.record_step(
        first, _intent(first, action="retry_charge"), ExecutionResult(status="executed"), {}, False
    )
    session.flush()

    second, _ = store.create_from_event(
        make_event("autopay", 2_000, customer_id="same_person", bank_error_code="INSUFFICIENT_FUNDS")
    )
    assert store.capacity_for(second).autopay_retries == 0


def test_b2b_touch_budget_stays_inside_its_own_invoice(session) -> None:
    store = CaseStore(session)

    first, _ = store.create_from_event(
        make_event("b2b", 10_000, customer_id="same_org", days_overdue=20, payment_history_score=0.7)
    )
    for step_number in (1, 2, 3):
        store.record_step(
            first, _intent(first, step_number=step_number), ExecutionResult(status="executed"), {}, False
        )
    session.flush()
    assert store.capacity_for(first).b2b_touches == 3

    second, _ = store.create_from_event(
        make_event("b2b", 8_000, customer_id="same_org", days_overdue=15, payment_history_score=0.7)
    )
    assert store.capacity_for(second).b2b_touches == 0


# ── Failure reported after we already recorded success ─────────────


def test_a_late_failure_report_moves_the_step_out_of_executed(session) -> None:
    """The inconsistent state this exists to prevent: a step reading `executed`
    sitting next to a host telling us it failed."""
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", 3_000))
    intent = _intent(case)
    store.record_step(case, intent, ExecutionResult(status="executed", cost=2.0), {}, False)
    session.flush()
    assert case.steps[0].action_status == ActionStatus.executed.value

    step = store.apply_host_report(
        intent_id=str(intent.intent_id),
        status=ActionStatus.execution_failed.value,
        details="SMS gateway rejected the number",
        cost=0.0,
        recovered_amount=0.0,
    )

    assert step.action_status == ActionStatus.execution_failed.value
    assert step.action_details == "SMS gateway rejected the number"
    # The cost banked on the optimistic reading is unwound.
    assert case.cost_of_recovery == 0.0


def test_a_late_failure_returns_the_case_to_open_work(session) -> None:
    """Re-opened rather than escalated: the agent still has levers and steps."""
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", 3_000))
    intent = _intent(case)
    store.record_step(case, intent, ExecutionResult(status="executed"), {}, False)
    store.set_status(case, CaseStatus.resolved)
    session.flush()

    store.apply_host_report(
        intent_id=str(intent.intent_id),
        status=ActionStatus.execution_failed.value,
        details="bounced",
        cost=0.0,
        recovered_amount=0.0,
    )

    assert case.status == CaseStatus.in_progress.value


def test_a_late_failure_does_not_reopen_a_case_that_really_was_paid(session) -> None:
    """A recovery reported alongside a failed step still wins: money arriving is
    the one fact that ends a case whatever else is reported with it."""
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", 3_000))
    intent = _intent(case)
    store.record_step(case, intent, ExecutionResult(status="executed"), {}, False)
    session.flush()

    store.apply_host_report(
        intent_id=str(intent.intent_id),
        status=ActionStatus.execution_failed.value,
        details="the nudge bounced, but they paid anyway",
        cost=0.0,
        recovered_amount=3_000.0,
    )

    assert case.status == CaseStatus.resolved.value


def test_a_late_failure_does_not_take_a_case_back_off_a_human(session) -> None:
    """Escalation is a judgement, not a claim about money. One failed send does
    not hand the case back to the agent behind the reviewer's back."""
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", 3_000))
    intent = _intent(case)
    store.record_step(case, intent, ExecutionResult(status="executed"), {}, False)
    store.set_status(case, CaseStatus.escalated)
    session.flush()

    store.apply_host_report(
        intent_id=str(intent.intent_id),
        status=ActionStatus.execution_failed.value,
        details="bounced",
        cost=0.0,
        recovered_amount=0.0,
    )

    assert case.status == CaseStatus.escalated.value
