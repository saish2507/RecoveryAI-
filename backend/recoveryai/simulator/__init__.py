"""Demo traffic generator.

Event pools ported from the previous build's simulator. What is *not* ported is
the `AmbiguousCaseCapper`: the old simulator decided which cases were "ambiguous"
by consulting the LLM budget, which meant the data changed shape depending on how
much quota was left — the fake traffic was quietly designed around the model
rather than the model being tested against realistic traffic.

Here the simulator emits a realistic mix and nothing else.

The pools intentionally include unrecognised gateway codes, because real ones do.
"""

from __future__ import annotations

import math
import random
from typing import Any
from uuid import uuid4

from recoveryai.core.models import LTVTier, RecoveryEvent, Vertical

# ── Event pools ────────────────────────────────────────────────────

LTV_TIERS = [LTVTier.low, LTVTier.medium, LTVTier.high]

CART_POOL: dict[str, Any] = {
    "error_codes": [
        "card_declined",
        "insufficient_funds",
        "otp_timeout",
        None,
        # Codes the rules do not know. These are the cases that earn an LLM call,
        # and in production they are a permanent fact of gateway integration.
        "3DS_CHALLENGE_ABANDONED",
        "ISSUER_RISK_HOLD_B7",
    ],
    "error_weights": [0.34, 0.22, 0.12, 0.14, 0.10, 0.08],
    "ltv_weights": [0.5, 0.3, 0.2],
    "session_durations": [5, 15, 45, 120],
    "session_weights": [0.45, 0.25, 0.18, 0.12],
    "price_ratios": [0.8, 1.0, 1.2, 1.5, 2.0],
    "price_weights": [0.2, 0.3, 0.27, 0.15, 0.08],
    "amount_range": (50.0, 5_000.0),
}

B2B_POOL: dict[str, Any] = {
    "dispute_weights": [0.1, 0.9],
    "days_overdue_range": (1, 90),
    "payment_history_range": (0.0, 1.0),
    "previous_touches_range": (0, 4),
    "ltv_weights": [0.4, 0.35, 0.25],
    "amount_range": (1_000.0, 50_000.0),
}

AUTOPAY_POOL: dict[str, Any] = {
    "error_codes": [
        "INSUFFICIENT_FUNDS",
        "CARD_EXPIRED",
        "MANDATE_EXPIRED",
        "MANDATE_REVOKED",
        "NACH_RETURN_UNSPECIFIED",
        "PSP_ERR_5522",
    ],
    "error_weights": [0.36, 0.18, 0.13, 0.11, 0.12, 0.10],
    "ltv_weights": [0.5, 0.3, 0.2],
    "retry_count_range": (0, 4),
    "amount_range": (200.0, 2_000.0),
}

VERTICAL_WEIGHTS = [0.4, 0.35, 0.25]  # cart, b2b, autopay


def _amount(bounds: tuple[float, float]) -> float:
    return round(random.uniform(*bounds), 2)


def _tier(weights: list[float]) -> LTVTier:
    return random.choices(LTV_TIERS, weights=weights)[0]


# ── Generators ─────────────────────────────────────────────────────


def generate_cart_event(customer_id: str | None = None) -> RecoveryEvent:
    pool = CART_POOL
    error_code = random.choices(pool["error_codes"], weights=pool["error_weights"])[0]
    return RecoveryEvent(
        vertical=Vertical.cart,
        customer_id=customer_id or f"cart_cust_{uuid4().hex[:8]}",
        customer_ltv_tier=_tier(pool["ltv_weights"]),
        amount=_amount(pool["amount_range"]),
        raw_failure_reason=error_code or "",
        vertical_metadata={
            "payment_gateway_error_code": error_code,
            "session_duration_seconds": random.choices(
                pool["session_durations"], weights=pool["session_weights"]
            )[0],
            "price_vs_customer_avg": random.choices(
                pool["price_ratios"], weights=pool["price_weights"]
            )[0],
        },
    )


def generate_b2b_event(customer_id: str | None = None) -> RecoveryEvent:
    pool = B2B_POOL
    dispute = random.choices([True, False], weights=pool["dispute_weights"])[0]
    return RecoveryEvent(
        vertical=Vertical.b2b,
        customer_id=customer_id or f"b2b_cust_{uuid4().hex[:8]}",
        customer_ltv_tier=_tier(pool["ltv_weights"]),
        amount=_amount(pool["amount_range"]),
        raw_failure_reason="dispute_initiated" if dispute else "payment_pending",
        vertical_metadata={
            "dispute_flag": dispute,
            "days_overdue": random.randint(*pool["days_overdue_range"]),
            "payment_history_score": round(random.uniform(*pool["payment_history_range"]), 2),
            "previous_touches": random.randint(*pool["previous_touches_range"]),
        },
    )


def generate_autopay_event(customer_id: str | None = None) -> RecoveryEvent:
    pool = AUTOPAY_POOL
    error_code = random.choices(pool["error_codes"], weights=pool["error_weights"])[0]
    return RecoveryEvent(
        vertical=Vertical.autopay,
        customer_id=customer_id or f"autopay_cust_{uuid4().hex[:8]}",
        customer_ltv_tier=_tier(pool["ltv_weights"]),
        amount=_amount(pool["amount_range"]),
        raw_failure_reason=error_code,
        vertical_metadata={
            "bank_error_code": error_code,
            "retry_count": random.randint(*pool["retry_count_range"]),
        },
    )


GENERATORS = {
    Vertical.cart: generate_cart_event,
    Vertical.b2b: generate_b2b_event,
    Vertical.autopay: generate_autopay_event,
}


def generate_event(vertical: Vertical | None = None, customer_id: str | None = None) -> RecoveryEvent:
    """One event. Vertical is sampled from a realistic mix unless specified."""
    if vertical is None:
        vertical = random.choices(list(GENERATORS), weights=VERTICAL_WEIGHTS)[0]
    return GENERATORS[vertical](customer_id)


# ── Named demo scenarios ───────────────────────────────────────────
#
# Deterministic fixtures for driving a live demo. They use a *stable* customer id
# per vertical so repeated injections accumulate guardrail state — which is how
# you show a guardrail block actually firing rather than describing one.

SCENARIOS: dict[str, dict[str, Any]] = {
    "cart_price_sensitive": {
        "vertical": Vertical.cart,
        "description": "High-LTV shopper, long session, well above their usual spend. Discount territory — "
        "inject twice to watch the 90-day discount guardrail block the second one.",
        "ltv": LTVTier.high,
        "amount": 4_250.0,
        "raw_failure_reason": "",
        "metadata": {
            "payment_gateway_error_code": None,
            "session_duration_seconds": 210,
            "price_vs_customer_avg": 1.45,
        },
    },
    "cart_unknown_gateway_code": {
        "vertical": Vertical.cart,
        "description": "A gateway code the rules have never seen. Routes to the agent as ambiguous "
        "instead of failing validation — the bug class that broke the previous build.",
        "ltv": LTVTier.medium,
        "amount": 1_890.0,
        "raw_failure_reason": "ISSUER_RISK_HOLD_B7",
        "metadata": {"payment_gateway_error_code": "ISSUER_RISK_HOLD_B7", "session_duration_seconds": 95},
    },
    "b2b_disputed": {
        "vertical": Vertical.b2b,
        "description": "Disputed invoice. Escalates to a human without spending an LLM call — the "
        "outcome is forced, so asking the model would waste budget.",
        "ltv": LTVTier.high,
        "amount": 28_400.0,
        "raw_failure_reason": "dispute_initiated",
        "metadata": {"dispute_flag": True, "days_overdue": 22, "payment_history_score": 0.88},
    },
    "b2b_touches_exhausted": {
        "vertical": Vertical.b2b,
        "description": "Three outreach touches already spent. Escalation is forced by the guardrail.",
        "ltv": LTVTier.medium,
        "amount": 12_750.0,
        "raw_failure_reason": "payment_pending",
        "metadata": {"dispute_flag": False, "days_overdue": 41, "payment_history_score": 0.62,
                     "previous_touches": 3},
    },
    "b2b_ambiguous": {
        "vertical": Vertical.b2b,
        "description": "Decent history, meaningfully late, no dispute. Genuinely a judgement call.",
        "ltv": LTVTier.high,
        "amount": 41_000.0,
        "raw_failure_reason": "payment_pending",
        "metadata": {"dispute_flag": False, "days_overdue": 34, "payment_history_score": 0.66,
                     "previous_touches": 1},
    },
    "autopay_retries_exhausted": {
        "vertical": Vertical.autopay,
        "description": "Three bank-side retries already burned. The retry guardrail redirects to "
        "asking the customer for a new instrument.",
        "ltv": LTVTier.medium,
        "amount": 1_450.0,
        "raw_failure_reason": "INSUFFICIENT_FUNDS",
        "metadata": {"bank_error_code": "INSUFFICIENT_FUNDS", "retry_count": 3},
    },
    "autopay_mandate_revoked": {
        "vertical": Vertical.autopay,
        "description": "Revoked mandate. Retrying can never work; only re-authorisation fixes it.",
        "ltv": LTVTier.high,
        "amount": 1_999.0,
        "raw_failure_reason": "MANDATE_REVOKED",
        "metadata": {"bank_error_code": "MANDATE_REVOKED", "retry_count": 1},
    },
    "autopay_unknown_bank_code": {
        "vertical": Vertical.autopay,
        "description": "Unrecognised NACH return code after repeated failures. Ambiguous by design.",
        "ltv": LTVTier.low,
        "amount": 720.0,
        "raw_failure_reason": "NACH_RETURN_UNSPECIFIED",
        "metadata": {"bank_error_code": "NACH_RETURN_UNSPECIFIED", "retry_count": 3},
    },
}


def build_scenario(name: str, customer_id: str | None = None) -> RecoveryEvent:
    """One of the named demo scenarios above."""
    try:
        spec = SCENARIOS[name]
    except KeyError as exc:
        raise ValueError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}") from exc

    vertical: Vertical = spec["vertical"]
    return RecoveryEvent(
        vertical=vertical,
        customer_id=customer_id or f"demo_{vertical.value}_customer",
        customer_ltv_tier=spec["ltv"],
        amount=spec["amount"],
        raw_failure_reason=spec["raw_failure_reason"],
        vertical_metadata=dict(spec["metadata"]),
    )


# ── Permutation matrix ─────────────────────────────────────────────
#
# Ten archetypes that between them exercise every distinct path through
# `core/diagnosis.py`, walked in order rather than sampled. Random traffic at
# volume buries the interesting cases in duplicates of the common ones; walking
# the decision space on purpose means a watcher sees each behaviour exactly once
# per lap.
#
# What is fixed per archetype is only what *determines the diagnosis* — the error
# code, the dispute flag, which side of a threshold a signal falls on. Everything
# else is drawn per case.
#
# An earlier version fixed the amounts too, so the b2b/forgot slot was ₹41,000
# on every single lap and the queue filled with rows identical but for their id.
# Coverage was fine and the data was obviously synthetic at a glance, which is
# the one thing demo traffic cannot afford to be.

PERMUTATION_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "label": "cart / payment_failure / confident",
        "vertical": Vertical.cart,
        "ltv_weights": [0.5, 0.35, 0.15],
        "amount_range": (240.0, 4_800.0),
        # A code the rules recognise: pins the diagnosis, frees everything else.
        "codes": ["card_declined", "insufficient_funds", "otp_timeout", "do_not_honour"],
        "signals": {"session_duration_seconds": ("int", 30, 400)},
    },
    {
        "label": "cart / distraction / confident",
        "vertical": Vertical.cart,
        "ltv_weights": [0.45, 0.4, 0.15],
        "amount_range": (320.0, 6_500.0),
        "codes": [None],
        # Under 30s is what makes this "distraction" — the ceiling is load-bearing.
        "signals": {
            "session_duration_seconds": ("int", 3, 28),
            "price_vs_customer_avg": ("float", 0.7, 1.05),
        },
    },
    {
        "label": "cart / price_sensitivity / discount-eligible",
        "vertical": Vertical.cart,
        "ltv_weights": [0.1, 0.35, 0.55],
        "amount_range": (2_400.0, 24_000.0),
        "codes": [None],
        # Above 1.3 is what makes this "price sensitivity".
        "signals": {
            "session_duration_seconds": ("int", 90, 600),
            "price_vs_customer_avg": ("float", 1.35, 2.6),
        },
    },
    {
        "label": "cart / unknown gateway code / ambiguous",
        "vertical": Vertical.cart,
        "ltv_weights": [0.3, 0.4, 0.3],
        "amount_range": (600.0, 12_000.0),
        "codes": ["ISSUER_RISK_HOLD_B7", "3DS_CHALLENGE_ABANDONED", "PSP_DECLINE_X22", "ACQ_ERR_5561"],
        "signals": {"session_duration_seconds": ("int", 20, 300)},
    },
    {
        "label": "b2b / disputed / requires a human",
        "vertical": Vertical.b2b,
        "ltv_weights": [0.15, 0.35, 0.5],
        "amount_range": (8_500.0, 96_000.0),
        "codes": ["dispute_initiated", "chargeback_raised"],
        "signals": {
            "dispute_flag": ("const", True),
            "days_overdue": ("int", 5, 75),
            "payment_history_score": ("float", 0.4, 0.95),
        },
    },
    {
        "label": "b2b / cash_flow_trouble / payment plan",
        "vertical": Vertical.b2b,
        "ltv_weights": [0.4, 0.4, 0.2],
        "amount_range": (6_000.0, 72_000.0),
        "codes": ["payment_pending", "invoice_overdue"],
        # History below 0.5 is what makes this "cash flow trouble".
        "signals": {
            "dispute_flag": ("const", False),
            "days_overdue": ("int", 20, 110),
            "payment_history_score": ("float", 0.05, 0.48),
        },
    },
    {
        "label": "b2b / forgot / ambiguous middle",
        "vertical": Vertical.b2b,
        "ltv_weights": [0.2, 0.35, 0.45],
        "amount_range": (4_500.0, 88_000.0),
        "codes": ["payment_pending", "invoice_overdue"],
        # Decent history, meaningfully late, no dispute: a genuine judgement call.
        "signals": {
            "dispute_flag": ("const", False),
            "days_overdue": ("int", 8, 55),
            "payment_history_score": ("float", 0.55, 0.8),
        },
    },
    {
        "label": "autopay / low_balance / retry can work",
        "vertical": Vertical.autopay,
        "ltv_weights": [0.4, 0.4, 0.2],
        "amount_range": (149.0, 3_600.0),
        "codes": ["INSUFFICIENT_FUNDS", "LOW_BALANCE"],
        "signals": {"retry_count": ("int", 0, 1)},
    },
    {
        "label": "autopay / expired_instrument / new card needed",
        "vertical": Vertical.autopay,
        "ltv_weights": [0.45, 0.35, 0.2],
        "amount_range": (199.0, 2_800.0),
        "codes": ["CARD_EXPIRED", "INSTRUMENT_EXPIRED", "MANDATE_EXPIRED"],
        "signals": {"retry_count": ("int", 0, 2)},
    },
    {
        "label": "autopay / mandate_broken / retry can never work",
        "vertical": Vertical.autopay,
        "ltv_weights": [0.25, 0.35, 0.4],
        "amount_range": (299.0, 5_400.0),
        "codes": ["MANDATE_REVOKED", "MANDATE_CANCELLED", "MANDATE_PAUSED"],
        "signals": {"retry_count": ("int", 1, 3)},
    },
)

PERMUTATION_CYCLE_LENGTH = len(PERMUTATION_MATRIX)

#: Matches every customer id the permutation walk has ever minted. Used to resume
#: the walk after a restart; see `permutation_customer_pattern`.
PERMUTATION_ID_PATTERN = "%\\_perm%\\_c%"

#: Which metadata key carries the failure code, per vertical. Cart and autopay
#: name it differently, and B2B carries none at all.
_CODE_KEY = {Vertical.cart: "payment_gateway_error_code", Vertical.autopay: "bank_error_code"}


def permutation_customer_pattern() -> str:
    """SQL LIKE pattern for permutation-generated customers.

    Exists so `main._simulator_loop` can count what the walk has already emitted
    and carry on from there, rather than starting over at slot zero every time
    the process restarts.
    """
    return PERMUTATION_ID_PATTERN


def _sample(spec: tuple[Any, ...]) -> Any:
    kind = spec[0]
    if kind == "const":
        return spec[1]
    if kind == "int":
        return random.randint(spec[1], spec[2])
    return round(random.uniform(spec[1], spec[2]), 2)


def _sample_amount(low: float, high: float) -> float:
    """A believable figure — log-uniform, and never round.

    Log-uniform because a linear draw over 240–24,000 puts almost everything in
    the thousands, whereas real traffic is mostly small with a long tail. The odd
    paise matter for a duller reason: a column of round numbers reads as
    placeholder data however varied the magnitudes are.
    """
    rupees = int(math.exp(random.uniform(math.log(low), math.log(high))))
    # Paise picked separately rather than added as a float and rounded: adding
    # 0.996 to a whole number and rounding to 2dp gives a whole number back, so
    # roughly one amount in a hundred came out round anyway.
    return rupees + random.randint(1, 99) / 100


def build_permutation_event(index: int) -> tuple[RecoveryEvent, str]:
    """The `index`-th case of the permutation walk, wrapping every 10.

    *Which* archetype comes up is deterministic — that is what guarantees every
    diagnosis path is covered once per lap. The numbers inside it are not: amount,
    LTV tier, failure code and every numeric signal are drawn per case, so two
    cases from the same slot never look alike.

    Returns `(event, label)`. The customer id carries the lap number so each lap
    injects fresh customers — otherwise the per-customer discount guardrail would
    block lap two onward and every later cycle would read as a guardrail story
    rather than the diagnosis story it is meant to show.

    `index` must therefore be continuous across restarts, not merely across one
    process's lifetime. When it was a plain in-memory counter, every restart
    replayed lap zero and re-minted `cart_perm0_c0` and friends — duplicate rows
    in the queue, and worse, a "fresh" customer who had already spent their one
    discount in a previous run. The caller seeds it; see
    `permutation_customer_pattern`.
    """
    slot = index % PERMUTATION_CYCLE_LENGTH
    spec = PERMUTATION_MATRIX[slot]
    cycle = index // PERMUTATION_CYCLE_LENGTH
    vertical: Vertical = spec["vertical"]

    code = random.choice(spec["codes"])
    metadata: dict[str, Any] = {name: _sample(rng) for name, rng in spec["signals"].items()}
    code_key = _CODE_KEY.get(vertical)
    if code_key is not None:
        metadata[code_key] = code

    event = RecoveryEvent(
        vertical=vertical,
        customer_id=f"{vertical.value}_perm{slot}_c{cycle}",
        customer_ltv_tier=_tier(spec["ltv_weights"]),
        amount=_sample_amount(*spec["amount_range"]),
        raw_failure_reason=code or "",
        vertical_metadata=metadata,
    )
    return event, spec["label"]


