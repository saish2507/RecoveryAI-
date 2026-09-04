"""Customer-facing copy for the actions that actually contact someone.

An action name is not a message. `send_nudge` tells a reviewer what the agent
decided; it tells them nothing about what the customer will read, which is the
part that can embarrass a brand, breach a tone guideline, or promise something
the business did not agree to. This module is where "what we decided" becomes
"what we will actually say", so that a human can judge the second thing.

Two sources, one shape:

* **The model**, when it was consulted — the drafting parameters live on the same
  tool schema as the decision, so the copy arrives in the *same* function call.
  A separate "now write the email" round trip would double latency and cost on
  every outreach action, and could drift from the reasoning that justified it.
* **A template**, otherwise. Rule-decided cases, guardrail redirects and every
  LLM failure path still need a message, and a case that reaches a customer with
  an empty body is worse than one with a plain but correct one.

Nothing here sends anything. Drafts are recorded on the step's `action_params`,
which is what makes them reviewable before an executor is ever pointed at a real
channel.
"""

from __future__ import annotations

from typing import Any

from recoveryai.core.actions import (
    OFFER_PARTIAL_PAYMENT_PLAN,
    SEND_DISCOUNT,
    SEND_MANDATE_RESETUP_LINK,
    SEND_NUDGE,
    SEND_PAYMENT_UPDATE_LINK,
)
from recoveryai.core.models import RecoveryEvent

#: Actions that put words in front of a customer. Everything else — a retry, an
#: escalation, a deliberate wait — is internal and gets no draft, because
#: inventing customer copy for an action that contacts nobody would put text in
#: the audit trail that was never going to be sent.
OUTREACH_ACTIONS = frozenset(
    {
        SEND_NUDGE,
        SEND_DISCOUNT,
        SEND_PAYMENT_UPDATE_LINK,
        SEND_MANDATE_RESETUP_LINK,
        OFFER_PARTIAL_PAYMENT_PLAN,
    }
)

SUBJECT_KEY = "message_subject"
BODY_KEY = "message_body"

#: Hard ceilings, applied to model output as well as templates. A model that
#: returns three paragraphs has not failed validation in any way a schema can
#: catch, but it has written something nobody will read on a phone.
MAX_SUBJECT_CHARS = 90
MAX_BODY_CHARS = 600


def _money(event: RecoveryEvent) -> str:
    return f"{event.currency} {event.amount:,.2f}"


def _template(action: str, event: RecoveryEvent, params: dict[str, Any]) -> tuple[str, str]:
    """Plain, correct copy for when no model wrote any.

    Deliberately unexciting. These run on the deterministic path, which is also
    the path the system falls back to when the model is unavailable — the moment
    you least want a surprise going out under your brand.
    """
    amount = _money(event)

    if action == SEND_DISCOUNT:
        pct = params.get("discount_percent", 10)
        try:
            pct = f"{float(pct):.0f}"
        except (TypeError, ValueError):
            pct = "10"
        return (
            f"A {pct}% discount on your pending order",
            f"Your payment of {amount} did not go through. "
            f"Here is {pct}% off to complete it — the discount is applied at checkout.",
        )

    if action == SEND_PAYMENT_UPDATE_LINK:
        return (
            "Update your payment method",
            f"We could not process {amount} because your saved card is no longer usable. "
            "Add a current card and we will take it from there.",
        )

    if action == SEND_MANDATE_RESETUP_LINK:
        return (
            "Re-authorise your automatic payments",
            f"Your automatic payment mandate is no longer active, so {amount} could not be "
            "collected. Re-authorising takes about a minute and keeps your plan running.",
        )

    if action == OFFER_PARTIAL_PAYMENT_PLAN:
        split = params.get("split") or "50% now, 50% in 30 days"
        return (
            "A payment plan for your outstanding invoice",
            f"Your invoice for {amount} is outstanding. If settling it in one go is "
            f"difficult right now, we can split it: {split}. Reply and we will set it up.",
        )

    return (
        "Your payment did not go through",
        f"A payment of {amount} was not completed. You can finish it whenever suits you — "
        "nothing has been lost from your order.",
    )


def _clean(value: Any, limit: int) -> str:
    """Collapse whitespace and enforce the ceiling. Never raises on odd input."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def attach_draft(
    action: str, event: RecoveryEvent, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return `params` with customer-facing copy guaranteed present.

    The model's own wording wins when it supplied any — it saw the case context
    and can be specific in a way a template cannot. A missing, blank or
    over-long field falls back per-field rather than wholesale, so a good subject
    line is not discarded because the body came back empty.
    """
    params = dict(params or {})
    if action not in OUTREACH_ACTIONS:
        return params

    fallback_subject, fallback_body = _template(action, event, params)

    subject = _clean(params.get(SUBJECT_KEY), MAX_SUBJECT_CHARS)
    body = _clean(params.get(BODY_KEY), MAX_BODY_CHARS)

    params[SUBJECT_KEY] = subject or _clean(fallback_subject, MAX_SUBJECT_CHARS)
    params[BODY_KEY] = body or _clean(fallback_body, MAX_BODY_CHARS)
    return params


def draft_from_params(params: dict[str, Any] | None) -> dict[str, str] | None:
    """The drafted message on a recorded step, or `None` if that step sent nothing."""
    params = params or {}
    subject = params.get(SUBJECT_KEY)
    body = params.get(BODY_KEY)
    if not subject and not body:
        return None
    return {"subject": str(subject or ""), "body": str(body or "")}
