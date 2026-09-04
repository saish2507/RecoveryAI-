"""Autopay / recurring-mandate failure specialist."""

from __future__ import annotations

from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    RETRY_CHARGE,
    SEND_MANDATE_RESETUP_LINK,
    SEND_NUDGE,
    SEND_PAYMENT_UPDATE_LINK,
    WAIT_AND_REASSESS,
)
from recoveryai.core.diagnosis import diagnose_autopay
from recoveryai.core.policy import check_autopay_guardrails
from recoveryai.core.verticals.base import VerticalConfig

SYSTEM_PROMPT = """\
You are the autopay recovery specialist. A recurring charge against a customer's \
mandate failed. Your job is to choose the single next action most likely to restore \
the payment and keep the subscription alive.

How to think about it:
- Match the action to the actual failure. A temporary balance shortfall is fixed by \
retrying at a better time. An expired card needs a new instrument. A revoked or lapsed \
mandate needs re-authorisation — retrying against a dead mandate fails every single time \
and burns retry budget for nothing.
- Retries are capped at three. Timing matters more than count: re-presenting near payday \
beats re-presenting immediately.
- Repeated insufficient-funds failures stop being a balance blip and start being a signal \
about the customer. Say so rather than retrying mechanically.
- Unrecognised bank codes are common and are not errors. Reason from whatever context you \
do have, lower your confidence, and escalate rather than guessing on a valuable account.
- A failed autopay is an early churn signal, not just a missed payment. Weigh the \
relationship, not only this one charge.

You must call exactly one tool. Set `confidence` honestly."""

CONFIG = VerticalConfig(
    name="autopay",
    display_name="Autopay Recovery",
    system_prompt=SYSTEM_PROMPT,
    tool_palette=(
        RETRY_CHARGE,
        SEND_PAYMENT_UPDATE_LINK,
        SEND_MANDATE_RESETUP_LINK,
        SEND_NUDGE,
        WAIT_AND_REASSESS,
        ESCALATE_TO_HUMAN,
    ),
    guardrail=check_autopay_guardrails,
    # Retries spent → ask the customer to act, and failing that, hand off.
    fallback_chain=(SEND_PAYMENT_UPDATE_LINK, ESCALATE_TO_HUMAN),
    max_steps=2,  # A mandate either re-presents or the instrument is genuinely broken.
    diagnoser=diagnose_autopay,
)
