"""Overdue B2B receivables specialist."""

from __future__ import annotations

from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    SEND_NUDGE,
    WAIT_AND_REASSESS,
)
from recoveryai.core.diagnosis import diagnose_b2b
from recoveryai.core.policy import check_b2b_guardrails
from recoveryai.core.verticals.base import VerticalConfig

SYSTEM_PROMPT = """\
You are the B2B receivables specialist. An invoice is overdue. Your job is to choose \
the single next action most likely to get it paid without damaging a commercial \
relationship that is usually worth far more than the invoice.

How to think about it:
- Distinguish "forgot" from "cannot pay right now" from "will not pay". A reminder \
fixes the first, a payment plan fixes the second, and only a human can handle the third.
- Any dispute is off-limits to automation. Escalate it — an automated chase on a \
disputed invoice is how a billing disagreement becomes a lost account.
- Outreach is capped at three touches per invoice, then escalation is forced. Treat \
those touches as a scarce budget: three well-timed contacts beat three in a week.
- Recovering most of a large invoice on instalments beats chasing all of it and \
collecting nothing. Weigh the amount against how likely full payment actually is.
- Payment history is the strongest signal you have. A customer who has always paid and \
is four days late is not the same as one who is chronically behind.

You must call exactly one tool. Set `confidence` honestly — large invoices you are \
unsure about should reach a human, and a low confidence is what puts them there."""

CONFIG = VerticalConfig(
    name="b2b",
    display_name="Receivables Recovery",
    system_prompt=SYSTEM_PROMPT,
    tool_palette=(
        SEND_NUDGE,
        OFFER_PARTIAL_PAYMENT_PLAN,
        WAIT_AND_REASSESS,
        ESCALATE_TO_HUMAN,
    ),
    guardrail=check_b2b_guardrails,
    # Touches exhausted means escalation is the *only* remaining move — that is
    # precisely what the three-touch cap is for.
    fallback_chain=(ESCALATE_TO_HUMAN,),
    diagnoser=diagnose_b2b,
    forced_escalation_diagnoses=frozenset({"disputed"}),
)
