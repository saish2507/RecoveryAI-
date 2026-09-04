"""Tool (function-call) declarations — the agent's action space, as the model sees it.

Every tool schema requires `reasoning` and `confidence`. That is deliberate: it
means the model's rationale and its own uncertainty arrive inside the *same*
function call as the decision, so there is no second turn, no free-text parsing,
and no case where an action is recorded without an explanation attached.

`confidence` is not decoration — it feeds the human-review priority score, so a
large, uncertain decision surfaces above a small, confident one.
"""

from __future__ import annotations

from typing import Any

from recoveryai.core.actions import (
    CLOSE_AS_UNRECOVERABLE,
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    SEND_MANDATE_RESETUP_LINK,
    SEND_NUDGE,
    SEND_PAYMENT_UPDATE_LINK,
    WAIT_AND_REASSESS,
)
from recoveryai.core.llm.base import ToolSpec

_REASONING_PARAM = {
    "type": "string",
    "description": (
        "One or two sentences explaining why this action fits this specific case. "
        "Reference the concrete signals you used. This is shown to a human reviewer."
    ),
}

_CONFIDENCE_PARAM = {
    "type": "number",
    "description": (
        "Your confidence in this choice, 0.0–1.0. Be honest: low confidence on a "
        "high-value case routes it to a human, which is the desired outcome when "
        "the signals are genuinely unclear."
    ),
}


#: Drafting parameters, added only to the actions that actually contact someone.
#: They ride on the decision call rather than a follow-up "now write it" turn:
#: the model has the case context loaded at exactly the moment it commits to an
#: action, so the copy and the reasoning that justified it cannot drift apart.
_MESSAGE_SUBJECT_PARAM = {
    "type": "string",
    "description": (
        "Subject line for the message this action sends, under 90 characters. "
        "Specific to this customer's situation — not a generic 'Payment failed'."
    ),
}

_MESSAGE_BODY_PARAM = {
    "type": "string",
    "description": (
        "The message body the customer will read, under 600 characters. Plain, warm, "
        "and honest about what happened. State the real reason the payment failed and "
        "the one thing they need to do. Never invent refunds, deadlines, penalties, "
        "account consequences, or any offer beyond this action. A human reviews this "
        "before it is sent."
    ),
}


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {
        "reasoning": _REASONING_PARAM,
        "confidence": _CONFIDENCE_PARAM,
        **(properties or {}),
    }
    return {
        "type": "object",
        "properties": props,
        "required": ["reasoning", "confidence", *(required or [])],
    }


def _outreach_schema(
    properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    """`_schema` plus the drafting fields, for actions that message a customer.

    The message fields are *not* marked required. A model that omits them still
    produces a usable decision, and `drafts.attach_draft` fills the gap from a
    template — failing the whole call over a missing subject line would trade a
    good decision for no decision.
    """
    return _schema(
        {
            "message_subject": _MESSAGE_SUBJECT_PARAM,
            "message_body": _MESSAGE_BODY_PARAM,
            **(properties or {}),
        },
        required=required,
    )


#: Full catalogue. Verticals select from it; the agent prunes it further per case.
TOOL_CATALOGUE: dict[str, ToolSpec] = {
    SEND_NUDGE: ToolSpec(
        name=SEND_NUDGE,
        description=(
            "Send a free, low-pressure reminder to complete the payment. The default "
            "safe choice: no cost, no discount leakage, modest recovery rate. Prefer "
            "this when the customer probably just got distracted or forgot."
        ),
        parameters=_outreach_schema(
            {
                "channel": {
                    "type": "string",
                    "enum": ["email", "sms", "whatsapp", "push"],
                    "description": "Delivery channel for the reminder.",
                }
            }
        ),
    ),
    SEND_DISCOUNT: ToolSpec(
        name=SEND_DISCOUNT,
        description=(
            "Offer a discount coupon. This costs real margin and is hard-capped at one "
            "per customer per 90 days, so spend it only when price is the actual "
            "objection — not as a generic retry. Never appropriate for a technical "
            "payment failure, where price was never the problem."
        ),
        parameters=_outreach_schema(
            {
                "discount_percent": {
                    "type": "number",
                    "description": "Discount percentage, 1–25. Keep it the smallest amount likely to work.",
                }
            },
            required=["discount_percent"],
        ),
    ),
    RETRY_CHARGE: ToolSpec(
        name=RETRY_CHARGE,
        description=(
            "Re-present the existing mandate at a chosen time. Correct for a temporary "
            "balance shortfall. Useless — and capped at 3 attempts — when the underlying "
            "instrument or mandate is broken, since retrying cannot fix either."
        ),
        parameters=_schema(
            {
                "retry_window": {
                    "type": "string",
                    "enum": ["immediate", "next_salary_date", "in_3_days", "in_7_days"],
                    "description": (
                        "When to re-present. Timing it near payday materially raises success odds."
                    ),
                }
            },
            required=["retry_window"],
        ),
    ),
    SEND_PAYMENT_UPDATE_LINK: ToolSpec(
        name=SEND_PAYMENT_UPDATE_LINK,
        description=(
            "Ask the customer to supply a new payment instrument. Correct when the card "
            "is expired or otherwise unusable. Pointless when the instrument is fine and "
            "the account was merely empty."
        ),
        parameters=_outreach_schema(),
    ),
    SEND_MANDATE_RESETUP_LINK: ToolSpec(
        name=SEND_MANDATE_RESETUP_LINK,
        description=(
            "Ask the customer to re-authorise the recurring mandate. The only action that "
            "fixes a revoked, cancelled or lapsed mandate — retrying against a dead mandate "
            "will fail every time."
        ),
        parameters=_outreach_schema(),
    ),
    OFFER_PARTIAL_PAYMENT_PLAN: ToolSpec(
        name=OFFER_PARTIAL_PAYMENT_PLAN,
        description=(
            "Offer to split the invoice into instalments. The right move when the customer "
            "wants to pay but genuinely cannot right now — recovering most of a large invoice "
            "slowly beats chasing all of it and getting nothing."
        ),
        parameters=_outreach_schema(
            {
                "split": {
                    "type": "string",
                    "description": "Proposed structure, e.g. '50% now, 50% in 30 days'.",
                }
            },
            required=["split"],
        ),
    ),
    ESCALATE_TO_HUMAN: ToolSpec(
        name=ESCALATE_TO_HUMAN,
        description=(
            "Hand the case to a human. Always available and never blocked. Use it for "
            "disputes, legal or relationship-sensitive situations, high-value cases where "
            "you are genuinely unsure, and any case where the automated levers are spent."
        ),
        parameters=_schema(
            {
                "escalation_reason": {
                    "type": "string",
                    "description": "What specifically a human needs to decide or verify.",
                }
            },
            required=["escalation_reason"],
        ),
    ),
    WAIT_AND_REASSESS: ToolSpec(
        name=WAIT_AND_REASSESS,
        description=(
            "Deliberately take no outreach action this step and re-evaluate later. A real "
            "choice, not a no-op: contacting a customer twice in quick succession destroys "
            "goodwill. Use it when the previous touch has not had time to land."
        ),
        parameters=_schema(),
    ),
    CLOSE_AS_UNRECOVERABLE: ToolSpec(
        name=CLOSE_AS_UNRECOVERABLE,
        description=(
            "Stop working the case. Appropriate for small amounts where further effort "
            "costs more than the expected recovery. Prefer escalation over this for anything "
            "of material value."
        ),
        parameters=_schema(
            {
                "close_reason": {
                    "type": "string",
                    "description": "Why further recovery effort is not worth its cost.",
                }
            },
            required=["close_reason"],
        ),
    ),
}


def build_tools(action_names: list[str] | tuple[str, ...]) -> list[ToolSpec]:
    """ToolSpecs for the given actions, preserving order and skipping unknowns."""
    return [TOOL_CATALOGUE[name] for name in action_names if name in TOOL_CATALOGUE]
