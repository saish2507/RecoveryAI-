"""Escalation notifications: silent by default, and never able to undo a handoff."""

from __future__ import annotations

import json

import pytest
from conftest import make_event
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.llm.base import ToolCall
from recoveryai.core.models import ActionIntent, CaseStatus, Vertical
from recoveryai.core.notifications import NullNotifier, SlackNotifier, build_notifier
from recoveryai.core.verticals import get_vertical


class RecordingNotifier:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def case_escalated(self, *, case, intent, reason) -> bool:
        self.calls.append({"case_id": intent.case_id, "reason": reason, "amount": intent.amount})
        return True


class ExplodingNotifier:
    name = "exploding"

    def case_escalated(self, *, case, intent, reason) -> bool:
        raise RuntimeError("chat provider is down")


def any_decision(governed_llm):
    """A model that will answer, for tests that need *a* decision but not a specific one.

    Every case is decided by the model and nothing decides in its absence, so a
    test that wants a step to exist has to supply an answer.
    """
    return governed_llm([ToolCall("send_nudge", {}, "chase it", 0.7)] * 8)


def make_intent(action: str = "escalate_to_human", **kwargs) -> ActionIntent:
    return ActionIntent(
        case_id="case_x",
        step_number=1,
        vertical=Vertical.b2b,
        customer_id="cust_1",
        amount=28_400.0,
        proposed_action=action,
        final_action=action,
        **kwargs,
    )


# ── Defaults ───────────────────────────────────────────────────────


def test_nothing_is_sent_when_no_webhook_is_configured(settings) -> None:
    """A fresh checkout must not start posting into somebody's chat workspace."""
    assert isinstance(build_notifier(settings), NullNotifier)
    assert build_notifier(settings).case_escalated(case=None, intent=make_intent(), reason="x") is False


def test_a_configured_webhook_selects_slack(settings) -> None:
    settings.slack_webhook_url = "https://hooks.slack.example/T/B/xxx"
    assert isinstance(build_notifier(settings), SlackNotifier)


def test_a_blank_webhook_url_is_not_a_webhook(settings) -> None:
    settings.slack_webhook_url = "   "
    assert isinstance(build_notifier(settings), NullNotifier)


# ── Wiring ─────────────────────────────────────────────────────────


def test_an_escalation_notifies(session, settings, governed_llm) -> None:
    notifier = RecordingNotifier()
    agent = RecoveryAgent(settings=settings, llm=any_decision(governed_llm), notifier=notifier)

    case = agent.handle_event(session, make_event("b2b", 28_400, dispute_flag=True))[0]

    assert case.status == CaseStatus.escalated.value
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["amount"] == 28_400


def test_a_resolved_case_notifies_nobody(session, settings, governed_llm, outcome) -> None:
    outcome(True)
    notifier = RecordingNotifier()
    agent = RecoveryAgent(settings=settings, llm=any_decision(governed_llm), notifier=notifier)

    agent.handle_event(session, make_event("cart", 500, payment_gateway_error_code="card_declined"))

    assert notifier.calls == []


def test_the_step_limit_handoff_also_notifies(session, settings, governed_llm) -> None:
    """Running out of steps is a handoff too — and the easiest one to miss."""
    notifier = RecordingNotifier()
    agent = RecoveryAgent(settings=settings, llm=any_decision(governed_llm), notifier=notifier)

    case = agent.handle_event(session, make_event("cart", 500, payment_gateway_error_code="card_declined"))[0]
    for _ in range(get_vertical("cart").max_steps):
        agent.advance_case(session, case)

    assert case.status == CaseStatus.escalated.value
    assert "step limit" in notifier.calls[0]["reason"]


def test_a_broken_notifier_cannot_undo_the_escalation(session, settings, governed_llm) -> None:
    """The case escalated correctly; losing that because chat is down is worse."""
    agent = RecoveryAgent(settings=settings, llm=any_decision(governed_llm), notifier=ExplodingNotifier())

    case = agent.handle_event(session, make_event("b2b", 9_000, dispute_flag=True))[0]

    assert case.status == CaseStatus.escalated.value


# ── Payload ────────────────────────────────────────────────────────


def test_slack_payload_leads_with_what_a_reviewer_decides_on() -> None:
    notifier = SlackNotifier("https://hooks.slack.example/T/B/xxx")
    blocks = notifier._blocks(case=None, intent=make_intent(), reason="dispute flagged")

    assert "28,400" in blocks["text"]
    rendered = json.dumps(blocks)
    assert "dispute flagged" in rendered
    assert "case_x" in rendered


def test_a_redirected_proposal_is_named_in_the_alert() -> None:
    """A reviewer needs to know the agent wanted something the guardrail refused."""
    notifier = SlackNotifier("https://hooks.slack.example/T/B/xxx")
    intent = make_intent(
        "escalate_to_human", guardrail_reason="guardrail: max_3_touches_escalation"
    )
    intent = intent.model_copy(update={"proposed_action": "send_nudge"})

    rendered = json.dumps(notifier._blocks(case=None, intent=intent, reason="capacity spent"))
    assert "send_nudge" in rendered
    assert "max_3_touches" in rendered


def test_a_transport_failure_reports_false_rather_than_raising(monkeypatch) -> None:
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", boom)
    notifier = SlackNotifier("https://hooks.slack.example/T/B/xxx")
    assert notifier.case_escalated(case=None, intent=make_intent(), reason="x") is False


def test_a_rejected_post_reports_false(monkeypatch) -> None:
    import httpx

    class Response:
        status_code = 403

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response())
    notifier = SlackNotifier("https://hooks.slack.example/T/B/xxx")
    assert notifier.case_escalated(case=None, intent=make_intent(), reason="x") is False


def test_a_successful_post_reports_true(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, content=content)
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    notifier = SlackNotifier("https://hooks.slack.example/T/B/xxx")

    assert notifier.case_escalated(case=None, intent=make_intent(), reason="x") is True
    assert captured["url"] == "https://hooks.slack.example/T/B/xxx"
    assert json.loads(captured["content"])["blocks"]


@pytest.mark.parametrize(
    ("amount", "expected"), [(200.0, "low"), (9_000.0, "medium"), (60_000.0, "high")]
)
def test_severity_tracks_the_money(amount: float, expected: str) -> None:
    from recoveryai.core.notifications import _severity

    assert _severity(amount) == expected
