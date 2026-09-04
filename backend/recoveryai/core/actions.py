"""The action catalogue — what the agent is allowed to decide.

Ported from the previous build's `Action` subclasses. Their role has changed:
they are no longer the system's final word, they are `SimulatedExecutor`'s
internal detail. In a real deployment the host's own infrastructure carries out
the `ActionIntent` and these classes are never touched.

Action identifiers are snake_case and canonical: the same string is the tool
name the model sees, the key the guardrails match on, the value stored in
`case_steps.final_action`, and the payload field a webhook host reads. One
vocabulary end to end — the previous build's PascalCase/snake_case split was a
standing source of silent mismatches.
"""

from __future__ import annotations

import random
from typing import Any

from recoveryai.core.models import ActionStatus, RecoveryEvent

# ── Canonical action names ─────────────────────────────────────────

SEND_NUDGE = "send_nudge"
SEND_DISCOUNT = "send_discount"
RETRY_CHARGE = "retry_charge"
SEND_PAYMENT_UPDATE_LINK = "send_payment_update_link"
SEND_MANDATE_RESETUP_LINK = "send_mandate_resetup_link"
OFFER_PARTIAL_PAYMENT_PLAN = "offer_partial_payment_plan"
ESCALATE_TO_HUMAN = "escalate_to_human"
WAIT_AND_REASSESS = "wait_and_reassess"
CLOSE_AS_UNRECOVERABLE = "close_as_unrecoverable"


#: The first decision on a case dispatches; it does not yet know what happened.
#:
#: Nothing here can observe money arriving at the instant it acts. A reminder has
#: not been read yet, a payment link has not been opened, and a card
#: authorisation is not a settlement. Treating dispatch as a result was the
#: source of cases that opened and closed inside the same millisecond with a full
#: recovery booked against them — an outcome no payment system produces and no
#: reviewer can audit. Outcomes are therefore read at the next check-in, which is
#: also where a real integration hears back: `POST /actions/{id}/report`, or
#: Razorpay's `payment_link.paid`.
FIRST_OBSERVABLE_STEP = 2


def lands(probability: float, step_number: int = FIRST_OBSERVABLE_STEP) -> bool:
    """Has this attempt collected the money, as of this check-in?

    Factored out as a named seam so tests can force an outcome instead of
    seeding the global RNG and hoping. Nothing else in the module calls
    `random` directly.
    """
    if step_number < FIRST_OBSERVABLE_STEP:
        return False
    return probability > 0 and random.random() < probability


class Action:
    """Common interface. `simulate()` produces a plausible outcome, nothing more.

    An invoice is recovered or it is not — there is no world in which a nudge
    wins back 15% of a cart. So `recovery_rate` is the *probability* the attempt
    lands, and a landed attempt recovers the whole amount at risk.

    The previous reading — recovery_rate as a fraction of the amount, booked on
    every step regardless of outcome — is what let a case accrue more "recovered"
    revenue than it was ever worth: six nudges at 15% each reported 90% of an
    invoice recovered on a case that ended up escalated to a human having
    collected nothing. Rates are still illustrative constants rather than a
    model; a production integration reports real outcomes through
    `POST /api/v1/actions/{id}/report`.
    """

    name: str
    status: ActionStatus = ActionStatus.executed
    #: Probability this attempt lands. On landing, the full amount is recovered.
    recovery_rate: float = 0.0
    #: Fraction of the at-risk amount this action costs to run.
    cost_rate: float = 0.0
    #: Flat servicing cost, in currency units.
    flat_cost: float = 0.0
    #: How the trace should word a landed / not-landed outcome. Outreach waits on
    #: a customer; a charge attempt gets an answer from the bank there and then.
    outcome_words: tuple[str, str] = ("payment completed", "no response yet")

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        raise NotImplementedError

    def simulate(
        self,
        event: RecoveryEvent,
        params: dict[str, Any] | None = None,
        *,
        step_number: int = FIRST_OBSERVABLE_STEP,
        probability: float | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        landed = lands(self.recovery_rate if probability is None else probability, step_number)
        return {
            "action": self.name,
            # A landed attempt is definitively finished, whatever the action's
            # usual resting status is.
            "status": ActionStatus.executed if landed else self.status,
            "details": f"[SIMULATED] {self.describe(event, params)} — {self.outcome_words[not landed]}",
            "cost": round(event.amount * self.cost_rate + self.flat_cost, 2),
            "recovered_amount": round(event.amount, 2) if landed else 0.0,
        }


class SendNudgeAction(Action):
    """A plain 'finish your payment' reminder. Free, low yield, always safe."""

    name = SEND_NUDGE
    recovery_rate = 0.15

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        channel = params.get("channel", "email")
        subject = params.get("message_subject")
        suffix = f' — "{subject}"' if subject else ""
        return f"Nudge sent to {event.customer_id} via {channel}{suffix}"


class SendDiscountAction(Action):
    """A discount coupon. The expensive lever, hence the 1-per-90-days guardrail."""

    name = SEND_DISCOUNT
    recovery_rate = 0.25

    def simulate(
        self,
        event: RecoveryEvent,
        params: dict[str, Any] | None = None,
        *,
        step_number: int = FIRST_OBSERVABLE_STEP,
        probability: float | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        pct = float(params.get("discount_percent", 10))
        pct = min(25.0, max(1.0, pct))  # the model does not get to invent an 80% coupon
        landed = lands(self.recovery_rate if probability is None else probability, step_number)
        return {
            "action": self.name,
            "status": ActionStatus.executed,
            "details": (
                f"[SIMULATED] {pct:.0f}% discount coupon sent to {event.customer_id}"
                f" — {'redeemed' if landed else 'not redeemed'}"
            ),
            # A coupon nobody redeems costs nothing. Booking the discount as
            # spend on every send would overstate cost of recovery exactly as
            # badly as the old code overstated revenue.
            "cost": round(event.amount * pct / 100.0, 2) if landed else 0.0,
            "recovered_amount": round(event.amount, 2) if landed else 0.0,
        }

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        return f"Discount coupon sent to {event.customer_id}"


class RetryChargeAction(Action):
    """Re-present the mandate, timed for a window when funds are likelier present.

    The best odds in the palette, because it is the only action that does not
    depend on the customer noticing anything.
    """

    name = RETRY_CHARGE
    status = ActionStatus.scheduled
    recovery_rate = 0.45
    outcome_words = ("charge succeeded", "charge declined again")

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        window = params.get("retry_window", "next_salary_date")
        return f"Charge retry scheduled for {window} on {event.customer_id}"


class SendPaymentUpdateLinkAction(Action):
    """Ask the customer to re-enter an instrument. For expired cards, not empty accounts."""

    name = SEND_PAYMENT_UPDATE_LINK
    recovery_rate = 0.18

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        return f"Payment-instrument update link sent to {event.customer_id}"


class SendMandateResetupLinkAction(Action):
    """Rebuild a revoked or lapsed mandate — a retry cannot fix this."""

    name = SEND_MANDATE_RESETUP_LINK
    recovery_rate = 0.20

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        return f"Mandate re-setup link sent to {event.customer_id}"


class OfferPartialPaymentPlanAction(Action):
    """Split the invoice. Costs servicing effort, not the deferred principal."""

    name = OFFER_PARTIAL_PAYMENT_PLAN
    recovery_rate = 0.35
    # The previous build charged 50% of the invoice as `cost`, treating deferred
    # revenue as money spent — which made the whole B2B vertical read as
    # loss-making on the dashboard. Deferring an invoice is not a cost; the real
    # cost is the ops handling, so that is what is recorded.
    flat_cost = 25.0

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        split = params.get("split", "50% now, 50% in 30 days")
        return f"Partial payment plan ({split}) offered to {event.customer_id}"


class EscalateToHumanAction(Action):
    """Hand off. Always permitted — never guardrail-blocked, by design."""

    name = ESCALATE_TO_HUMAN
    status = ActionStatus.escalated

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        reason = params.get("escalation_reason", "agent requested human review")
        return f"Escalated to human queue for {event.customer_id}: {reason}"


class WaitAndReassessAction(Action):
    """Deliberately do nothing yet.

    A real recovery workflow's most common correct move is patience — chasing a
    customer twice in an hour destroys more value than it recovers. Making this
    an explicit choice means "no action" shows up in the trace as a decision the
    agent made, rather than as the absence of one.
    """

    name = WAIT_AND_REASSESS
    status = ActionStatus.scheduled

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        return f"Holding {event.customer_id} for re-evaluation; no outreach this step"


class CloseAsUnrecoverableAction(Action):
    """Stop spending on this case. Cheap only in the sense that quitting is cheap."""

    name = CLOSE_AS_UNRECOVERABLE

    def describe(self, event: RecoveryEvent, params: dict[str, Any]) -> str:
        reason = params.get("close_reason", "recovery cost exceeds expected value")
        return f"Case closed as unrecoverable for {event.customer_id}: {reason}"


ACTION_REGISTRY: dict[str, Action] = {
    action.name: action
    for action in (
        SendNudgeAction(),
        SendDiscountAction(),
        RetryChargeAction(),
        SendPaymentUpdateLinkAction(),
        SendMandateResetupLinkAction(),
        OfferPartialPaymentPlanAction(),
        EscalateToHumanAction(),
        WaitAndReassessAction(),
        CloseAsUnrecoverableAction(),
    )
}

ALL_ACTIONS: tuple[str, ...] = tuple(ACTION_REGISTRY)


def get_action(name: str) -> Action | None:
    return ACTION_REGISTRY.get(name)


def is_known_action(name: str) -> bool:
    return name in ACTION_REGISTRY
