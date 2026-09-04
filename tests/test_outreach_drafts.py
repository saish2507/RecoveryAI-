"""Drafted customer copy: where it comes from, and what it must never carry over.

The reviewable-before-sending claim rests on two properties — a draft always
exists, and it always describes the action that will actually run. The second is
the one with teeth: a guardrail redirect changes the action after the model has
already written copy for a different one.
"""

from __future__ import annotations

from conftest import make_event
from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    SEND_NUDGE,
    WAIT_AND_REASSESS,
)
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.cases import CaseStore
from recoveryai.core.drafts import (
    BODY_KEY,
    MAX_BODY_CHARS,
    MAX_SUBJECT_CHARS,
    SUBJECT_KEY,
    attach_draft,
    draft_from_params,
)
from recoveryai.core.llm.base import ToolCall
from recoveryai.core.tools import TOOL_CATALOGUE


def test_every_outreach_tool_offers_the_drafting_fields() -> None:
    for action in (SEND_NUDGE, SEND_DISCOUNT, OFFER_PARTIAL_PAYMENT_PLAN):
        props = TOOL_CATALOGUE[action].parameters["properties"]
        assert "message_subject" in props
        assert "message_body" in props


def test_internal_actions_do_not_offer_drafting_fields() -> None:
    """Copy for an action that contacts nobody would be words never sent."""
    for action in (ESCALATE_TO_HUMAN, WAIT_AND_REASSESS, RETRY_CHARGE):
        assert "message_body" not in TOOL_CATALOGUE[action].parameters["properties"]


def test_a_rule_decided_action_still_gets_copy() -> None:
    """The deterministic path is also the LLM-outage path — it cannot go out blank."""
    params = attach_draft(SEND_NUDGE, make_event("cart", 500), {})
    assert params[SUBJECT_KEY]
    assert "500" in params[BODY_KEY]


def test_the_models_own_wording_wins_when_it_supplied_any() -> None:
    params = attach_draft(
        SEND_NUDGE,
        make_event("cart", 500),
        {SUBJECT_KEY: "Your cart is still waiting", BODY_KEY: "Two taps and it is done."},
    )
    assert params[SUBJECT_KEY] == "Your cart is still waiting"
    assert params[BODY_KEY] == "Two taps and it is done."


def test_a_blank_field_falls_back_without_discarding_the_good_one() -> None:
    params = attach_draft(
        SEND_NUDGE, make_event("cart", 500), {SUBJECT_KEY: "Still waiting", BODY_KEY: "   "}
    )
    assert params[SUBJECT_KEY] == "Still waiting"
    assert params[BODY_KEY]  # filled from the template


def test_overlong_model_output_is_truncated() -> None:
    params = attach_draft(
        SEND_NUDGE, make_event("cart", 500), {SUBJECT_KEY: "x" * 400, BODY_KEY: "y" * 5_000}
    )
    assert len(params[SUBJECT_KEY]) <= MAX_SUBJECT_CHARS
    assert len(params[BODY_KEY]) <= MAX_BODY_CHARS


def test_internal_actions_get_no_draft() -> None:
    assert draft_from_params(attach_draft(ESCALATE_TO_HUMAN, make_event("b2b", 9_000), {})) is None


def test_a_blocked_discounts_copy_never_rides_along_to_the_nudge(session, settings, governed_llm):
    """The failure this guards: a customer promised 20% off by a redirected nudge.

    The model writes coupon copy, the 90-day guardrail refuses the coupon, and
    the case falls back to a plain nudge. If the drafted body survived that
    redirect, the nudge would carry an offer the guardrail just refused.
    """
    llm = governed_llm(
        [
            ToolCall(
                SEND_DISCOUNT,
                {
                    "discount_percent": 20,
                    SUBJECT_KEY: "20% off, just for you",
                    BODY_KEY: "Here is 20% off to finish your order.",
                },
                "price resistance",
                0.8,
            )
        ]
    )
    agent = RecoveryAgent(settings=settings, llm=llm)

    # An unrecognised gateway code is ambiguous enough to earn a model call, and
    # still diagnoses as a technical failure — which the cart guardrail refuses
    # to discount. Both halves are needed: a confident diagnosis would skip the
    # model entirely and there would be no model-written copy to redirect.
    case, decision, _ = agent.handle_event(
        session, make_event("cart", 4_000, payment_gateway_error_code="ODD_CODE_77")
    )

    assert decision.intent.proposed_action == SEND_DISCOUNT
    assert decision.intent.final_action != SEND_DISCOUNT
    body = decision.intent.params[BODY_KEY]
    assert "20%" not in body
    assert "discount" not in body.lower()

    step = CaseStore(session).get(case.id).steps[0]
    assert "20%" not in step.action_params[BODY_KEY]
