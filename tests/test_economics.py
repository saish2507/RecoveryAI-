"""What a case is worth working, and what that buys it.

The two properties that matter here are that an action's odds depend on what
actually broke, and that the resulting figure changes the agent's behaviour
rather than merely decorating a queue.
"""

from __future__ import annotations

from conftest import make_event
from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    SEND_MANDATE_RESETUP_LINK,
    SEND_NUDGE,
    SEND_PAYMENT_UPDATE_LINK,
)
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.economics import assess, band, success_rate
from recoveryai.core.llm.base import ToolCall
from recoveryai.core.models import CaseStatus
from recoveryai.core.policy import GuardrailCapacity
from recoveryai.core.verticals import get_vertical

# ── Odds depend on what broke ──────────────────────────────────────


def test_a_retry_never_succeeds_against_a_revoked_mandate() -> None:
    """The charge has no authorisation to run against. Not unlikely — impossible."""
    assert success_rate(RETRY_CHARGE, "mandate_broken") == 0.0


def test_a_retry_is_the_best_lever_for_a_temporary_shortfall() -> None:
    assert success_rate(RETRY_CHARGE, "low_balance") > success_rate(SEND_NUDGE, "low_balance")


def test_re_authorisation_beats_retrying_on_a_broken_mandate() -> None:
    """The ranking a flat per-action rate got backwards."""
    assert success_rate(SEND_MANDATE_RESETUP_LINK, "mandate_broken") > success_rate(
        RETRY_CHARGE, "mandate_broken"
    )


def test_a_new_instrument_beats_retrying_an_expired_card() -> None:
    assert success_rate(SEND_PAYMENT_UPDATE_LINK, "expired_instrument") > success_rate(
        RETRY_CHARGE, "expired_instrument"
    )


def test_an_unmapped_pair_falls_back_to_the_actions_own_rate() -> None:
    assert success_rate(SEND_NUDGE, "some_unseen_diagnosis") == 0.15


# ── Expected recovery ──────────────────────────────────────────────


def test_a_broken_mandate_is_never_assessed_on_a_retry() -> None:
    """The prioritiser must not recommend an attempt that cannot collect."""
    prospects = assess(
        vertical=get_vertical("autopay"),
        diagnosis="mandate_broken",
        amount=5_000.0,
        capacity=GuardrailCapacity(),
    )
    assert prospects.action == SEND_MANDATE_RESETUP_LINK


def test_a_disputed_invoice_is_worth_nothing_to_automation() -> None:
    """Second-largest exposure in the book, and zero for the agent to do.

    Sorting a work queue by amount puts this at the top. It belongs at the
    bottom of *that* queue and near the top of a human's.
    """
    prospects = assess(
        vertical=get_vertical("b2b"),
        diagnosis="disputed",
        amount=50_000.0,
        capacity=GuardrailCapacity(),
    )
    assert prospects.action is None
    assert prospects.value == 0.0
    assert prospects.worth_pursuing is False


def test_spent_capacity_lowers_what_a_case_is_worth() -> None:
    """Once the one permitted discount is gone, the case is worth what a nudge can do."""
    cart = get_vertical("cart")
    fresh = assess(
        vertical=cart, diagnosis="price_sensitivity", amount=10_000.0, capacity=GuardrailCapacity()
    )
    spent = assess(
        vertical=cart,
        diagnosis="price_sensitivity",
        amount=10_000.0,
        capacity=GuardrailCapacity(discounts_in_90d=1),
    )
    assert spent.value < fresh.value
    assert spent.action != SEND_DISCOUNT


def test_repeated_failures_lower_what_a_case_is_worth() -> None:
    """A customer who ignored two reminders is not a fresh coin flip on the third."""
    kwargs = dict(vertical=get_vertical("b2b"), diagnosis="forgot", amount=20_000.0,
                  capacity=GuardrailCapacity())
    assert assess(**kwargs, steps_taken=2).value < assess(**kwargs, steps_taken=0).value


def test_a_small_case_with_a_costly_lever_is_not_worth_pursuing() -> None:
    """The economic floor: stop when the attempt costs more than it recovers."""
    prospects = assess(
        vertical=get_vertical("b2b"),
        diagnosis="cash_flow_trouble",
        amount=40.0,  # a couple of rupees of expected recovery
        capacity=GuardrailCapacity(),
    )
    assert prospects.worth_pursuing is False


# ── Bands ──────────────────────────────────────────────────────────


def test_higher_expected_recovery_earns_a_shorter_wait() -> None:
    _p0, fast = band(80_000.0)
    _p3, slow = band(50.0)
    assert fast < slow


def test_an_ordinary_case_is_left_on_the_configured_cadence() -> None:
    """The band scales the configured delay; it does not replace it."""
    assert band(2_000.0)[1] == 1.0


# ── The connection to execution ────────────────────────────────────


def test_expected_recovery_sets_the_next_check_in(session, settings, governed_llm) -> None:
    """The whole point: the score decides *when*, not merely where in a list.

    Sorting only matters when there is a backlog to reorder. Setting the timer
    matters always, because it decides how many times a case is looked at at all.
    """
    settings.followup_delay_seconds = 3_600.0
    llm = governed_llm([ToolCall("send_nudge", {}, "chase", 0.6)] * 4)
    agent = RecoveryAgent(settings=settings, llm=llm)

    big = agent.handle_event(session, make_event("b2b", 90_000, days_overdue=20,
                                                 payment_history_score=0.6))[0]
    small = agent.handle_event(session, make_event("b2b", 900, days_overdue=20,
                                                  payment_history_score=0.6))[0]

    assert big.next_followup_at < small.next_followup_at


def test_a_case_not_worth_pursuing_is_closed_rather_than_ground_on(
    session, settings, governed_llm
) -> None:
    llm = governed_llm([ToolCall("offer_partial_payment_plan", {"split": "half now"}, "plan", 0.6)] * 4)
    agent = RecoveryAgent(settings=settings, llm=llm)

    case = agent.handle_event(
        session, make_event("b2b", 30.0, days_overdue=40, payment_history_score=0.2)
    )[0]

    assert case.status == CaseStatus.abandoned.value


def test_the_stored_figure_reflects_the_case_after_the_step(session, settings, governed_llm) -> None:
    llm = governed_llm([ToolCall("send_nudge", {}, "chase", 0.6)])
    agent = RecoveryAgent(settings=settings, llm=llm)

    case = agent.handle_event(session, make_event("b2b", 40_000, days_overdue=20,
                                                  payment_history_score=0.6))[0]

    assert case.expected_recovery > 0


def test_escalation_is_never_counted_as_something_that_collects() -> None:
    prospects = assess(
        vertical=get_vertical("b2b"),
        diagnosis="forgot",
        amount=10_000.0,
        capacity=GuardrailCapacity(b2b_touches=3),  # only escalation left
    )
    assert prospects.action != ESCALATE_TO_HUMAN


def test_expected_recovery_reaches_the_api(client) -> None:
    """The figure that schedules the case must also be visible on it.

    Adding the column to the ORM without adding it to the read model left the
    console rendering an em-dash for every case: the agent was scheduling on a
    number the UI could not see.
    """
    client.post("/api/v1/dev/inject?vertical=b2b")
    listed = client.get("/api/v1/cases?limit=1").json()["items"][0]

    assert "expected_recovery" in listed
    detail = client.get(f"/api/v1/cases/{listed['id']}").json()
    assert "expected_recovery" in detail
