"""Guardrail checkers and decision tables, in isolation.

Ported from the previous build's `test_guardrails.py`. These are the cheapest
tests in the suite and cover the highest-consequence code: a wrong answer here
is money given away or a customer harassed.
"""

from __future__ import annotations

import pytest
from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    SEND_MANDATE_RESETUP_LINK,
    SEND_NUDGE,
    SEND_PAYMENT_UPDATE_LINK,
    WAIT_AND_REASSESS,
)
from recoveryai.core.policy import (
    MAX_AUTOPAY_RETRIES,
    MAX_B2B_TOUCHES,
    GuardrailCapacity,
    check_autopay_guardrails,
    check_b2b_guardrails,
    check_cart_guardrails,
    check_guardrails,
)
from recoveryai.core.verticals import VERTICALS

FRESH = GuardrailCapacity()


# ── Cart: one discount per 90 days ─────────────────────────────────


def test_first_discount_is_allowed() -> None:
    assert check_cart_guardrails(SEND_DISCOUNT, FRESH) == (True, None)


def test_second_discount_is_blocked_with_a_named_reason() -> None:
    allowed, reason = check_cart_guardrails(SEND_DISCOUNT, GuardrailCapacity(discounts_in_90d=1))
    assert allowed is False
    assert reason == "guardrail: max_1_discount_per_90d"


def test_discount_cap_does_not_restrict_other_cart_actions() -> None:
    spent = GuardrailCapacity(discounts_in_90d=5)
    assert check_cart_guardrails(SEND_NUDGE, spent)[0] is True
    assert check_cart_guardrails(SEND_PAYMENT_UPDATE_LINK, spent)[0] is True


# ── B2B: three touches, then escalation ────────────────────────────


@pytest.mark.parametrize("touches", [0, 1, 2])
def test_outreach_allowed_below_the_touch_cap(touches: int) -> None:
    assert check_b2b_guardrails(SEND_NUDGE, GuardrailCapacity(b2b_touches=touches))[0] is True


@pytest.mark.parametrize("touches", [3, 4, 99])
def test_outreach_blocked_at_and_beyond_the_touch_cap(touches: int) -> None:
    allowed, reason = check_b2b_guardrails(SEND_NUDGE, GuardrailCapacity(b2b_touches=touches))
    assert allowed is False
    assert reason == "guardrail: max_3_touches_escalation"


def test_payment_plan_also_counts_as_a_touch() -> None:
    spent = GuardrailCapacity(b2b_touches=MAX_B2B_TOUCHES)
    assert check_b2b_guardrails(OFFER_PARTIAL_PAYMENT_PLAN, spent)[0] is False


# ── Autopay: three retries ─────────────────────────────────────────


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_retry_allowed_below_the_cap(retries: int) -> None:
    assert check_autopay_guardrails(RETRY_CHARGE, GuardrailCapacity(autopay_retries=retries))[0] is True


def test_retry_blocked_at_the_cap() -> None:
    allowed, reason = check_autopay_guardrails(
        RETRY_CHARGE, GuardrailCapacity(autopay_retries=MAX_AUTOPAY_RETRIES)
    )
    assert allowed is False
    assert reason == "guardrail: max_3_autopay_retries"


def test_retry_cap_does_not_block_fixing_the_underlying_problem() -> None:
    """Retries being spent is the moment a mandate reset matters most."""
    spent = GuardrailCapacity(autopay_retries=9)
    assert check_autopay_guardrails(SEND_MANDATE_RESETUP_LINK, spent)[0] is True
    assert check_autopay_guardrails(SEND_PAYMENT_UPDATE_LINK, spent)[0] is True


# ── Universal invariants ───────────────────────────────────────────


@pytest.mark.parametrize("vertical", ["cart", "b2b", "autopay"])
def test_escalation_is_never_blocked(vertical: str) -> None:
    """A case with no legal move would be revenue trapped by its own guardrail."""
    maxed = GuardrailCapacity(discounts_in_90d=99, b2b_touches=99, autopay_retries=99)
    assert check_guardrails(vertical, ESCALATE_TO_HUMAN, maxed) == (True, None)


@pytest.mark.parametrize("vertical", ["cart", "b2b", "autopay"])
def test_waiting_is_never_blocked(vertical: str) -> None:
    maxed = GuardrailCapacity(discounts_in_90d=99, b2b_touches=99, autopay_retries=99)
    assert check_guardrails(vertical, WAIT_AND_REASSESS, maxed) == (True, None)


def test_unknown_vertical_fails_closed() -> None:
    allowed, reason = check_guardrails("crypto_moonshot", SEND_NUDGE, FRESH)
    assert allowed is False
    assert "unknown_vertical" in reason


@pytest.mark.parametrize("name,config", sorted(VERTICALS.items()))
def test_every_fallback_chain_ends_somewhere_always_permitted(name, config) -> None:
    """The redirect invariant: a blocked proposal must always have a destination."""
    maxed = GuardrailCapacity(discounts_in_90d=99, b2b_touches=99, autopay_retries=99)
    assert config.fallback_chain, f"{name} has no fallback chain"
    assert any(check_guardrails(name, candidate, maxed)[0] for candidate in config.fallback_chain)


@pytest.mark.parametrize("name,config", sorted(VERTICALS.items()))
def test_fallback_chain_only_uses_actions_the_vertical_offers(name, config) -> None:
    for candidate in config.fallback_chain:
        assert candidate in config.tool_palette, f"{name} falls back to unofferred {candidate!r}"


# ── Diagnosis-aware discount guardrail ──────────────────────────────
#
# The product story this system is built against is explicit: a payment
# failure gets a free nudge, a distracted shopper gets a free nudge, and only
# genuine price resistance earns a discount. These tests pin that the guardrail
# enforces it, which is now the only thing that does — the model proposes every
# action, so a rule that lived anywhere else would be a suggestion.


@pytest.mark.parametrize("diagnosis", ["payment_failure", "distraction"])
def test_discount_is_refused_for_diagnoses_it_is_not_justified_by(diagnosis: str) -> None:
    """A card decline or a distracted shopper is not a price objection."""
    allowed, reason = check_cart_guardrails(SEND_DISCOUNT, FRESH, diagnosis)
    assert allowed is False
    assert reason == f"guardrail: discount_not_justified_for_{diagnosis}"


def test_the_diagnosis_check_fires_even_with_a_fresh_90_day_window() -> None:
    """This is not a capacity limit — a first-ever discount can still be wrong."""
    never_discounted = GuardrailCapacity(discounts_in_90d=0)
    allowed, _reason = check_cart_guardrails(SEND_DISCOUNT, never_discounted, "payment_failure")
    assert allowed is False


def test_price_sensitivity_is_the_only_diagnosis_that_earns_a_discount() -> None:
    allowed, reason = check_cart_guardrails(SEND_DISCOUNT, FRESH, "price_sensitivity")
    assert allowed is True
    assert reason is None


def test_an_unspecified_diagnosis_does_not_block_a_discount() -> None:
    """Callers outside the agent loop (tests, tooling) that omit diagnosis are unaffected."""
    assert check_cart_guardrails(SEND_DISCOUNT, FRESH)[0] is True


def test_the_diagnosis_check_does_not_touch_other_cart_actions() -> None:
    for action in (SEND_NUDGE, SEND_PAYMENT_UPDATE_LINK, ESCALATE_TO_HUMAN):
        assert check_cart_guardrails(action, FRESH, "payment_failure")[0] is True


@pytest.mark.parametrize("vertical", ["b2b", "autopay"])
def test_other_verticals_accept_the_uniform_call_signature_without_using_it(vertical: str) -> None:
    """b2b/autopay guardrails ignore diagnosis; the call shape must still work."""
    assert check_guardrails(vertical, ESCALATE_TO_HUMAN, FRESH, "irrelevant") == (True, None)
