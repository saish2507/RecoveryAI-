"""The execution seam: swappability, shadow mode, and webhook handoff.

These tests are the evidence for the central integration claim. "You can drop
our executor and use your own" is a promise that either holds under a substituted
implementation or is marketing.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeExecutor, make_event
from recoveryai.core.actions import SEND_NUDGE
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.execution import (
    ActionExecutor,
    ShadowExecutor,
    SimulatedExecutor,
    WebhookExecutor,
    build_executor,
)
from recoveryai.core.llm.base import ToolCall
from recoveryai.core.models import ActionIntent, ActionStatus, Vertical
from recoveryai.core.signing import sign_payload, verify_signature


def any_decision(governed_llm):
    """A model that will answer, for tests that need *a* decision but not a specific one.

    Every case is decided by the model and nothing decides in its absence, so a
    test that wants a step to exist has to supply an answer.
    """
    return governed_llm([ToolCall("send_nudge", {}, "chase it", 0.7)] * 8)


def make_intent(action: str = SEND_NUDGE, step_number: int = 2, **kwargs) -> ActionIntent:
    # Defaults to an observable step: most tests here are about what an executor
    # does with a decision, not about when an outcome first becomes knowable.
    return ActionIntent(
        case_id="case_x",
        step_number=step_number,
        vertical=Vertical.cart,
        customer_id="cust_1",
        amount=1000.0,
        proposed_action=action,
        final_action=action,
        **kwargs,
    )


# ── Protocol conformance ───────────────────────────────────────────


@pytest.mark.parametrize(
    "executor",
    [SimulatedExecutor(), ShadowExecutor(SimulatedExecutor()), WebhookExecutor("http://x"), FakeExecutor()],
)
def test_all_executors_satisfy_the_protocol(executor) -> None:
    assert isinstance(executor, ActionExecutor)


def test_simulated_executor_tags_its_output() -> None:
    result = SimulatedExecutor().execute(make_intent(), make_event())
    assert result.status is ActionStatus.executed
    assert "[SIMULATED]" in result.details


def test_a_landed_action_recovers_the_whole_amount(outcome) -> None:
    """An invoice is paid or it is not. There is no 15%-of-a-cart outcome."""
    outcome(True)
    result = SimulatedExecutor().execute(make_intent(step_number=2), make_event(amount=1000))
    assert result.recovered_amount == 1000.0


def test_the_first_touch_reports_no_outcome_yet(outcome) -> None:
    """Dispatch is not a result: nothing has been opened, read, or settled yet."""
    outcome(True)  # even forcing success
    result = SimulatedExecutor().execute(make_intent(step_number=1), make_event(amount=1000))
    assert result.recovered_amount == 0.0


def test_an_action_that_does_not_land_recovers_nothing(outcome) -> None:
    """The bug this replaced: six nudges booking 90% of an invoice as recovered."""
    outcome(False)
    result = SimulatedExecutor().execute(make_intent(), make_event(amount=1000))
    assert result.recovered_amount == 0.0


def test_simulated_executor_clamps_a_runaway_discount(outcome) -> None:
    """A model asking for an 80% coupon does not get one."""
    outcome(True)  # cost is only booked on a redeemed coupon
    intent = make_intent("send_discount", params={"discount_percent": 80})
    result = SimulatedExecutor().execute(intent, make_event(amount=1000))
    assert result.cost <= 250.0  # 25% ceiling


def test_an_unredeemed_coupon_costs_nothing(outcome) -> None:
    outcome(False)
    intent = make_intent("send_discount", params={"discount_percent": 20})
    result = SimulatedExecutor().execute(intent, make_event(amount=1000))
    assert result.cost == 0.0


def test_unknown_action_is_an_error_not_an_exception() -> None:
    result = SimulatedExecutor().execute(make_intent("teleport_the_money"), make_event())
    assert result.status is ActionStatus.error


# ── Executor swap ──────────────────────────────────────────────────


def test_host_can_inject_its_own_executor(session, settings, governed_llm):
    """The whole integration story in one assertion."""
    fake = FakeExecutor()
    agent = RecoveryAgent(settings=settings, executor=fake, llm=any_decision(governed_llm))

    agent.handle_event(session, make_event("cart", 500, payment_gateway_error_code="card_declined"))

    assert len(fake.executed) == 1
    assert isinstance(fake.executed[0], ActionIntent)
    assert fake.executed[0].reasoning  # a decision always arrives explained


def test_intent_reaching_the_executor_is_fully_decided(session, settings, governed_llm):
    """The host receives a finished decision, not a request to make one."""
    fake = FakeExecutor()
    llm = governed_llm([ToolCall("send_nudge", {"channel": "sms"}, "distracted shopper", 0.72)])
    agent = RecoveryAgent(settings=settings, executor=fake, llm=llm)

    agent.handle_event(session, make_event("cart", 500, payment_gateway_error_code="ODD_CODE_77"))

    intent = fake.executed[0]
    assert intent.final_action == SEND_NUDGE
    assert intent.guardrail_verdict.value == "allowed"
    assert intent.confidence == pytest.approx(0.72)
    assert intent.params["channel"] == "sms"
    # "Fully decided" now includes the words the customer will read: the executor
    # is handed copy to send, not an action name to compose one from.
    assert intent.params["message_subject"]
    assert intent.params["message_body"]


# ── Shadow mode ────────────────────────────────────────────────────


def test_shadow_mode_dispatches_nothing(session, settings, governed_llm):
    """Pilot against real traffic safely: every decision logged, none carried out."""
    settings.agent_mode = "shadow"
    inner = FakeExecutor()
    agent = RecoveryAgent(settings=settings, executor=ShadowExecutor(inner), llm=any_decision(governed_llm))

    case, decision, _ = agent.handle_event(
        session, make_event("cart", 500, payment_gateway_error_code="card_declined")
    )

    assert inner.executed == []  # nothing reached the real executor
    assert decision.result.status is ActionStatus.shadow_logged
    assert "[SHADOW]" in decision.result.details


def test_shadow_mode_still_produces_a_full_audit_trail(session, settings, governed_llm):
    """A shadow run is only useful if you can read what it would have done."""
    settings.agent_mode = "shadow"
    llm = governed_llm([ToolCall("send_discount", {"discount_percent": 15}, "price sensitive", 0.83)])
    agent = RecoveryAgent(settings=settings, executor=ShadowExecutor(FakeExecutor()), llm=llm)

    case, decision, _ = agent.handle_event(
        session,
        make_event("cart", 4_000, "high", session_duration_seconds=200, price_vs_customer_avg=1.2),
    )

    step = case.steps[0]
    assert step.proposed_action == "send_discount"
    assert step.final_action == "send_discount"
    assert step.reasoning == "price sensitive"
    assert step.confidence == pytest.approx(0.83)
    assert step.action_status == ActionStatus.shadow_logged.value
    assert step.context_snapshot["case"]["amount_at_risk"] == 4_000
    assert step.recovered_amount == 0.0  # no revenue is claimed for a decision never taken


def test_shadow_mode_still_enforces_guardrails(session, settings, governed_llm):
    """Shadow output is worthless if it reflects rules that would not apply live."""
    settings.agent_mode = "shadow"
    llm = governed_llm([ToolCall("send_discount", {"discount_percent": 10}, "price", 0.8)] * 2)
    agent = RecoveryAgent(settings=settings, executor=ShadowExecutor(FakeExecutor()), llm=llm)
    signals = {"session_duration_seconds": 200, "price_vs_customer_avg": 1.2}

    agent.handle_event(session, make_event("cart", 4_000, "high", "shadow_cust", **signals))
    second = agent.handle_event(session, make_event("cart", 4_000, "high", "shadow_cust", **signals))[1]

    assert second.intent.guardrail_verdict.value == "blocked"
    assert second.intent.final_action == SEND_NUDGE


def test_build_executor_wraps_the_configured_executor_in_shadow(settings) -> None:
    settings.agent_mode = "shadow"
    settings.action_executor = "webhook"
    settings.action_webhook_url = "https://host.example/actions"

    executor = build_executor(settings)

    assert isinstance(executor, ShadowExecutor)
    # The configured executor is preserved, so the shadow log can say what it
    # would have used rather than merely that something was suppressed.
    assert executor.wrapped.name == "webhook"


def test_build_executor_defaults_to_simulated(settings) -> None:
    assert build_executor(settings).name == "simulated"


# ── Webhook executor ───────────────────────────────────────────────


def test_webhook_without_a_url_reports_an_error_not_a_crash() -> None:
    result = WebhookExecutor("").execute(make_intent(), make_event())
    assert result.status is ActionStatus.error
    assert "not configured" in result.details


def test_webhook_posts_a_signed_payload(monkeypatch) -> None:
    """The host must be able to prove the intent came from us."""
    import httpx

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "executed", "details": "sent by host", "recovered_amount": 120.0}

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, content=content, headers=headers)
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    executor = WebhookExecutor("https://host.example/actions", secret="s3cret")
    result = executor.execute(make_intent(), make_event())

    assert result.status is ActionStatus.executed
    assert result.recovered_amount == 120.0
    assert verify_signature("s3cret", captured["content"], captured["headers"]["X-RecoveryAI-Signature"])

    body = json.loads(captured["content"])
    assert body["intent"]["final_action"] == SEND_NUDGE
    assert "event" in body


def test_webhook_accepting_without_an_outcome_is_pending_not_success(monkeypatch) -> None:
    """Never invent a recovery. An accepted intent is not a collected payment."""
    import httpx

    class FakeResponse:
        status_code = 202

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())

    result = WebhookExecutor("https://host.example/actions").execute(make_intent(), make_event())

    assert result.status is ActionStatus.pending_host_execution
    assert result.recovered_amount == 0.0


def test_webhook_transport_failure_degrades_to_error(monkeypatch) -> None:
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", boom)

    result = WebhookExecutor("https://host.example/actions").execute(make_intent(), make_event())
    assert result.status is ActionStatus.error


def test_webhook_http_error_status_is_reported(monkeypatch) -> None:
    import httpx

    class FakeResponse:
        status_code = 503

        def json(self):
            return {}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())

    result = WebhookExecutor("https://host.example/actions").execute(make_intent(), make_event())
    assert result.status is ActionStatus.error
    assert "503" in result.details


# ── Signing ────────────────────────────────────────────────────────


def test_signature_roundtrip() -> None:
    body = b'{"a":1}'
    assert verify_signature("secret", body, sign_payload("secret", body))


def test_signature_rejects_tampering_and_wrong_keys() -> None:
    body = b'{"a":1}'
    sig = sign_payload("secret", body)
    assert not verify_signature("secret", b'{"a":2}', sig)
    assert not verify_signature("other", body, sig)
    assert not verify_signature("secret", body, None)
    assert not verify_signature("", body, sig)  # no secret never passes


def test_signature_accepts_a_bare_hex_digest() -> None:
    """Some senders omit the `sha256=` prefix; that should not be a rejection."""
    body = b'{"a":1}'
    bare = sign_payload("secret", body).removeprefix("sha256=")
    assert verify_signature("secret", body, bare)
