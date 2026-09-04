"""Did the shadow run agree with what actually happened?

Shadow mode answers "what would this agent have done", and that is only half of
a pilot. The other half is whether those decisions were any good, and the only
evidence for that is what the host's own process achieved on the same cases —
which arrives through `POST /api/v1/actions/{intent_id}/report` like any other
outcome.

**What "agreement" means here.** Not "the agent picked the same action the human
did" — the report does not carry the host's action, and inventing a taxonomy to
map one onto the other would be a worse lie than a simpler measure. What is
compared is the agent's *judgement about recoverability*:

* the agent chose to work the case, and money came in  → agreed (`worked`)
* the agent chose to stop or escalate, and none did    → agreed (`gave_up`)
* the agent chose to work it, and nothing came in      → disagreed
* the agent chose to stop, and money came in anyway    → disagreed, and this is
  the expensive direction: revenue the agent would have left on the table

That is a coarse measure and this module says so rather than dressing it up. It
is a go/no-go signal for a pilot — "the agent's instinct matched reality 8 times
in 10" — not a model evaluation. The counts are the deliverable; the ratio is
offered because everyone computes it anyway.

Read-only. Evaluating a decision never changes a case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from recoveryai.core.actions import CLOSE_AS_UNRECOVERABLE, ESCALATE_TO_HUMAN, WAIT_AND_REASSESS
from recoveryai.core.models import ActionStatus
from recoveryai.db.models import CaseStep

#: Actions that are not an attempt to recover the money on this step. Two of
#: them hand the case elsewhere; `wait_and_reassess` defers, which is a decision
#: not to act *now* and so is scored as declining to work it.
NON_RECOVERY_ACTIONS = frozenset({ESCALATE_TO_HUMAN, CLOSE_AS_UNRECOVERABLE, WAIT_AND_REASSESS})


@dataclass
class ShadowComparison:
    """One shadow decision set beside the outcome the host reported for it."""

    case_id: str
    step_number: int
    intent_id: str
    shadow_action: str
    agent_would_have_acted: bool
    reported_status: str
    recovered_amount: float
    agreed: bool
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "step_number": self.step_number,
            "intent_id": self.intent_id,
            "shadow_action": self.shadow_action,
            "agent_would_have_acted": self.agent_would_have_acted,
            "reported_status": self.reported_status,
            "recovered_amount": self.recovered_amount,
            "agreed": self.agreed,
            "verdict": self.verdict,
        }


@dataclass
class ShadowEvaluation:
    """The summary. Counts first; the ratio is a convenience, not the point."""

    shadow_steps: int = 0
    evaluated: int = 0
    agreements: int = 0
    disagreements: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    comparisons: list[ShadowComparison] = field(default_factory=list)

    @property
    def awaiting_outcome(self) -> int:
        """Shadow decisions no host has reported on yet. Not a disagreement."""
        return self.shadow_steps - self.evaluated

    @property
    def agreement_rate(self) -> float | None:
        """`None`, not `0.0`, when nothing has been reported yet.

        A pilot on its first day has no agreement rate. Reporting one as zero
        would read as "the agent is wrong about everything", which is the
        opposite of what an empty sample means.
        """
        if not self.evaluated:
            return None
        return round(self.agreements / self.evaluated, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shadow_steps": self.shadow_steps,
            "evaluated": self.evaluated,
            "awaiting_outcome": self.awaiting_outcome,
            "agreements": self.agreements,
            "disagreements": self.disagreements,
            "agreement_rate": self.agreement_rate,
            "by_verdict": self.by_verdict,
            "comparisons": [c.as_dict() for c in self.comparisons],
        }


def _classify(would_have_acted: bool, recovered: bool) -> tuple[bool, str]:
    if would_have_acted and recovered:
        return True, "agreed_worked"
    if not would_have_acted and not recovered:
        return True, "agreed_gave_up"
    if would_have_acted and not recovered:
        return False, "acted_but_nothing_recovered"
    return False, "gave_up_but_money_arrived"


def _has_outcome(step: CaseStep) -> bool:
    """Whether a host has reported on this step yet.

    A shadow step is written with `action_status=shadow_logged` and stays that
    way until somebody reports; anything else means a report landed. Checked
    alongside `recovered_amount` so a report of "executed, recovered 0" counts
    as evaluated rather than as silence.
    """
    return step.action_status != ActionStatus.shadow_logged.value or step.recovered_amount > 0


def evaluate_shadow_decisions(session: Session, limit: int = 200) -> ShadowEvaluation:
    """Compare every reported-on shadow decision against its outcome."""
    steps = list(
        session.scalars(
            select(CaseStep)
            .where(CaseStep.was_shadow.is_(True))
            .order_by(CaseStep.created_at.desc())
            .limit(limit)
        )
    )

    evaluation = ShadowEvaluation(shadow_steps=len(steps))
    for step in steps:
        if not _has_outcome(step):
            continue

        would_have_acted = step.final_action not in NON_RECOVERY_ACTIONS
        recovered = (step.recovered_amount or 0.0) > 0
        agreed, verdict = _classify(would_have_acted, recovered)

        evaluation.evaluated += 1
        if agreed:
            evaluation.agreements += 1
        else:
            evaluation.disagreements += 1
        evaluation.by_verdict[verdict] = evaluation.by_verdict.get(verdict, 0) + 1
        evaluation.comparisons.append(
            ShadowComparison(
                case_id=step.case_id,
                step_number=step.step_number,
                intent_id=step.intent_id,
                shadow_action=step.final_action,
                agent_would_have_acted=would_have_acted,
                reported_status=step.action_status,
                recovered_amount=round(float(step.recovered_amount or 0.0), 2),
                agreed=agreed,
                verdict=verdict,
            )
        )

    return evaluation
