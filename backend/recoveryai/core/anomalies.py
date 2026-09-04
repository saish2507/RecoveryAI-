"""Spotting that something broke upstream, before anyone files a ticket.

Every other part of this system reasons about one case at a time. That is the
right unit for deciding an action and the wrong unit for noticing that a bank
started rejecting every mandate twenty minutes ago. Forty cases, each correctly
diagnosed `mandate_broken` and each correctly sent a re-authorisation link, is
forty good decisions and one missed incident — the agent will happily work them
one by one while the actual problem is that an issuer changed something.

So this module reads the population rather than the case: it compares a recent
window against the period before it and reports what changed shape. It decides
nothing and sends nothing; it is a read-only view over cases that already exist.

The comparison is deliberately naive — recent rate against trailing rate, with a
floor on absolute counts. A proper seasonal model would be better and is not
what a prototype at ten cases an hour needs; what it needs is to not cry wolf
when two cases arrive in a quiet hour, which the count floor handles.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from recoveryai.core.cases import TERMINAL_STATUSES
from recoveryai.db.models import Case

#: How far back "now" reaches.
DEFAULT_WINDOW_MINUTES = 60

#: How much history the recent window is judged against. Longer is steadier but
#: slower to accept a genuine change in the business as the new normal.
DEFAULT_BASELINE_HOURS = 24

#: A signal must clear all three to be reported. The count floor is what stops a
#: quiet night — where one extra case is a 300% rise — from paging anyone.
MIN_RECENT_COUNT = 3
SPIKE_FACTOR = 3.0
NEW_SIGNAL_MIN_COUNT = 4


@dataclass(frozen=True)
class Anomaly:
    """One thing that looks unlike the recent past."""

    kind: str
    key: str
    label: str
    recent_count: int
    recent_per_hour: float
    baseline_per_hour: float
    factor: float
    amount_at_risk: float
    severity: str
    detail: str
    sample_case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "recent_count": self.recent_count,
            "recent_per_hour": round(self.recent_per_hour, 2),
            "baseline_per_hour": round(self.baseline_per_hour, 2),
            "factor": round(self.factor, 2) if self.factor != float("inf") else None,
            "amount_at_risk": round(self.amount_at_risk, 2),
            "severity": self.severity,
            "detail": self.detail,
            "sample_case_ids": self.sample_case_ids,
        }


def _error_code(case: Case) -> str | None:
    """The upstream code behind this case, whichever vertical named it.

    Cart and autopay put it under different metadata keys and B2B has none at
    all, so this is where that inconsistency stops mattering to callers.
    """
    meta = case.vertical_metadata or {}
    raw = meta.get("bank_error_code") or meta.get("payment_gateway_error_code")
    code = str(raw or case.raw_failure_reason or "").strip()
    return code.upper() or None


def _severity(amount: float, factor: float) -> str:
    if amount >= 50_000 or factor >= 8:
        return "high"
    if amount >= 10_000 or factor >= 5:
        return "medium"
    return "low"


def _tally(cases: list[Case]) -> tuple[Counter, Counter, dict[str, float], dict[str, list[str]]]:
    """Count each case under both its diagnosis key and its error-code key."""
    diagnoses: Counter = Counter()
    codes: Counter = Counter()
    amounts: dict[str, float] = defaultdict(float)
    samples: dict[str, list[str]] = defaultdict(list)

    for case in cases:
        diag_key = f"{case.vertical}/{case.diagnosis}"
        diagnoses[diag_key] += 1
        amounts[diag_key] += case.amount or 0.0
        if len(samples[diag_key]) < 3:
            samples[diag_key].append(case.id)

        code = _error_code(case)
        if code:
            code_key = f"{case.vertical}/{code}"
            codes[code_key] += 1
            amounts[code_key] += case.amount or 0.0
            if len(samples[code_key]) < 3:
                samples[code_key].append(case.id)

    return diagnoses, codes, amounts, samples


def _compare(
    kind: str,
    recent: Counter,
    baseline: Counter,
    amounts: dict[str, float],
    samples: dict[str, list[str]],
    window_hours: float,
    baseline_hours: float,
) -> list[Anomaly]:
    found: list[Anomaly] = []

    for key, count in recent.items():
        recent_rate = count / window_hours
        baseline_rate = baseline.get(key, 0) / baseline_hours

        if baseline_rate == 0:
            # Nothing like this in the trailing window at all. Held to a higher
            # count than a spike, because "first ever occurrence" is the noisiest
            # possible signal and a single odd gateway code is not an incident.
            if count < NEW_SIGNAL_MIN_COUNT:
                continue
            factor = float("inf")
            detail = f"no occurrences in the previous {baseline_hours:.0f}h; {count} in the last hour"
        else:
            if count < MIN_RECENT_COUNT:
                continue
            factor = recent_rate / baseline_rate
            if factor < SPIKE_FACTOR:
                continue
            detail = (
                f"{recent_rate:.1f}/h now against a {baseline_rate:.1f}/h baseline "
                f"({factor:.1f}× normal)"
            )

        amount = amounts.get(key, 0.0)
        found.append(
            Anomaly(
                kind=kind,
                key=key,
                label=key.replace("/", " · "),
                recent_count=count,
                recent_per_hour=recent_rate,
                baseline_per_hour=baseline_rate,
                factor=factor,
                amount_at_risk=amount,
                severity=_severity(amount, factor),
                detail=detail,
                sample_case_ids=samples.get(key, []),
            )
        )

    return found


def detect_anomalies(
    session: Session,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    baseline_hours: int = DEFAULT_BASELINE_HOURS,
) -> list[Anomaly]:
    """Signals that spiked in the recent window, worst first.

    Ordered by money rather than by how extreme the ratio is: a 12× spike in
    ₹200 carts matters less than a 3× spike in ₹40,000 invoices, and the person
    reading this has finite attention.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=window_minutes)
    baseline_start = window_start - timedelta(hours=baseline_hours)

    recent_cases = list(
        session.scalars(select(Case).where(Case.created_at >= window_start, Case.created_at <= now))
    )
    baseline_cases = list(
        session.scalars(
            select(Case).where(Case.created_at >= baseline_start, Case.created_at < window_start)
        )
    )

    if not recent_cases:
        return []

    window_hours = max(window_minutes / 60.0, 1e-6)
    recent_diag, recent_codes, amounts, samples = _tally(recent_cases)
    base_diag, base_codes, _, _ = _tally(baseline_cases)

    found = _compare(
        "diagnosis_spike", recent_diag, base_diag, amounts, samples, window_hours, baseline_hours
    )
    found += _compare(
        "error_code_spike", recent_codes, base_codes, amounts, samples, window_hours, baseline_hours
    )
    found += _escalation_anomaly(recent_cases, baseline_cases, window_hours)

    found.sort(key=lambda a: (a.amount_at_risk, a.recent_count), reverse=True)
    return found


def _escalation_anomaly(
    recent: list[Case], baseline: list[Case], window_hours: float
) -> list[Anomaly]:
    """Flag the agent handing off far more than it usually does.

    Distinct from a diagnosis spike: the mix of incoming cases can be perfectly
    normal while the agent's ability to finish them collapses — a guardrail
    misconfiguration, or an LLM outage pushing everything down the fallback path.
    That shows up here and nowhere else.
    """
    if len(recent) < MIN_RECENT_COUNT or not baseline:
        return []

    def rate(cases: list[Case]) -> float:
        closed = [c for c in cases if c.status in TERMINAL_STATUSES]
        if not closed:
            return 0.0
        return sum(1 for c in closed if c.status == "escalated") / len(closed)

    recent_rate, baseline_rate = rate(recent), rate(baseline)
    if recent_rate < 0.5 or recent_rate <= baseline_rate * 1.5:
        return []

    amount = sum(c.amount or 0.0 for c in recent if c.status == "escalated")
    factor = recent_rate / baseline_rate if baseline_rate else float("inf")
    return [
        Anomaly(
            kind="escalation_spike",
            key="all/escalation_rate",
            label="escalation rate",
            recent_count=sum(1 for c in recent if c.status == "escalated"),
            recent_per_hour=sum(1 for c in recent if c.status == "escalated") / window_hours,
            baseline_per_hour=0.0,
            factor=factor,
            amount_at_risk=amount,
            severity=_severity(amount, factor),
            detail=(
                f"{recent_rate:.0%} of closed cases escalated in this window, "
                f"against {baseline_rate:.0%} normally"
            ),
        )
    ]
