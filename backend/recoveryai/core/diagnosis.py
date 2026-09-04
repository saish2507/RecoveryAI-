"""Zero-cost rule-based diagnosis — the prior the model reasons from.

Adapted from the previous build's `diagnosis.py`. The rules themselves are
unchanged; what changed is their standing. They no longer *decide* anything —
they produce a diagnosis and a confidence, and the agent uses that confidence to
decide whether the case is worth an LLM call at all. Most traffic is resolved
here at zero cost and zero latency, which is what makes the budget last.

Nothing in this module performs I/O or calls a model.
"""

from __future__ import annotations

from typing import Any

from recoveryai.core.models import LTVTier, RecoveryEvent

#: Confidence at or below which a diagnosis is considered genuinely ambiguous.
#:
#: Describes the rules' own certainty, not a routing decision — every case goes
#: to the model regardless. It travels in the prompt so the model knows how much
#: weight the prior deserves, and it is what the diagnosis tests assert against.
AMBIGUITY_THRESHOLD = 0.7

UNKNOWN = "unknown"


def _normalise(code: Any) -> str:
    return str(code or "").strip().lower()


def _optional_float(value: Any) -> float | None:
    """Parse a numeric signal, keeping "absent" distinct from any real value.

    The distinction matters more than it looks. Defaulting a missing
    `payment_history_score` to 1.0 silently asserts a perfect track record for a
    customer we have never billed — an unknown treated as the best possible case.
    Returning `None` forces callers to decide what to do about not knowing,
    which is the honest question.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Cart ───────────────────────────────────────────────────────────

CART_CODE_RULES: dict[str, str] = {
    "card_declined": "payment_failure",
    "insufficient_funds": "payment_failure",
    "otp_timeout": "payment_failure",
    "do_not_honour": "payment_failure",
    "do_not_honor": "payment_failure",
    "3ds_failed": "payment_failure",
    "gateway_timeout": "payment_failure",
}


def diagnose_cart(event: RecoveryEvent) -> tuple[str, float, str]:
    meta = event.vertical_metadata or {}
    error_code = _normalise(meta.get("payment_gateway_error_code") or event.raw_failure_reason)
    session_duration = meta.get("session_duration_seconds", 0) or 0
    price_vs_avg = _optional_float(meta.get("price_vs_customer_avg"))

    if error_code:
        mapped = CART_CODE_RULES.get(error_code)
        if mapped:
            return mapped, 0.95, f"gateway_error={error_code} → {mapped}"
        # A code we have never seen. This is the case `raw_failure_reason`-as-str
        # exists to serve: it is a judgement call, not a validation failure.
        return "payment_failure", 0.45, f"unrecognised gateway code {error_code!r} → needs judgement"

    if session_duration and session_duration < 30:
        return "distraction", 0.9, f"session {session_duration}s < 30s → distraction"

    if price_vs_avg is None:
        # A first-time shopper has no "customer average" to compare against, so
        # the price signal is absent rather than neutral. Saying "no strong price
        # signal" here would be a claim we cannot support.
        return "distraction", 0.4, "first-time shopper: no spending baseline to judge price against"

    if price_vs_avg > 1.3:
        return "price_sensitivity", 0.9, f"price ratio {price_vs_avg} > 1.3 → price sensitivity"

    if 1.1 <= price_vs_avg <= 1.3:
        return "price_sensitivity", 0.5, f"price ratio {price_vs_avg} in ambiguous 1.1–1.3 band"

    return "distraction", 0.55, f"price ratio {price_vs_avg} is unremarkable; no gateway error"


# ── B2B ────────────────────────────────────────────────────────────

B2B_CODE_RULES: dict[str, str] = {
    "dispute_initiated": "disputed",
    "chargeback_raised": "disputed",
    "payment_pending": "forgot",
    "invoice_overdue": "forgot",
}


def diagnose_b2b(event: RecoveryEvent) -> tuple[str, float, str]:
    meta = event.vertical_metadata or {}
    dispute_flag = bool(meta.get("dispute_flag", False))
    days_overdue = meta.get("days_overdue", 0) or 0
    payment_history = _optional_float(meta.get("payment_history_score"))

    # A dispute is a legal/relationship matter, never an automated nudge.
    if dispute_flag or _normalise(event.raw_failure_reason) in {"dispute_initiated", "chargeback_raised"}:
        return "disputed", 1.0, "dispute flagged → mandatory human escalation"

    if payment_history is None:
        # Cold start. Every rule below reads a track record this customer does
        # not have, so none of them can fire honestly.
        #
        # The previous behaviour defaulted a missing score to 1.0, which meant a
        # company we had never billed was scored identically to one with a proven
        # record — and at 0.9 confidence, which is above the ambiguity threshold.
        # So the case skipped the model *and* sank below the review queue: high
        # certainty asserted from zero evidence, failing in the unsafe direction.
        #
        # A nudge is still the right cheap opening move on an unknown customer.
        # What changes is the confidence attached to it: low enough that the
        # agent gets consulted, and that a large amount surfaces for a human.
        if days_overdue > 60:
            return (
                "cash_flow_trouble",
                0.4,
                f"{days_overdue}d overdue and no payment history on file — non-payment "
                "is the concern, but there is no track record to confirm it",
            )
        return (
            "forgot",
            0.35,
            f"{days_overdue}d overdue, no payment history on file — first-time customer, "
            "so no basis to judge whether they usually pay",
        )

    if days_overdue < 7 and payment_history > 0.8:
        return "forgot", 0.9, f"{days_overdue}d overdue, history {payment_history:.2f} → simple oversight"

    if payment_history < 0.5:
        return "cash_flow_trouble", 0.9, f"payment history {payment_history:.2f} < 0.5 → cash-flow trouble"

    if days_overdue > 60:
        return "cash_flow_trouble", 0.65, f"{days_overdue}d overdue → likely cash-flow trouble"

    # The messy middle: decent history, meaningfully late. Genuinely a judgement call.
    return "forgot", 0.5, f"{days_overdue}d overdue with mid history {payment_history:.2f} → ambiguous"


# ── Autopay ────────────────────────────────────────────────────────

AUTOPAY_CODE_RULES: dict[str, str] = {
    "insufficient_funds": "low_balance",
    "low_balance": "low_balance",
    "card_expired": "expired_instrument",
    "instrument_expired": "expired_instrument",
    "mandate_expired": "expired_instrument",
    "mandate_revoked": "mandate_broken",
    "mandate_cancelled": "mandate_broken",
    "mandate_paused": "mandate_broken",
}


def diagnose_autopay(event: RecoveryEvent) -> tuple[str, float, str]:
    meta = event.vertical_metadata or {}
    error_code = _normalise(meta.get("bank_error_code") or event.raw_failure_reason)
    retry_count = meta.get("retry_count", 0) or 0

    mapped = AUTOPAY_CODE_RULES.get(error_code)
    if mapped:
        # Repeated "insufficient funds" stops being a balance blip and starts
        # being a signal about the customer — worth a second look.
        if mapped == "low_balance" and retry_count >= 2:
            return mapped, 0.6, f"{error_code} but already retried {retry_count}× → diminishing returns"
        return mapped, 0.95, f"bank code {error_code} → {mapped}"

    if retry_count >= 3:
        return "suspicious", 0.5, f"unrecognised code {error_code!r} after {retry_count} retries"

    if error_code:
        return "suspicious", 0.45, f"unrecognised bank code {error_code!r} → needs judgement"

    return "suspicious", 0.4, "no bank error code supplied"


DIAGNOSERS = {
    "cart": diagnose_cart,
    "b2b": diagnose_b2b,
    "autopay": diagnose_autopay,
}


def diagnose(event: RecoveryEvent) -> tuple[str, float, str]:
    """Rule diagnosis for any vertical. Returns `(diagnosis, confidence, reasoning)`."""
    diagnoser = DIAGNOSERS.get(event.vertical.value)
    if diagnoser is None:
        return UNKNOWN, 0.0, f"no diagnoser for vertical {event.vertical.value!r}"
    return diagnoser(event)


def urgency_multiplier(event: RecoveryEvent) -> float:
    """Domain heuristic in roughly [0.5, 2.0], multiplied by amount for queue order.

    The point is that a $500 cart abandoned 20 seconds ago and a $500 invoice 80
    days overdue are not equally urgent, and FIFO cannot tell them apart.
    """
    meta = event.vertical_metadata or {}
    tier_weight = {LTVTier.high: 1.4, LTVTier.medium: 1.1, LTVTier.low: 0.9}[event.customer_ltv_tier]

    if event.vertical.value == "cart":
        # Cart intent decays fast; a fresh abandonment is the one worth chasing.
        session = meta.get("session_duration_seconds", 0) or 0
        base = 1.2 if session >= 60 else 1.0
    elif event.vertical.value == "b2b":
        # Collectability falls off with age, so lean on recent invoices, but a
        # dispute is urgent regardless of age.
        days = meta.get("days_overdue", 0) or 0
        base = 1.5 if meta.get("dispute_flag") else max(0.6, 1.3 - (days / 120.0))
    else:  # autopay
        # Each failed retry raises the odds of an outright churn.
        retries = meta.get("retry_count", 0) or 0
        base = 1.0 + min(0.5, retries * 0.15)

    return round(max(0.5, min(2.0, base * tier_weight)), 4)
