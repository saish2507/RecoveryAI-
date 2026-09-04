"""Scoring a shadow pilot against what actually happened.

Shadow mode answers "what would this have done". These cover the other half:
whether those decisions were any good, judged against outcomes the host reported
through the normal `/actions/{intent_id}/report` path.

The measure is coarse on purpose — agreement about *recoverability*, not about
action names — and the tests pin that definition so nobody later reads the
number as something stronger than it is.
"""

from __future__ import annotations

from conftest import make_event
from recoveryai.core.cases import CaseStore
from recoveryai.core.models import ActionIntent, ActionStatus, ExecutionResult, Vertical
from recoveryai.core.shadow_eval import evaluate_shadow_decisions


def shadow_step(session, action: str, amount: float = 5_000.0) -> ActionIntent:
    """Record one shadow decision, exactly as `ShadowExecutor` would."""
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", amount, customer_id=f"c_{action}_{amount}"))
    intent = ActionIntent(
        case_id=case.id,
        step_number=1,
        vertical=Vertical.cart,
        customer_id=case.customer_id,
        amount=case.amount,
        proposed_action=action,
        final_action=action,
        confidence=0.7,
    )
    store.record_step(
        case,
        intent,
        ExecutionResult(status=ActionStatus.shadow_logged, details="[SHADOW] dispatched nothing"),
        {},
        False,
    )
    session.flush()
    return intent


def report(session, intent: ActionIntent, recovered: float) -> None:
    CaseStore(session).apply_host_report(
        intent_id=str(intent.intent_id),
        status=ActionStatus.executed.value,
        details="our own process handled it",
        cost=0.0,
        recovered_amount=recovered,
    )
    session.flush()


# ── The four verdicts ──────────────────────────────────────────────


def test_acting_on_a_case_that_paid_is_an_agreement(session) -> None:
    intent = shadow_step(session, "send_nudge")
    report(session, intent, recovered=5_000.0)

    result = evaluate_shadow_decisions(session)

    assert result.evaluated == 1
    assert result.agreements == 1
    assert result.by_verdict == {"agreed_worked": 1}


def test_giving_up_on_a_case_that_never_paid_is_an_agreement(session) -> None:
    intent = shadow_step(session, "escalate_to_human")
    report(session, intent, recovered=0.0)

    result = evaluate_shadow_decisions(session)

    assert result.agreements == 1
    assert result.by_verdict == {"agreed_gave_up": 1}


def test_acting_on_a_case_that_never_paid_is_a_disagreement(session) -> None:
    intent = shadow_step(session, "send_nudge")
    report(session, intent, recovered=0.0)

    result = evaluate_shadow_decisions(session)

    assert result.disagreements == 1
    assert result.by_verdict == {"acted_but_nothing_recovered": 1}


def test_giving_up_on_a_case_that_paid_is_the_expensive_disagreement(session) -> None:
    """Revenue the agent would have left alone. The direction that matters."""
    intent = shadow_step(session, "close_as_unrecoverable")
    report(session, intent, recovered=9_000.0)

    result = evaluate_shadow_decisions(session)

    assert result.disagreements == 1
    assert result.by_verdict == {"gave_up_but_money_arrived": 1}
    assert result.comparisons[0].agent_would_have_acted is False
    assert result.comparisons[0].recovered_amount == 9_000.0


def test_waiting_counts_as_declining_to_act_now(session) -> None:
    intent = shadow_step(session, "wait_and_reassess")
    report(session, intent, recovered=0.0)

    assert evaluate_shadow_decisions(session).by_verdict == {"agreed_gave_up": 1}


# ── The population ─────────────────────────────────────────────────


def test_unreported_shadow_decisions_are_pending_not_wrong(session) -> None:
    """A pilot's first day has no agreement rate. Counting silence as
    disagreement would make the agent look wrong about everything."""
    shadow_step(session, "send_nudge")
    shadow_step(session, "send_nudge", amount=6_000.0)

    result = evaluate_shadow_decisions(session)

    assert result.shadow_steps == 2
    assert result.evaluated == 0
    assert result.awaiting_outcome == 2
    assert result.disagreements == 0
    assert result.agreement_rate is None


def test_a_zero_recovery_report_still_counts_as_evaluated(session) -> None:
    """"Executed, recovered nothing" is a real answer, not silence."""
    intent = shadow_step(session, "send_nudge")
    report(session, intent, recovered=0.0)

    result = evaluate_shadow_decisions(session)
    assert result.evaluated == 1
    assert result.awaiting_outcome == 0


def test_the_summary_mixes_agreements_and_disagreements(session) -> None:
    report(session, shadow_step(session, "send_nudge", 1_000.0), recovered=1_000.0)
    report(session, shadow_step(session, "send_nudge", 2_000.0), recovered=2_000.0)
    report(session, shadow_step(session, "send_nudge", 3_000.0), recovered=0.0)
    shadow_step(session, "send_nudge", 4_000.0)  # still pending

    result = evaluate_shadow_decisions(session)

    assert result.shadow_steps == 4
    assert result.evaluated == 3
    assert result.agreements == 2
    assert result.disagreements == 1
    assert result.agreement_rate == 0.6667
    assert result.awaiting_outcome == 1


def test_live_decisions_are_not_scored_as_shadow_ones(session) -> None:
    """The population is shadow decisions only; a live action that recovered
    money is not evidence about a decision nobody made in shadow."""
    store = CaseStore(session)
    case, _ = store.create_from_event(make_event("cart", 5_000, customer_id="live_one"))
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
        ),
        ExecutionResult(status=ActionStatus.executed, recovered_amount=5_000.0),
        {},
        False,
    )
    session.flush()

    assert evaluate_shadow_decisions(session).shadow_steps == 0


def test_the_shadow_marker_survives_the_outcome_report(session) -> None:
    """The reason `was_shadow` is a column and not a reading of `action_status`:
    the report overwrites the status, and keying off it would delete every step
    from the population at the moment it became evaluable."""
    intent = shadow_step(session, "send_nudge")
    report(session, intent, recovered=5_000.0)

    case = CaseStore(session).get(intent.case_id)
    assert case.steps[0].action_status == ActionStatus.executed.value
    assert case.steps[0].was_shadow is True
    assert evaluate_shadow_decisions(session).shadow_steps == 1


# ── Over HTTP ──────────────────────────────────────────────────────


def test_the_endpoint_reports_the_summary(client) -> None:
    response = client.get("/api/v1/system/shadow-evaluation")
    assert response.status_code == 200

    body = response.json()
    assert body["shadow_steps"] == 0
    assert body["agreement_rate"] is None
    assert body["by_verdict"] == {}


def test_the_endpoint_scores_a_shadow_pilot_end_to_end(client) -> None:
    """Shadow decisions in, host outcome back through the public report path,
    agreement out — the whole pilot loop over HTTP."""
    from recoveryai.core.execution import ShadowExecutor

    # Wrapped after the app is built, because `build_executor` read `AGENT_MODE`
    # at construction time. This is the same wrapping shadow mode performs.
    agent = client.app.state.agent
    agent.executor = ShadowExecutor(agent.executor)

    created = client.post(
        "/api/v1/events",
        json={
            "vertical": "cart",
            "customer_id": "pilot_cust",
            "customer_ltv_tier": "high",
            "amount": 7_500.0,
            "raw_failure_reason": "card_declined",
            "vertical_metadata": {"payment_gateway_error_code": "card_declined"},
        },
    )
    assert created.status_code == 201

    detail = client.get(f"/api/v1/cases/{created.json()['id']}").json()
    step = detail["steps"][0]
    assert step["action_status"] == "shadow_logged"

    reported = client.post(
        f"/api/v1/actions/{step['intent_id']}/report",
        json={"status": "executed", "details": "our team called them", "recovered_amount": 7_500.0},
    )
    assert reported.status_code == 200

    body = client.get("/api/v1/system/shadow-evaluation").json()
    assert body["shadow_steps"] == 1
    assert body["evaluated"] == 1
    assert body["agreements"] == 1
    assert body["agreement_rate"] == 1.0
