"""Case lifecycle, guardrail capacity and priority scoring.

The guardrail counters live here, computed from `case_steps` on every read. The
previous build kept them in an in-memory dict on `AppState`, which meant a
restart silently reset every customer's discount and retry history — the
guardrails looked enforced and were not. Deriving them from the audit trail
makes the limit as durable as the record of it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    WAIT_AND_REASSESS,
)
from recoveryai.core.diagnosis import urgency_multiplier
from recoveryai.core.models import (
    ActionIntent,
    ActionStatus,
    CaseStatus,
    ExecutionResult,
    RecoveryEvent,
)
from recoveryai.core.policy import GUARDRAIL_VERSION, GuardrailCapacity
from recoveryai.db.models import Case, CaseStep

logger = logging.getLogger(__name__)

DISCOUNT_WINDOW_DAYS = 90

#: Statuses from which no further agent work happens.
TERMINAL_STATUSES = frozenset(
    {CaseStatus.resolved.value, CaseStatus.escalated.value, CaseStatus.abandoned.value}
)

#: Terminal statuses that represent a *judgement* rather than an outcome.
#:
#: A late failure report can legitimately re-open a `resolved` case, because
#: resolution is a claim about money that the report may have just retracted.
#: It must never re-open these two: a human owns the escalated case now, and
#: abandonment was a deliberate decision that one failed send does not revisit.
_HUMAN_OWNED_STATUSES = frozenset({CaseStatus.escalated.value, CaseStatus.abandoned.value})


class ConcurrentModificationError(RuntimeError):
    """Someone else wrote this case while we were deciding about it.

    Raised in place of SQLAlchemy's `StaleDataError` so callers can catch a
    domain error without importing the ORM's exception module, and so the case
    id travels with the failure. Callers decide what to do: the scheduler skips
    and lets the next sweep pick the case up with fresh state, an API handler
    retries once.

    Never swallowed at this layer. The whole value of the check is that a lost
    write becomes loud, and a store that quietly reconciled the conflict would
    be a slower way of losing it.
    """

    def __init__(self, case_id: str, message: str = "") -> None:
        self.case_id = case_id
        super().__init__(
            message or f"case {case_id!r} was modified by another writer; reload and retry"
        )


# ── Priority scoring ───────────────────────────────────────────────
#
# One column, two formulas, selected by whether a decision exists yet. The
# processing queue only ever reads undecided cases and the review queue only
# ever reads decided ones, so the column is unambiguous at every read site.


def intake_priority(event: RecoveryEvent) -> float:
    """`amount × urgency` — orders the work queue.

    Without this the queue is FIFO, and a ₹40,000 invoice waits behind a ₹200
    cart purely because it arrived a second later.
    """
    return round(event.amount * urgency_multiplier(event), 2)


#: What a missing confidence is worth in the review formula.
#:
#: Zero, not the 0.5 a silent default would give. A step arrives with no
#: confidence when nothing *assessed* the case — a forced escalation, a
#: guardrail that left exactly one legal move — and "nobody judged this" is the
#: strongest possible reason to put a human on it. Defaulting to 0.5 would bury
#: those cases mid-queue, which is precisely where an unreviewed decision should
#: never sit.
NO_CONFIDENCE = 0.0


def effective_confidence(confidence: float | None) -> float:
    """The confidence to score with, given one that may be absent.

    Named rather than inlined as `confidence or 0.0` on purpose: `or` also
    swallows a legitimate `0.0`, and the two cases deserve to stay
    distinguishable in the code even though they score the same.
    """
    return NO_CONFIDENCE if confidence is None else confidence


def review_priority(amount: float, confidence: float | None) -> float:
    """`(1 − confidence) × amount` — orders the human review queue.

    A large case the agent was unsure about outranks a small one that merely hit
    a guardrail ceiling. Reviewer attention is the scarcest resource in the
    system, so it is spent where uncertainty is most expensive.

    A `None` confidence scores as maximally uncertain rather than raising; see
    `NO_CONFIDENCE`.
    """
    return round(max(0.0, 1.0 - effective_confidence(confidence)) * amount, 2)


# ── Store ──────────────────────────────────────────────────────────


class CaseStore:
    """All case reads and writes. Holds a session; owns no transaction boundary."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Ingestion ──────────────────────────────────────────────

    def find_by_idempotency_key(self, key: str) -> Case | None:
        return self.session.scalar(select(Case).where(Case.idempotency_key == key))

    def get(self, case_id: str) -> Case | None:
        return self.session.get(Case, case_id)

    def create_from_event(
        self, event: RecoveryEvent, idempotency_key: str | None = None
    ) -> tuple[Case, bool]:
        """Idempotent intake. Returns `(case, created)`.

        Webhook senders retry on timeout — including on requests we in fact
        processed. Keying on the sender's `event_id` means a redelivery returns
        the original case instead of working the same money twice.
        """
        key = (idempotency_key or str(event.event_id)).strip()
        existing = self.find_by_idempotency_key(key)
        if existing is not None:
            return existing, False

        diagnosis, confidence, _reason = _rule_diagnosis(event)
        case = Case(
            id=f"case_{uuid4().hex[:16]}",
            idempotency_key=key,
            event_id=str(event.event_id),
            vertical=event.vertical.value,
            customer_id=event.customer_id,
            ltv_tier=event.customer_ltv_tier.value,
            amount=event.amount,
            currency=event.currency,
            status=CaseStatus.new.value,
            priority_score=intake_priority(event),
            raw_failure_reason=event.raw_failure_reason,
            diagnosis=diagnosis,
            vertical_metadata=dict(event.vertical_metadata or {}),
            latest_confidence=None,
        )
        self.session.add(case)
        self.session.flush()
        return case, True

    def to_event(self, case: Case) -> RecoveryEvent:
        """Rebuild the originating event from a stored case.

        Follow-up re-evaluations happen minutes or days later, long after the
        HTTP request that carried the original payload is gone.
        """
        return RecoveryEvent(
            event_id=case.event_id,
            vertical=case.vertical,
            customer_id=case.customer_id,
            customer_ltv_tier=case.ltv_tier,
            amount=case.amount,
            currency=case.currency,
            raw_failure_reason=case.raw_failure_reason,
            vertical_metadata=dict(case.vertical_metadata or {}),
            timestamp=case.created_at,
        )

    # ── Guardrail capacity ─────────────────────────────────────

    def capacity_for(self, case: Case) -> GuardrailCapacity:
        """Levers already spent on this customer/case.

        Counts include state the *host* reports in `vertical_metadata` — retries
        the bank already attempted, touches made before the case reached us.
        Ignoring those would let the agent hand a customer their fourth retry
        while believing it was the first.
        """
        meta = case.vertical_metadata or {}
        cutoff = datetime.now(UTC) - timedelta(days=DISCOUNT_WINDOW_DAYS)

        # Discounts are capped per *customer*, so this spans their other cases too.
        discounts = (
            self.session.scalar(
                select(func.count(CaseStep.id))
                .join(Case, Case.id == CaseStep.case_id)
                .where(
                    Case.customer_id == case.customer_id,
                    CaseStep.final_action == SEND_DISCOUNT,
                    CaseStep.created_at >= cutoff,
                )
            )
            or 0
        ) + int(meta.get("discounts_in_90d", 0) or 0)

        # Touches and retries are capped per *invoice*, i.e. per case.
        outreach = (
            self.session.scalar(
                select(func.count(CaseStep.id)).where(
                    CaseStep.case_id == case.id,
                    CaseStep.final_action.notin_([ESCALATE_TO_HUMAN, WAIT_AND_REASSESS]),
                )
            )
            or 0
        ) + int(meta.get("previous_touches", 0) or 0)

        retries = (
            self.session.scalar(
                select(func.count(CaseStep.id)).where(
                    CaseStep.case_id == case.id,
                    CaseStep.final_action == RETRY_CHARGE,
                )
            )
            or 0
        ) + int(meta.get("retry_count", 0) or 0)

        return GuardrailCapacity(
            discounts_in_90d=discounts,
            b2b_touches=outreach,
            autopay_retries=retries,
            step_count=case.step_count,
        )

    # ── Steps and lifecycle ────────────────────────────────────

    def record_step(
        self,
        case: Case,
        intent: ActionIntent,
        result: ExecutionResult,
        context_snapshot: dict[str, Any],
        llm_call_made: bool,
    ) -> CaseStep:
        """Append one decision to the audit trail and roll the case forward."""
        step = CaseStep(
            case_id=case.id,
            step_number=intent.step_number,
            decision_source=intent.decision_source.value,
            llm_call_made=llm_call_made,
            context_snapshot=context_snapshot,
            proposed_action=intent.proposed_action,
            final_action=intent.final_action,
            action_params=dict(intent.params or {}),
            reasoning=intent.reasoning,
            confidence=intent.confidence,
            guardrail_verdict=intent.guardrail_verdict.value,
            guardrail_reason=intent.guardrail_reason,
            # Falls back to the current constant rather than storing NULL: an
            # unstamped intent is one a caller hand-built, and the rules that
            # judged it were still the ones loaded in this process.
            guardrail_version=intent.guardrail_version or GUARDRAIL_VERSION,
            action_status=result.status.value,
            action_details=result.details,
            intent_id=str(intent.intent_id),
            was_shadow=result.status is ActionStatus.shadow_logged,
            cost=result.cost,
            recovered_amount=result.recovered_amount,
        )
        # Appended to the relationship rather than `session.add`-ed, so an
        # already-loaded `case.steps` collection reflects the new step. The next
        # decision builds its prompt from that collection; a stale one would
        # hand the model a case with no history.
        case.steps.append(step)

        case.step_count = intent.step_number
        case.cost_of_recovery = round((case.cost_of_recovery or 0.0) + result.cost, 2)
        case.amount_recovered = round((case.amount_recovered or 0.0) + result.recovered_amount, 2)
        case.latest_confidence = intent.confidence
        # A decision now exists, so the score switches to the review formula.
        case.priority_score = review_priority(case.amount, intent.confidence)
        case.updated_at = datetime.now(UTC)
        self._flush_guarding_version(case)
        return step

    def _flush_guarding_version(self, case: Case) -> None:
        """Flush, turning a lost-update collision into a domain error.

        SQLAlchemy detects the collision by row count: with `version_id_col` set,
        its UPDATE carries `WHERE version = <the value we read>`, so a writer
        that got there first leaves this one matching zero rows.
        """
        # Read the id *before* flushing. A failed flush leaves the session
        # awaiting a rollback, and any attribute access that has to touch the
        # database from there raises `PendingRollbackError` — which would bury
        # the conflict we are trying to report under a second, less useful error.
        case_id = case.id
        try:
            self.session.flush()
        except StaleDataError as exc:
            logger.warning("concurrent modification detected", extra={"case_id": case_id})
            raise ConcurrentModificationError(case_id) from exc

    def set_status(self, case: Case, status: CaseStatus) -> None:
        case.status = status.value
        case.updated_at = datetime.now(UTC)
        if status.value in TERMINAL_STATUSES:
            case.next_followup_at = None

    def schedule_followup(self, case: Case, delay_seconds: float) -> datetime:
        when = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        case.next_followup_at = when
        case.updated_at = datetime.now(UTC)
        return when

    def due_followups(self, limit: int = 50) -> list[Case]:
        """Cases whose follow-up time has passed, highest value first."""
        now = datetime.now(UTC)
        stmt = (
            select(Case)
            .where(
                Case.next_followup_at.is_not(None),
                Case.next_followup_at <= now,
                Case.status.notin_(list(TERMINAL_STATUSES)),
            )
            .order_by(Case.priority_score.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def apply_host_report(
        self, intent_id: str, status: str, details: str, cost: float, recovered_amount: float
    ) -> CaseStep | None:
        """Fold an async outcome report from the host back into the case.

        The awkward case this has to get right: the host already told us the
        dispatch was accepted, we recorded the step as `executed`, and only later
        does the host discover the action actually failed. Leaving the step
        reading `executed` next to a failure report is the inconsistent state —
        so the report wins, the step moves to `execution_failed`, and the case
        goes back to being open work rather than sitting on a success that did
        not happen.
        """
        step = self.session.scalar(select(CaseStep).where(CaseStep.intent_id == intent_id))
        if step is None:
            return None

        failed = status == ActionStatus.execution_failed.value
        case = self.session.get(Case, step.case_id)
        if case is not None:
            # Subtract this step's previous figures before adding the reported
            # ones, so repeated reports replace rather than accumulate.
            case.cost_of_recovery = round((case.cost_of_recovery or 0.0) - step.cost + cost, 2)
            case.amount_recovered = round(
                (case.amount_recovered or 0.0) - step.recovered_amount + recovered_amount, 2
            )
            case.updated_at = datetime.now(UTC)
            if recovered_amount > 0:
                self.set_status(case, CaseStatus.resolved)
            elif failed and case.amount_recovered <= 0 and case.status not in _HUMAN_OWNED_STATUSES:
                # The action we were counting on did not happen, so the case is
                # unfinished business again. Re-opened rather than escalated: the
                # agent still has steps and levers left, and burning a human on
                # a failed send before trying another one is the wrong default.
                #
                # This reaches back into `resolved` on purpose. The common shape
                # is a host reporting a recovery, then correcting itself — and
                # the correction has just unwound the recovery that justified
                # resolving, so leaving the case closed would strand real money.
                # Guarded on `amount_recovered` so a case carrying a *different*
                # step's genuine recovery stays resolved.
                self.set_status(case, CaseStatus.in_progress)

        step.action_status = status
        step.action_details = details or step.action_details
        step.cost = cost
        step.recovered_amount = recovered_amount
        if case is not None:
            self._flush_guarding_version(case)
        else:
            self.session.flush()
        return step


def _rule_diagnosis(event: RecoveryEvent) -> tuple[str, float, str]:
    from recoveryai.core.diagnosis import diagnose  # local: keeps import graph acyclic

    return diagnose(event)
