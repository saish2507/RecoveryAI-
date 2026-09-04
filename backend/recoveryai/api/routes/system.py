"""Health, status and dashboard metrics.

`/health` is for load balancers: cheap, unauthenticated, no database.
`/api/v1/system/status` is for operators: LLM budget headroom, scheduler health,
security posture. Rate-governance state is genuinely operational information —
"the agent stopped using the model at 14:00" is answered here, not by reading
logs.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from recoveryai.api.deps import Agent, AppSettings, DbSession, Scheduler, get_broadcaster
from recoveryai.api.security import require_api_key, warn_if_unsecured
from recoveryai.core.anomalies import detect_anomalies
from recoveryai.core.cases import TERMINAL_STATUSES
from recoveryai.core.models import LLMToggle
from recoveryai.core.shadow_eval import evaluate_shadow_decisions
from recoveryai.db.models import Case, CaseStep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

#: Endpoints that must answer without credentials.
#:
#: Separate router rather than an exemption inside the authenticated one,
#: because "which routes are public" should be a list you can read rather than a
#: condition you have to evaluate. Exactly one route belongs here.
public_router = APIRouter(tags=["system"])


@public_router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    """Deliberately unauthenticated: a load balancer has no API key, and a
    health check that can fail closed on an auth misconfiguration would take a
    healthy service out of rotation. Returns no data about any case."""
    return {"status": "ok"}


@router.get(
    "/api/v1/system/status",
    summary="Operational status: LLM governance, scheduler, security posture",
    description="""
Everything an operator needs to answer "is it working, and is it safe".

`llm` reports remaining rate-limit and daily-budget headroom — when those hit
zero the agent keeps running on its deterministic policy tables, so a drop in
`llm` usage is graceful degradation, not an outage. `configured` (a key exists)
and `enabled` (the runtime switch) are reported separately because they are the
same symptom with different fixes; `available` is the conjunction the agent acts on.

`security.warnings` is populated when auth or signature verification is disabled.
A non-empty list in production is a misconfiguration.
""",
)
def system_status(
    request: Request, settings: AppSettings, agent: Agent, scheduler: Scheduler
) -> dict[str, Any]:
    return {
        "status": "ok",
        "agent": agent.status_snapshot(),
        "scheduler": scheduler.snapshot(),
        "websocket": {"connections": get_broadcaster(request).connection_count},
        "security": {
            "api_key_auth_enabled": settings.auth_enabled,
            "webhook_signature_verification_enabled": bool(settings.webhook_signing_secret),
            "cors_origins": settings.cors_origins,
            "warnings": warn_if_unsecured(settings),
        },
    }


@router.post(
    "/api/v1/system/llm",
    dependencies=[Depends(require_api_key)],
    summary="Switch model consultation on or off at runtime",
    description="""
Turns the LLM off without touching the key or restarting the process.

Off, every case is decided by the rule engine and policy tables — the same path
the agent already takes when the daily budget runs out, so this changes who
decides, not what the system is capable of. Events keep arriving and cases keep
progressing either way.

The switch lives in memory: a restart returns to whatever `GEMINI_API_KEY`
implies. It is a spending control, not configuration.
""",
)
def set_llm_enabled(payload: LLMToggle, agent: Agent) -> dict[str, Any]:
    agent.llm.enabled = payload.enabled
    logger.info("llm switched %s", "on" if payload.enabled else "off")
    return agent.llm.snapshot()


@router.get(
    "/api/v1/system/metrics",
    summary="Aggregate recovery metrics for the dashboard",
    description="""
Portfolio-level view: how much revenue is at risk, how much was recovered, what
it cost, and how the agent is deciding.

`decision_sources` is the honest breakdown of *who decided* — `rule` versus
`llm` versus the deterministic fallbacks. A healthy system shows most traffic
resolved by rules at zero cost, with the model spent on genuinely ambiguous
cases.

Recovery figures come from the simulated executor unless a host is reporting real
outcomes; see LIMITATIONS.md.
""",
)
def system_metrics(session: DbSession) -> dict[str, Any]:
    totals = session.execute(
        select(
            func.count(Case.id),
            func.coalesce(func.sum(Case.amount), 0.0),
            func.coalesce(func.sum(Case.amount_recovered), 0.0),
            func.coalesce(func.sum(Case.cost_of_recovery), 0.0),
        )
    ).one()
    case_count, at_risk, recovered, cost = totals

    by_status = dict(
        session.execute(select(Case.status, func.count(Case.id)).group_by(Case.status)).all()
    )
    by_vertical = dict(
        session.execute(
            select(Case.vertical, func.count(Case.id)).group_by(Case.vertical)
        ).all()
    )
    by_source = dict(
        session.execute(
            select(CaseStep.decision_source, func.count(CaseStep.id)).group_by(CaseStep.decision_source)
        ).all()
    )
    by_action = dict(
        session.execute(
            select(CaseStep.final_action, func.count(CaseStep.id)).group_by(CaseStep.final_action)
        ).all()
    )

    blocked = (
        session.scalar(select(func.count(CaseStep.id)).where(CaseStep.guardrail_verdict == "blocked")) or 0
    )
    steps = session.scalar(select(func.count(CaseStep.id))) or 0
    llm_steps = session.scalar(select(func.count(CaseStep.id)).where(CaseStep.llm_call_made.is_(True))) or 0
    open_cases = (
        session.scalar(select(func.count(Case.id)).where(Case.status.notin_(list(TERMINAL_STATUSES)))) or 0
    )

    return {
        "cases": {
            "total": case_count,
            "open": open_cases,
            "by_status": by_status,
            "by_vertical": by_vertical,
        },
        "revenue": {
            "at_risk": round(float(at_risk), 2),
            "recovered": round(float(recovered), 2),
            "cost_of_recovery": round(float(cost), 2),
            "net_recovered": round(float(recovered) - float(cost), 2),
            "recovery_rate": round(float(recovered) / float(at_risk), 4) if at_risk else 0.0,
        },
        "decisions": {
            "steps": steps,
            "by_source": by_source,
            "by_final_action": by_action,
            "guardrail_blocks": blocked,
            # The number that shows routing is working: what fraction of
            # decisions actually cost a model call.
            "llm_call_share": round(llm_steps / steps, 4) if steps else 0.0,
        },
    }


@router.get(
    "/api/v1/system/shadow-evaluation",
    summary="Did the shadow run's decisions match what actually happened?",
    description="""
The other half of a shadow pilot. `AGENT_MODE=shadow` records what the agent
*would* have done; this compares those decisions against the outcomes your own
process reported through `POST /api/v1/actions/{intent_id}/report`.

Agreement is measured on the agent's judgement about **recoverability**, not on
action names — the outcome report does not carry which action you took, and
mapping yours onto ours would invent a precision that is not there:

* agent would have worked the case, money arrived → `agreed_worked`
* agent would have escalated or stopped, none arrived → `agreed_gave_up`
* agent would have worked it, nothing arrived → `acted_but_nothing_recovered`
* agent would have stopped, money arrived anyway → `gave_up_but_money_arrived`

That last one is the expensive direction: revenue the agent would have left
alone. `agreement_rate` is `null` until at least one outcome has been reported.

Read-only, and shadow steps with no outcome yet are counted as
`awaiting_outcome` rather than as disagreements.
""",
)
def shadow_evaluation(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=1000, description="Most recent shadow steps to score.")] = 200,
) -> dict[str, Any]:
    return evaluate_shadow_decisions(session, limit=limit).as_dict()


@router.get(
    "/api/v1/system/anomalies",
    summary="Population-level signals that spiked recently",
    description="""
What changed shape in the last hour, judged against the preceding day.

The rest of the API answers questions about one case. This answers the question
no per-case view can: forty correctly-diagnosed `mandate_broken` cases are forty
good decisions and one missed incident, and only a population view sees the
second thing.

Ordered by amount at risk, not by how extreme the ratio is — a 12× spike in ₹200
carts matters less than a 3× spike in ₹40,000 invoices.

Read-only: detecting an anomaly never changes a case or triggers an action.
""",
)
def system_anomalies(
    session: DbSession,
    window_minutes: Annotated[int, Query(ge=5, le=1440, description="Size of the recent window.")] = 60,
    baseline_hours: Annotated[
        int, Query(ge=1, le=168, description="Trailing period to compare against.")
    ] = 24,
) -> dict[str, Any]:
    found = detect_anomalies(session, window_minutes=window_minutes, baseline_hours=baseline_hours)
    return {
        "window_minutes": window_minutes,
        "baseline_hours": baseline_hours,
        "count": len(found),
        "highest_severity": found[0].severity if found else None,
        "anomalies": [a.as_dict() for a in found],
    }
