"""Razorpay-backed executor — the seam pointed at a real payment provider.

`SimulatedExecutor` proves the agent decides well. This proves the decision is
*actionable*: the same `ActionIntent`, unchanged, becomes a real Razorpay Payment
Link with the model's own copy on it, delivered to a real inbox in test mode.

Two things it deliberately does not do:

**It never reports money it has not seen.** Creating a payment link is not a
payment. Every outreach action returns `pending_host_execution` with
`recovered_amount=0`, and the case resolves only when Razorpay's
`payment_link.paid` webhook calls `POST /api/v1/actions/{intent_id}/report`. The
`reference_id` on the link is the intent id precisely so that round trip can find
its way home. This is the same discipline the rest of the system now follows —
revenue is recorded when it arrives, not when it is hoped for.

**It refuses live keys unless told twice.** A key that is not `rzp_test_*` is
rejected unless `RAZORPAY_ALLOW_LIVE=true`. The failure mode this guards against
— a recovery agent looping over real customers with real payment links because a
`.env` was copied from production — is bad enough to be worth an explicit gate.

Mandate re-presentment (`retry_charge`) is not implemented against the real API.
It needs a stored subscription or token this prototype has no concept of, and
quietly substituting a payment link would put a decision in the trace that never
happened.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from recoveryai.core.actions import (
    CLOSE_AS_UNRECOVERABLE,
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    WAIT_AND_REASSESS,
)
from recoveryai.core.drafts import BODY_KEY, OUTREACH_ACTIONS, SUBJECT_KEY
from recoveryai.core.models import ActionIntent, ActionStatus, ExecutionResult, RecoveryEvent

logger = logging.getLogger(__name__)

API_BASE = "https://api.razorpay.com/v1"

#: Actions that resolve entirely inside this system. Sending a payment link for
#: an escalation would contact a customer about a decision to *stop* contacting
#: them automatically.
INTERNAL_ACTIONS = frozenset({ESCALATE_TO_HUMAN, WAIT_AND_REASSESS, CLOSE_AS_UNRECOVERABLE})


class RazorpayError(RuntimeError):
    pass


def _to_paise(amount: float) -> int:
    """Rupees to the smallest currency unit, which is what the API expects.

    Rounded, not truncated: `int(18.5 * 100)` is 1849 on binary floats, and
    silently under-charging by a paise per link is the kind of bug that surfaces
    in a reconciliation report six months later.
    """
    return int(round(amount * 100))


class RazorpayExecutor:
    """Turns an `ActionIntent` into a real Razorpay Payment Link."""

    name = "razorpay"

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        allow_live: bool = False,
        callback_url: str = "",
        timeout_seconds: float = 15.0,
        expire_after_hours: int = 72,
    ) -> None:
        key_id = (key_id or "").strip()
        if key_id and not key_id.startswith("rzp_test_") and not allow_live:
            raise RazorpayError(
                f"refusing to use non-test Razorpay key {key_id[:12]!r}: "
                "set RAZORPAY_ALLOW_LIVE=true to send real payment links to real customers"
            )
        self.key_id = key_id
        self.key_secret = (key_secret or "").strip()
        self.callback_url = callback_url
        self.timeout_seconds = timeout_seconds
        self.expire_after_hours = expire_after_hours

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    @property
    def test_mode(self) -> bool:
        return self.key_id.startswith("rzp_test_")

    def _auth_header(self) -> str:
        raw = f"{self.key_id}:{self.key_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    # ── Payload ────────────────────────────────────────────────

    def _amount_for(self, intent: ActionIntent) -> int:
        """What to actually ask for, after any discount the agent decided on."""
        amount = intent.amount
        if intent.final_action == SEND_DISCOUNT:
            try:
                pct = float(intent.params.get("discount_percent", 0) or 0)
            except (TypeError, ValueError):
                pct = 0.0
            pct = min(25.0, max(0.0, pct))  # same ceiling the simulator enforces
            amount = amount * (1 - pct / 100.0)
        return _to_paise(amount)

    def _payload(self, intent: ActionIntent, event: RecoveryEvent) -> dict[str, Any]:
        params = intent.params or {}
        # The model wrote this copy during the decision call; it goes on the link
        # the customer actually opens rather than being an internal artefact.
        description = str(params.get(BODY_KEY) or params.get(SUBJECT_KEY) or "Complete your payment")

        meta = event.vertical_metadata or {}
        body: dict[str, Any] = {
            "amount": self._amount_for(intent),
            "currency": intent.currency,
            "description": description[:2048],
            # The handle Razorpay's webhook quotes back to us, which is what lets
            # an async `payment_link.paid` resolve the right case.
            "reference_id": str(intent.intent_id)[:40],
            "customer": {"name": str(meta.get("customer_name") or intent.customer_id)},
            "notify": {"email": False, "sms": False},
            "reminder_enable": True,
            "notes": {
                "case_id": intent.case_id,
                "vertical": intent.vertical.value,
                "step": str(intent.step_number),
                "action": intent.final_action,
            },
        }

        email = meta.get("customer_email")
        contact = meta.get("customer_contact")
        if email:
            body["customer"]["email"] = str(email)
            body["notify"]["email"] = True
        if contact:
            body["customer"]["contact"] = str(contact)
            body["notify"]["sms"] = True

        if intent.final_action == OFFER_PARTIAL_PAYMENT_PLAN:
            # The whole point of the action is that they cannot pay it all now.
            body["accept_partial"] = True
            body["first_min_partial_amount"] = max(100, self._amount_for(intent) // 2)

        if self.callback_url:
            body["callback_url"] = self.callback_url
            body["callback_method"] = "get"

        return body

    # ── Execution ──────────────────────────────────────────────

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        if intent.final_action in INTERNAL_ACTIONS:
            return ExecutionResult(
                status=ActionStatus.escalated
                if intent.final_action == ESCALATE_TO_HUMAN
                else ActionStatus.scheduled,
                details=f"{intent.final_action} handled internally; no provider call made",
                executor=self.name,
            )

        if intent.final_action == RETRY_CHARGE:
            # Honest refusal beats a substituted action. See module docstring.
            return ExecutionResult(
                status=ActionStatus.error,
                details=(
                    "retry_charge needs a stored mandate/subscription token, which this "
                    "integration does not hold; no charge was attempted"
                ),
                executor=self.name,
            )

        if intent.final_action not in OUTREACH_ACTIONS:
            return ExecutionResult(
                status=ActionStatus.error,
                details=f"no Razorpay mapping for action {intent.final_action!r}",
                executor=self.name,
            )

        if not self.configured:
            return ExecutionResult(
                status=ActionStatus.error,
                details="RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not configured",
                executor=self.name,
            )

        payload = json.dumps(self._payload(intent, event)).encode("utf-8")
        try:
            import httpx  # noqa: PLC0415 — lazy so core imports without httpx

            response = httpx.post(
                f"{API_BASE}/payment_links",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self._auth_header(),
                },
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "razorpay request failed",
                extra={"intent_id": str(intent.intent_id), "error": type(exc).__name__},
            )
            return ExecutionResult(
                status=ActionStatus.error,
                details=f"razorpay request failed: {type(exc).__name__}",
                executor=self.name,
            )

        return self._interpret(response, intent)

    def _interpret(self, response: Any, intent: ActionIntent) -> ExecutionResult:
        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code >= 400:
            described = ""
            if isinstance(data, dict):
                described = str(data.get("error", {}).get("description", "") or "")
            logger.warning(
                "razorpay rejected the request",
                extra={"intent_id": str(intent.intent_id), "status": response.status_code},
            )
            return ExecutionResult(
                status=ActionStatus.error,
                details=f"razorpay returned HTTP {response.status_code}: {described or 'no detail'}",
                executor=self.name,
            )

        link_id = str(data.get("id", "") or "")
        short_url = str(data.get("short_url", "") or "")
        mode = "TEST" if self.test_mode else "LIVE"

        logger.info(
            "razorpay payment link created",
            extra={
                "intent_id": str(intent.intent_id),
                "payment_link_id": link_id,
                "case_id": intent.case_id,
            },
        )
        return ExecutionResult(
            # Created, not collected. The case resolves on the paid webhook.
            status=ActionStatus.pending_host_execution,
            details=f"[{mode}] Razorpay payment link {link_id} created: {short_url}",
            cost=0.0,
            recovered_amount=0.0,
            executor=self.name,
        )
