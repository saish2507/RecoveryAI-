"""Follow-up scheduling — what makes a case a workflow rather than a fire-and-forget.

A recovery decision is rarely final. The nudge lands or it does not, and the
agent needs to look again. `FOLLOWUP_DELAY_SECONDS` defaults to the real
retry cadence (4 hours), so a workflow plays out on its own schedule; for a
live demo, use `/dev/followups/run` or `/dev/cases/{id}/advance` to force a
sweep on demand instead of waiting on it.

One polling job rather than a job per case: cases are already persisted with a
`next_followup_at`, so the database is the schedule. A per-case in-memory job
would be lost on restart and would have to be rebuilt from the same query this
job runs anyway.

APScheduler is the only import here, and it is not a web framework — `core` stays
embeddable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from recoveryai.core.agent import AgentDecision, RecoveryAgent
from recoveryai.core.cases import CaseStore, ConcurrentModificationError

logger = logging.getLogger(__name__)

#: How often to look for due follow-ups. Short enough that the demo feels live,
#: long enough that it is not a busy-loop against the database.
POLL_INTERVAL_SECONDS = 5

#: A job that fires late (process was busy, laptop slept) should still run —
#: dropping it would strand the case with no further re-evaluation.
MISFIRE_GRACE_SECONDS = 300


class FollowupScheduler:
    """Re-evaluates cases whose follow-up time has arrived.

    `on_decision` is an optional sink for live updates (the API passes a
    WebSocket broadcaster). Core knows nothing about what it does.
    """

    def __init__(
        self,
        agent: RecoveryAgent,
        session_scope: Callable[[], Any],
        on_decision: Callable[[AgentDecision], None] | None = None,
        poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.agent = agent
        self.session_scope = session_scope
        self.on_decision = on_decision
        self.poll_interval_seconds = poll_interval_seconds
        self._scheduler: Any = None
        self.last_run_at: datetime | None = None
        self.runs = 0
        self.decisions_made = 0
        self.errors = 0
        #: Sweeps skipped because the model had no headroom. Visible in the
        #: snapshot so a queue that looks stalled has an explanation.
        self.deferred_sweeps = 0
        #: Sweeps that hit a concurrent writer and stood down. Counted separately
        #: from `errors` because a conflict is the safety net working, not a
        #: fault — but a climbing number means two writers are fighting.
        self.skipped_conflicts = 0

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler  # noqa: PLC0415

        if self._scheduler is not None:
            return
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._scheduler.add_job(
            self.run_due_followups,
            trigger="interval",
            seconds=self.poll_interval_seconds,
            id="followup_sweep",
            max_instances=1,  # never let two sweeps work the same case concurrently
            coalesce=True,  # a backlog of missed ticks collapses into one run
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
        self._scheduler.start()
        logger.info("follow-up scheduler started", extra={"interval": self.poll_interval_seconds})

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("follow-up scheduler stopped")

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def _affordable_batch(self, limit: int) -> int:
        """How many cases this sweep can decide properly, capped by `limit`.

        An agent with no model available still works — the policy tables are a
        complete decision procedure — so a sweep is only deferred when the model
        is *temporarily* out of headroom and waiting will change the answer.
        Deferring when there is no model configured at all would stall recovery
        forever waiting on capacity that is never coming.
        """
        llm = getattr(self.agent, "llm", None)
        if llm is None or not llm.is_available():
            return limit
        headroom = getattr(llm, "headroom", None)
        if headroom is None:
            return limit
        return max(0, min(limit, headroom()))

    # ── The job ────────────────────────────────────────────────

    def run_due_followups(self, limit: int = 25) -> list[AgentDecision]:
        """Advance the highest-value cases the model has capacity to decide properly.

        Each case gets its own transaction. One poisoned case must not roll back
        the others' work or take the sweep down — a scheduler that dies on a
        single bad row stops the whole recovery pipeline silently.

        The batch is sized to the model's remaining headroom, not to `limit`.
        Taking twenty-five due cases against a four-per-minute ceiling meant the
        first few were decided by the agent and the rest silently fell back to
        the policy tables — so whether a case got judgement depended on where it
        sat in the list, which is not a property anyone chose. A case left for
        the next sweep five seconds later loses nothing; a case decided by a
        lookup table because of queue position loses the entire point.
        """
        self.runs += 1
        self.last_run_at = datetime.now(UTC)
        decisions: list[AgentDecision] = []

        batch = self._affordable_batch(limit)
        if batch == 0:
            self.deferred_sweeps += 1
            logger.debug("sweep deferred: no model headroom")
            return decisions

        try:
            with self.session_scope() as session:
                due_ids = [case.id for case in CaseStore(session).due_followups(limit=batch)]
        except Exception:
            self.errors += 1
            logger.exception("failed to query due follow-ups")
            return decisions

        for case_id in due_ids:
            try:
                with self.session_scope() as session:
                    case = CaseStore(session).get(case_id)
                    if case is None:
                        continue
                    # Clear it first: if the decision below fails, the case must
                    # not be picked up again on every subsequent tick forever.
                    case.next_followup_at = None
                    decision = self.agent.advance_case(session, case)

                if decision is not None:
                    decisions.append(decision)
                    self.decisions_made += 1
                    if self.on_decision is not None:
                        try:
                            self.on_decision(decision)
                        except Exception:
                            # A broken notification sink must not undo a decision
                            # that is already committed.
                            logger.exception("follow-up notification failed")
            except ConcurrentModificationError:
                # Someone else — a host outcome report, most likely — wrote this
                # case between our read and our write. Skip rather than retry:
                # the other writer's version is the current truth, and this
                # case's `next_followup_at` is already cleared, so the next
                # sweep picks it up against fresh state. Retrying in-loop would
                # re-decide from the state we know is stale.
                self.skipped_conflicts += 1
                logger.info("follow-up skipped; case changed underneath", extra={"case_id": case_id})
            except Exception:
                self.errors += 1
                logger.exception("follow-up failed for case", extra={"case_id": case_id})

        if decisions:
            logger.info("follow-up sweep advanced cases", extra={"count": len(decisions)})
        return decisions

    # ── Introspection ──────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "poll_interval_seconds": self.poll_interval_seconds,
            "misfire_grace_seconds": MISFIRE_GRACE_SECONDS,
            "runs": self.runs,
            "decisions_made": self.decisions_made,
            "errors": self.errors,
            "deferred_sweeps": self.deferred_sweeps,
            "skipped_conflicts": self.skipped_conflicts,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
        }
