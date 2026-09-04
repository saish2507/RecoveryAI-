"""What a case is worth working, and how hard.

Everything else in the system reasons about *which* action fits a case. This
module answers a different question: given that we know what we would do, how
much money is realistically still on the table, and does that justify the
attempt at all.

Two ideas, in order.

**An action's odds depend on what broke, not just on the action.** Re-presenting
a mandate recovers roughly half of temporary balance shortfalls and exactly none
of the revoked mandates — the charge cannot succeed against authorisation that no
longer exists. A single success rate per action cannot express that, and a
prioritiser built on one will confidently rank an attempt that has no chance of
working above one that does.

**Expected recovery, not face value.** A ₹50,000 invoice under active dispute is
worth nothing to an automated workflow, because the guardrail forbids every
action that could collect it; a ₹2,000 balance shortfall is worth about ₹900. The
larger number is the one to leave alone. Sorting a work queue by amount gets this
exactly backwards.

The rates below are illustrative constants, not a fitted model — the same caveat
that applies to `actions.recovery_rate`. What they encode is the *ordering* and
the zeroes, which are domain facts rather than estimates: a re-authorisation link
beats a retry on a dead mandate, and a retry on a dead mandate is not a small
number but zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from recoveryai.core.actions import (
    ACTION_REGISTRY,
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    SEND_MANDATE_RESETUP_LINK,
    SEND_NUDGE,
    SEND_PAYMENT_UPDATE_LINK,
    WAIT_AND_REASSESS,
)
from recoveryai.core.policy import GuardrailCapacity

#: `(action, diagnosis) -> probability the attempt collects the money`.
#:
#: Absent pairs fall back to the action's own `recovery_rate`. Zeroes are stated
#: explicitly and deliberately: they are the entries that stop the prioritiser
#: recommending something that cannot work.
SUCCESS_RATES: dict[tuple[str, str], float] = {
    # Retrying works when the account was merely empty, and never once the
    # authorisation behind it is gone.
    (RETRY_CHARGE, "low_balance"): 0.45,
    (RETRY_CHARGE, "expired_instrument"): 0.02,
    (RETRY_CHARGE, "mandate_broken"): 0.0,
    (RETRY_CHARGE, "suspicious"): 0.05,
    # A new instrument fixes an expired card and does nothing for an empty account.
    (SEND_PAYMENT_UPDATE_LINK, "expired_instrument"): 0.35,
    (SEND_PAYMENT_UPDATE_LINK, "low_balance"): 0.05,
    (SEND_PAYMENT_UPDATE_LINK, "mandate_broken"): 0.08,
    # Re-authorisation is the only thing that repairs a broken mandate.
    (SEND_MANDATE_RESETUP_LINK, "mandate_broken"): 0.40,
    (SEND_MANDATE_RESETUP_LINK, "expired_instrument"): 0.10,
    (SEND_MANDATE_RESETUP_LINK, "low_balance"): 0.03,
    # A reminder helps someone who forgot and barely moves a price objection.
    (SEND_NUDGE, "forgot"): 0.25,
    (SEND_NUDGE, "distraction"): 0.20,
    (SEND_NUDGE, "payment_failure"): 0.10,
    (SEND_NUDGE, "price_sensitivity"): 0.05,
    (SEND_NUDGE, "cash_flow_trouble"): 0.05,
    # A coupon answers a price objection and insults a technical decline.
    (SEND_DISCOUNT, "price_sensitivity"): 0.35,
    (SEND_DISCOUNT, "distraction"): 0.12,
    # A fresh instrument is the answer to a card that will not authorise, and no
    # answer at all to a shopper who thinks the price is too high — their card
    # was fine. Stated explicitly because the per-action fallback is 0.18, high
    # enough to make "ask for a new card" outrank a reminder on a cart the
    # customer simply thought was expensive.
    (SEND_PAYMENT_UPDATE_LINK, "price_sensitivity"): 0.02,
    (SEND_PAYMENT_UPDATE_LINK, "distraction"): 0.03,
    (SEND_PAYMENT_UPDATE_LINK, "payment_failure"): 0.30,
    (SEND_MANDATE_RESETUP_LINK, "price_sensitivity"): 0.01,
    (SEND_MANDATE_RESETUP_LINK, "distraction"): 0.01,
    # Instalments are for customers who want to pay and cannot right now.
    (OFFER_PARTIAL_PAYMENT_PLAN, "cash_flow_trouble"): 0.40,
    (OFFER_PARTIAL_PAYMENT_PLAN, "forgot"): 0.15,
}

#: Actions that collect nothing by construction. Escalation hands the case to a
#: person and waiting deliberately does nothing, so neither belongs in a
#: calculation of what automation can still recover.
NON_COLLECTING = frozenset({ESCALATE_TO_HUMAN, WAIT_AND_REASSESS})

#: Each failed attempt makes the next one less likely: a customer who ignored two
#: reminders is not a fresh coin flip on the third. Without this the score would
#: rate a case that has already failed twice exactly as highly as an untouched
#: one, and the agent would keep spending on cases that have stopped responding.
ATTEMPT_DECAY = 0.75

#: Expected recovery below which another automated attempt is not worth making.
#:
#: Stands in for the costs an action's own price tag does not capture — the model
#: call behind the decision, the operational attention, and the goodwill spent on
#: contacting someone again. Set conservatively: the point is to stop the agent
#: grinding on cases worth a couple of rupees, not to abandon recoverable money.
MIN_WORTH_PURSUING = 25.0


def success_rate(action: str, diagnosis: str) -> float:
    """Odds `action` collects, given what is actually wrong with the case."""
    specific = SUCCESS_RATES.get((action, diagnosis))
    if specific is not None:
        return specific
    fallback = ACTION_REGISTRY.get(action)
    return fallback.recovery_rate if fallback else 0.0


@dataclass(frozen=True)
class Prospects:
    """What automation can still realistically collect on a case."""

    #: Expected recovery net of what the attempt costs, in currency units.
    value: float
    #: The action the estimate assumes, or `None` when nothing legal can collect.
    action: str | None
    #: Odds that action lands, after decay for attempts already spent.
    probability: float
    #: What running it is expected to cost.
    cost: float

    @property
    def worth_pursuing(self) -> bool:
        """False when another attempt is not worth what it takes to make one.

        Compared against `MIN_WORTH_PURSUING` rather than zero. A reminder has no
        direct cost, so `value > 0` is satisfied by any positive amount however
        trivial — a ₹40 invoice clears it on ₹2 of expected recovery, and the
        floor never fires on precisely the cases it exists to stop. Working a
        case is not free even when the action is: it spends a model call, a slot
        in the queue, and a share of the customer's patience.
        """
        return self.value >= MIN_WORTH_PURSUING


#: Expected-recovery thresholds and what they buy, as multiples of the configured
#: follow-up delay. The bands exist because priority previously had no effect on
#: anything: the sweep filtered by due time and *then* sorted by score, so the
#: score only ever broke ties between cases already eligible — and with traffic
#: this light there were never enough simultaneous ties for it to matter. Letting
#: the score set the delay is what connects it to behaviour, because the delay is
#: the only input to whether a case is eligible at all.
#:
#: `1.0` is the configured cadence, so an ordinary case is unaffected and the
#: band only decides who is looked at *more* or *less* often than the default.
PRIORITY_BANDS: tuple[tuple[float, str, float], ...] = (
    (25_000.0, "P0", 0.25),
    (5_000.0, "P1", 0.5),
    (1_000.0, "P2", 1.0),
    (0.0, "P3", 2.0),
)


def band(value: float) -> tuple[str, float]:
    """`(label, delay multiplier)` for an expected-recovery figure."""
    for threshold, label, multiplier in PRIORITY_BANDS:
        if value >= threshold:
            return label, multiplier
    return PRIORITY_BANDS[-1][1], PRIORITY_BANDS[-1][2]


def assess(
    *,
    vertical,
    diagnosis: str,
    amount: float,
    capacity: GuardrailCapacity,
    steps_taken: int = 0,
) -> Prospects:
    """Best case for continuing to work this case automatically.

    Considers only actions this vertical offers *and* the guardrails currently
    permit, so the estimate tracks the case's real position: once the one
    permitted discount is spent, a price-sensitive cart's prospects fall to what
    a plain reminder can do, with no separate bookkeeping to keep in sync.
    """
    best: Prospects | None = None
    decay = ATTEMPT_DECAY**steps_taken

    for name in vertical.tool_palette:
        if name in NON_COLLECTING:
            continue
        if not vertical.guardrail(name, capacity, diagnosis)[0]:
            continue

        action = ACTION_REGISTRY.get(name)
        if action is None:
            continue

        probability = success_rate(name, diagnosis) * decay
        cost = round(amount * action.cost_rate + action.flat_cost, 2)
        value = round(amount * probability - cost, 2)

        if best is None or value > best.value:
            best = Prospects(value=value, action=name, probability=probability, cost=cost)

    return best or Prospects(value=0.0, action=None, probability=0.0, cost=0.0)
