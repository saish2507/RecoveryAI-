"""Dev tools — manual injection and simulator control.

Demoted from the previous build's centrepiece to a utility drawer. A manual
injector as the main UI implies a system that needs a human to feed it; the real
product surface is the case queue and the trace timeline, and this is here to
drive a demo and to let an integrator poke the agent without writing a client.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from recoveryai.api.deps import Agent, DbSession, Scheduler, get_broadcaster
from recoveryai.api.errors import APIError, error_response
from recoveryai.api.security import require_api_key
from recoveryai.core.models import CaseView, Vertical
from recoveryai.simulator import SCENARIOS, build_scenario, generate_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dev", tags=["dev"], dependencies=[Depends(require_api_key)])


@router.get("/scenarios", summary="List the named demo scenarios")
def list_scenarios() -> dict[str, str]:
    return {name: spec["description"] for name, spec in SCENARIOS.items()}


@router.post(
    "/inject",
    status_code=status.HTTP_201_CREATED,
    summary="Inject one event and get an immediate decision",
    description="""
Generates an event and runs it through the full agent path — same code as a real
webhook, no shortcuts.

Pass `scenario` for a deterministic fixture (see `/dev/scenarios`), or `vertical`
for a random realistic one. Injecting the same scenario repeatedly accumulates
guardrail state against a stable demo customer, which is how you show a guardrail
actually blocking rather than describing one.
""",
)
async def inject_event(
    request: Request,
    session: DbSession,
    agent: Agent,
    scenario: Annotated[str | None, Query(description="A name from /dev/scenarios.")] = None,
    vertical: Annotated[Vertical | None, Query(description="Random event in this vertical.")] = None,
    customer_id: Annotated[str | None, Query(description="Override the customer id.")] = None,
) -> CaseView:
    if scenario:
        try:
            event = build_scenario(scenario, customer_id)
        except ValueError as exc:
            raise APIError(status.HTTP_400_BAD_REQUEST, "unknown_scenario", str(exc)) from exc
    else:
        event = generate_event(vertical, customer_id)

    case, decision, _created = agent.handle_event(session, event)
    session.flush()

    await get_broadcaster(request).publish(
        "case.created",
        {
            "case_id": case.id,
            "vertical": case.vertical,
            "amount": case.amount,
            "final_action": decision.intent.final_action if decision else None,
            "source": "dev_inject",
        },
    )
    return CaseView.model_validate(case)


@router.post(
    "/followups/run",
    summary="Run the follow-up sweep immediately",
    description="Forces a re-evaluation pass instead of waiting for the next scheduled tick. "
    "Useful for stepping a case through its lifecycle during a demo.",
)
def run_followups_now(scheduler: Scheduler) -> dict[str, object]:
    decisions = scheduler.run_due_followups()
    return {
        "advanced": len(decisions),
        "cases": [
            {
                "case_id": d.case_id,
                "step": d.step_number,
                "proposed_action": d.intent.proposed_action,
                "final_action": d.intent.final_action,
                "guardrail_verdict": d.intent.guardrail_verdict.value,
                "status": d.case_status,
            }
            for d in decisions
        ],
    }


@router.post(
    "/cases/{case_id}/advance",
    summary="Force one more decision step on a case",
    responses={
        404: error_response("No such case."),
        409: error_response("Case is already closed."),
    },
)
async def advance_case(request: Request, session: DbSession, agent: Agent, case_id: str) -> dict[str, object]:
    from recoveryai.core.cases import CaseStore

    case = CaseStore(session).get(case_id)
    if case is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "case_not_found", f"No case with id {case_id!r}.")

    decision = agent.advance_case(session, case)
    if decision is None:
        raise APIError(
            status.HTTP_409_CONFLICT,
            "case_closed",
            f"Case {case_id} is {case.status}; no further steps will be taken.",
        )

    session.flush()
    await get_broadcaster(request).publish(
        "case.updated", {"case_id": case.id, "step": decision.step_number, "source": "dev_advance"}
    )
    return {
        "case_id": case.id,
        "step": decision.step_number,
        "routing": decision.routing.model_dump(),
        "proposed_action": decision.intent.proposed_action,
        "final_action": decision.intent.final_action,
        "guardrail_verdict": decision.intent.guardrail_verdict.value,
        "guardrail_reason": decision.intent.guardrail_reason,
        "reasoning": decision.intent.reasoning,
        "confidence": decision.intent.confidence,
        "status": decision.case_status,
    }
