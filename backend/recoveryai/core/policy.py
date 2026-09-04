"""Hard guardrails — the part of the system the model cannot argue with.

Every action the agent proposes passes through a checker here before anything is
dispatched. The checkers are
pure functions of (capacity so far, proposed action): no I/O, no model, no
randomness, trivially testable, and identical in shadow and live mode.

Ported from the previous build's `PolicyEngine` with the three thresholds and
their semantics unchanged:
  * cart    — at most 1 discount per customer per 90 days
  * b2b     — at most 3 outreach touches per invoice, then escalation is forced
  * autopay — at most 3 retries, then stop and flag

This module used to carry decision *tables* as well — a lookup that stood in for
the model when it was unavailable. They are gone. A table that quietly decides
under the agent's name produces a trace nobody can trust, so an unavailable model
now means no decision is recorded and the case is retried. What remains here is
only the part that constrains a decision, never the part that makes one.
"""

from __future__ import annotations

from dataclasses import dataclass

from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    WAIT_AND_REASSESS,
)

#: Revision of the rules in this module, stamped onto every decision step.
#:
#: Bump it whenever a threshold, a scope, or a table entry below changes. A
#: stored `guardrail_verdict` records what the rules concluded; without this it
#: does not record *which* rules concluded it, and an audit six months after a
#: threshold moved cannot tell a correct old decision from a wrong one.
GUARDRAIL_VERSION = "2026.09.1"

MAX_DISCOUNTS_PER_90D = 1
MAX_B2B_TOUCHES = 3
MAX_AUTOPAY_RETRIES = 3

#: Diagnoses a cart discount is never justified for. Both are explicit in the
#: product story this guardrail exists to enforce: a technical payment failure
#: or a distracted shopper is not a price objection, and discounting either
#: trains customers to abandon carts while recovering nothing a free nudge
#: would not have. Checked here — not only in the model's system prompt — so
#: the rule holds even if the LLM disagrees with it.
CART_DISCOUNT_UNJUSTIFIED_DIAGNOSES = frozenset({"payment_failure", "distraction"})

#: Diagnoses that may never be handled by an automated action, whatever the model
#: proposes. Enforced here rather than by declining to ask the model, because
#: every case is now decided by the model — a control that works by not asking
#: stops being a control the moment the question gets asked anyway.
FORCED_ESCALATION_DIAGNOSES = frozenset({"disputed"})


@dataclass(frozen=True)
class GuardrailCapacity:
    """How much of each budgeted lever this customer has already used.

    Sourced from the `case_steps` table, never from an in-memory dict — the
    previous build's `AppState` counters silently reset on restart and made the
    guardrails unenforceable in practice.

    **Counting scope is per-field, and deliberately not uniform.** It follows
    what the lever protects, not what is convenient to query:

    * `discounts_in_90d` — **per customer, across every case they have.** A
      discount is margin given to a *person*; letting them collect one per
      abandoned cart would make the 90-day cap meaningless the moment somebody
      abandons twice.
    * `b2b_touches` — **per case.** The harm being capped is pestering someone
      about one invoice. Two genuinely separate invoices each deserve their own
      three touches.
    * `autopay_retries` — **per case.** A retry re-presents one specific
      mandate; attempts against a different mandate say nothing about this one.

    `cases.CaseStore.capacity_for` is what actually implements these scopes, and
    each guardrail function below restates the scope it relies on.
    """

    discounts_in_90d: int = 0
    b2b_touches: int = 0
    autopay_retries: int = 0
    step_count: int = 0

    def remaining_for(self, vertical: str) -> dict[str, int]:
        """Human-readable headroom, injected into the prompt so the model can see it."""
        if vertical == "cart":
            return {"discounts_remaining": max(0, MAX_DISCOUNTS_PER_90D - self.discounts_in_90d)}
        if vertical == "b2b":
            return {"outreach_touches_remaining": max(0, MAX_B2B_TOUCHES - self.b2b_touches)}
        if vertical == "autopay":
            return {"retries_remaining": max(0, MAX_AUTOPAY_RETRIES - self.autopay_retries)}
        return {}


#: `(allowed, reason)` — reason is `None` when allowed.
GuardrailResult = tuple[bool, str | None]

#: Escalation is never blocked. If it were, a case that exhausted every lever
#: would have nowhere to go, and the guardrail would be trapping revenue rather
#: than protecting the customer.
ALWAYS_ALLOWED = frozenset({ESCALATE_TO_HUMAN, WAIT_AND_REASSESS})


def check_cart_guardrails(
    action_name: str, capacity: GuardrailCapacity, diagnosis: str = ""
) -> GuardrailResult:
    """Cart: at most one discount per customer per 90 days, and only for price resistance.

    The diagnosis check runs first and is unconditional — unlike the 90-day cap,
    it is not a matter of remaining budget. A discount proposed against a
    technical decline or a distracted shopper is wrong regardless of how many
    the customer has had before.

    **Scope: per customer, spanning all of their cases.** `discounts_in_90d`
    counts every discount this customer has had anywhere, so a second abandoned
    cart cannot buy a second coupon.
    """
    if action_name in ALWAYS_ALLOWED:
        return True, None
    if action_name != SEND_DISCOUNT:
        return True, None
    if diagnosis in CART_DISCOUNT_UNJUSTIFIED_DIAGNOSES:
        return False, f"guardrail: discount_not_justified_for_{diagnosis}"
    if capacity.discounts_in_90d >= MAX_DISCOUNTS_PER_90D:
        return False, "guardrail: max_1_discount_per_90d"
    return True, None


def check_b2b_guardrails(
    action_name: str, capacity: GuardrailCapacity, diagnosis: str = ""
) -> GuardrailResult:
    """B2B: disputes need a human, and at most three outreach touches per invoice.

    **Scope: per case.** One case is one invoice, and the cap exists to stop us
    pestering someone about *that* invoice. A customer with two overdue invoices
    legitimately gets three touches on each.

    The dispute rule used to live in the routing governor, which skipped the
    model entirely on a disputed invoice — so the question was never asked. That
    was only ever safe while routing had the power to decide. Now that every case
    is decided by the model, "this must not be handled automatically" has to be
    enforced where a wrong answer is actually caught, which is here.
    """
    if action_name in ALWAYS_ALLOWED:
        return True, None
    if diagnosis in FORCED_ESCALATION_DIAGNOSES:
        # A dispute is a legal and relationship matter. No automated outreach is
        # acceptable on it, however confident the model happens to be.
        return False, f"guardrail: {diagnosis}_requires_human"
    if capacity.b2b_touches >= MAX_B2B_TOUCHES:
        return False, "guardrail: max_3_touches_escalation"
    return True, None


def check_autopay_guardrails(
    action_name: str, capacity: GuardrailCapacity, diagnosis: str = ""
) -> GuardrailResult:
    """Autopay: at most three retries, then stop and flag.

    Only `retry_charge` consumes retry budget — sending a fresh mandate or
    instrument link is a different lever and is not rate-limited by it.

    **Scope: per case.** A retry re-presents one specific mandate; failures
    against some other mandate of the same customer carry no information about
    whether this one will clear, so they must not consume this case's budget.
    """
    del diagnosis  # this vertical's guardrail is capacity-only; kept for a uniform call signature
    if action_name in ALWAYS_ALLOWED:
        return True, None
    if action_name != RETRY_CHARGE:
        return True, None
    if capacity.autopay_retries >= MAX_AUTOPAY_RETRIES:
        return False, "guardrail: max_3_autopay_retries"
    return True, None


GUARDRAIL_CHECKERS = {
    "cart": check_cart_guardrails,
    "b2b": check_b2b_guardrails,
    "autopay": check_autopay_guardrails,
}


def check_guardrails(
    vertical: str, action_name: str, capacity: GuardrailCapacity, diagnosis: str = ""
) -> GuardrailResult:
    """Single entry point. An unknown vertical fails closed, into escalation."""
    checker = GUARDRAIL_CHECKERS.get(vertical)
    if checker is None:
        return False, f"guardrail: unknown_vertical:{vertical}"
    return checker(action_name, capacity, diagnosis)
