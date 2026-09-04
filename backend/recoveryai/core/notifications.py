"""Telling a human that a case now needs them.

An escalation that lands silently in a queue is only nominally an escalation.
The whole point of the guardrails handing a case to a person is that a person
finds out, and "it is visible in the console if you happen to look" is not that.

Same shape as `execution.py`: a protocol, a no-op default, and a real
implementation, injected rather than imported. Core does not depend on Slack —
it depends on "something that can tell a human", which is a different and much
weaker commitment.

Notifiers must never raise. A case that escalated correctly and then failed to
send a webhook has still escalated correctly; losing the transition because the
announcement failed would be strictly worse than a missed message.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from recoveryai.core.models import ActionIntent

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """Announce that a case has been handed to a human."""

    name: str

    def case_escalated(self, *, case: Any, intent: ActionIntent, reason: str) -> bool:
        """Returns whether anything was actually dispatched. Must not raise."""
        ...


class NullNotifier:
    """Default. Records the escalation in the log and sends nothing.

    This is what runs when no webhook is configured, which must stay the
    out-of-the-box behaviour: a system that starts posting to a chat channel
    because someone cloned the repo would be a nasty surprise.
    """

    name = "none"

    def case_escalated(self, *, case: Any, intent: ActionIntent, reason: str) -> bool:
        logger.info(
            "case escalated (no notifier configured)",
            extra={"case_id": intent.case_id, "reason": reason},
        )
        return False


def _severity(amount: float) -> str:
    if amount >= 25_000:
        return "high"
    if amount >= 5_000:
        return "medium"
    return "low"


class SlackNotifier:
    """Posts an escalation summary to a Slack incoming webhook.

    The message leads with what a reviewer has to decide, not with what the
    system did. Amount and vertical are the two fields that determine whether
    someone picks it up now or after lunch, so they go first; the agent's own
    reasoning follows for the person who opens it.
    """

    name = "slack"

    def __init__(self, webhook_url: str, timeout_seconds: float = 5.0) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def _blocks(self, *, case: Any, intent: ActionIntent, reason: str) -> dict[str, Any]:
        amount = f"{intent.currency} {intent.amount:,.2f}"
        severity = _severity(intent.amount)
        diagnosis = getattr(case, "diagnosis", "unknown")
        redirected = (
            f"\n*Proposed:* `{intent.proposed_action}` → *blocked:* {intent.guardrail_reason}"
            if intent.was_redirected
            else ""
        )
        return {
            "text": f"Case needs review: {amount} ({intent.vertical.value})",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Review needed · {amount}"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Vertical*\n{intent.vertical.value}"},
                        {"type": "mrkdwn", "text": f"*Diagnosis*\n{diagnosis}"},
                        {"type": "mrkdwn", "text": f"*Severity*\n{severity}"},
                        {"type": "mrkdwn", "text": f"*Confidence*\n{intent.confidence:.2f}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Why it escalated:* {reason}{redirected}\n\n_{intent.reasoning}_",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"`{intent.case_id}` · step {intent.step_number}"}
                    ],
                },
            ],
        }

    def case_escalated(self, *, case: Any, intent: ActionIntent, reason: str) -> bool:
        if not self.webhook_url:
            return False

        payload = json.dumps(self._blocks(case=case, intent=intent, reason=reason)).encode("utf-8")
        try:
            import httpx  # noqa: PLC0415 — lazy so core imports without httpx

            response = httpx.post(
                self.webhook_url,
                content=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "slack notification failed",
                extra={"case_id": intent.case_id, "error": type(exc).__name__},
            )
            return False

        if response.status_code >= 400:
            logger.warning(
                "slack rejected the notification",
                extra={"case_id": intent.case_id, "status": response.status_code},
            )
            return False

        logger.info("escalation announced to slack", extra={"case_id": intent.case_id})
        return True


def build_notifier(settings: Any) -> Notifier:
    """The notifier described by settings. Silent unless a webhook is configured."""
    url = getattr(settings, "slack_webhook_url", "") or ""
    if url.strip():
        return SlackNotifier(url.strip(), timeout_seconds=settings.slack_timeout_seconds)
    return NullNotifier()
