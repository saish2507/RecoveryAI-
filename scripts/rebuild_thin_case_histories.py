"""Rebuild cases whose stored history is thinner than the workflow now produces.

Cases created before outcomes were deferred to the following check-in were
opened and closed inside a single step: one row asserting a full recovery at the
same instant the first message was dispatched. That is not a trace anyone can
audit, and it does not match what the agent now does, so those records
misrepresent the system to anyone reading the queue.

This replays them. Each case is re-run through the real agent — real decisions,
real guardrails, real drafted copy — with only the *outcome* pinned, so the
resulting history is genuine rather than fabricated. The original thin rows are
removed once their replacement is committed.

Run against a stopped or idle API, from the repo root:

    venv/Scripts/python.exe scripts/rebuild_thin_case_histories.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from recoveryai.core import actions as actions_module  # noqa: E402
from recoveryai.core.actions import FIRST_OBSERVABLE_STEP  # noqa: E402
from recoveryai.core.agent import RecoveryAgent  # noqa: E402
from recoveryai.core.cases import CaseStore  # noqa: E402
from recoveryai.core.economics import assess  # noqa: E402
from recoveryai.core.models import CaseStatus, RecoveryEvent  # noqa: E402
from recoveryai.core.policy import GuardrailCapacity  # noqa: E402
from recoveryai.core.verticals import get_vertical  # noqa: E402
from recoveryai.db.models import Case, CaseStep  # noqa: E402
from recoveryai.db.session import session_scope  # noqa: E402

#: Histories shorter than this are the ones worth rebuilding.
THIN_HISTORY_STEPS = 2

#: The provider's per-minute ceiling is low and every decision now costs a call,
#: so the replay paces itself rather than tripping the limiter and degrading the
#: very decisions it is trying to record properly.
SECONDS_BETWEEN_DECISIONS = 16.0


def thin_cases(session) -> list[Case]:
    return list(
        session.query(Case)
        .filter(Case.status == CaseStatus.resolved.value, Case.step_count < THIN_HISTORY_STEPS)
        .order_by(Case.created_at)
    )


def last_collectable_step(vertical_name: str, diagnosis: str, amount: float) -> int:
    """The last step on which some permitted action can still collect.

    Not simply `max_steps`: B2B exhausts its three outreach touches and is forced
    into escalation on the fourth decision, so aiming a replay at the step cap
    would produce an escalated case rather than the resolved history being
    rebuilt. Derived from the guardrails rather than hardcoded, so it stays right
    if a cap moves.
    """
    vertical = get_vertical(vertical_name)
    viable = 1
    for step in range(1, vertical.max_steps + 1):
        prospects = assess(
            vertical=vertical,
            diagnosis=diagnosis,
            amount=amount,
            capacity=GuardrailCapacity(b2b_touches=step - 1, autopay_retries=step - 1),
            steps_taken=step - 1,
        )
        if prospects.action is None:
            break
        viable = step
    return max(viable, FIRST_OBSERVABLE_STEP)


def event_from(case: Case) -> RecoveryEvent:
    """The originating event, rebuilt so the replay decides on the same facts."""
    return RecoveryEvent(
        vertical=case.vertical,
        customer_id=f"{case.customer_id}_r",
        customer_ltv_tier=case.ltv_tier,
        amount=case.amount,
        currency=case.currency,
        raw_failure_reason=case.raw_failure_reason,
        vertical_metadata=dict(case.vertical_metadata or {}),
        timestamp=case.created_at,
    )


def replay(agent: RecoveryAgent, event: RecoveryEvent, collect_on_step: int) -> tuple[str, int]:
    """Work one case through to `collect_on_step`, then let the payment land."""
    original = actions_module.lands
    actions_module.lands = (
        lambda probability, step_number=actions_module.FIRST_OBSERVABLE_STEP: (
            step_number >= collect_on_step and probability > 0
        )
    )
    try:
        with session_scope() as session:
            case, _decision, _ = agent.handle_event(session, event)
            case_id = case.id
        for _ in range(collect_on_step - 1):
            time.sleep(SECONDS_BETWEEN_DECISIONS)
            with session_scope() as session:
                case = CaseStore(session).get(case_id)
                if case is None or case.status in {
                    CaseStatus.resolved.value,
                    CaseStatus.escalated.value,
                    CaseStatus.abandoned.value,
                }:
                    break
                agent.advance_case(session, case)
        with session_scope() as session:
            case = CaseStore(session).get(case_id)
            return case.status, case.step_count
    finally:
        actions_module.lands = original


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    args = parser.parse_args()

    with session_scope() as session:
        targets = [
            {
                "id": c.id,
                "vertical": c.vertical,
                "amount": c.amount,
                "steps": c.step_count,
                "event": event_from(c),
                # Land on the last step that can still collect, so the rebuilt
                # history shows the workflow being worked rather than a lucky
                # first touch — and still ends resolved.
                "collect_on": last_collectable_step(c.vertical, c.diagnosis, c.amount),
            }
            for c in thin_cases(session)
        ]

    if not targets:
        print("nothing to rebuild")
        return

    print(f"{len(targets)} case(s) with a history shorter than {THIN_HISTORY_STEPS} steps:")
    for t in targets:
        print(f"  {t['id']}  {t['vertical']:<8} Rs{t['amount']:>10,.2f}  "
              f"{t['steps']} step -> replay to {t['collect_on']}")
    if args.dry_run:
        return

    agent = RecoveryAgent()
    for index, t in enumerate(targets, start=1):
        print(f"\n[{index}/{len(targets)}] replaying {t['id']} ({t['vertical']})", flush=True)
        try:
            status, steps = replay(agent, t["event"], t["collect_on"])
        except Exception as exc:  # a failed replay must not destroy the original
            print(f"    replay failed ({type(exc).__name__}); leaving the original in place")
            continue

        print(f"    -> {status} after {steps} step(s)", flush=True)
        if steps < THIN_HISTORY_STEPS:
            print("    replacement is no better than the original; keeping both out of the way")
            continue

        with session_scope() as session:
            session.query(CaseStep).filter(CaseStep.case_id == t["id"]).delete()
            session.query(Case).filter(Case.id == t["id"]).delete()
        print("    removed the original single-step record")

        if index < len(targets):
            time.sleep(SECONDS_BETWEEN_DECISIONS)

    print("\ndone")


if __name__ == "__main__":
    main()
