"""Async outcome reporting — the return half of the webhook integration.

`WebhookExecutor` hands an intent to the host. Most hosts cannot say whether a
payment succeeded inside that HTTP request — the customer has not acted yet — so
they answer `202` and report the real outcome here, minutes or days later.

Without this endpoint the recovery numbers would be built entirely from the
simulator's assumptions, and "how much did we actually win back" would be an
estimate dressed as a measurement.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, status

from recoveryai.api.deps import DbSession, get_broadcaster
from recoveryai.api.errors import APIError, error_response
from recoveryai.api.security import require_api_key
from recoveryai.core.cases import CaseStore, ConcurrentModificationError
from recoveryai.core.models import ActionReport, CaseStepView

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["execution"])


@router.post(
    "/actions/{intent_id}/report",
    dependencies=[Depends(require_api_key)],
    summary="Report the real outcome of an action you executed",
    responses={
        404: error_response("No step matches that intent id."),
        409: error_response("The case was modified concurrently; retry the report."),
    },
    description="""
Call this after carrying out an `ActionIntent` you received at your
`ACTION_WEBHOOK_URL`.

The `intent_id` is the `intent.intent_id` from that payload, also sent as the
`X-RecoveryAI-Intent-Id` header. Reporting a non-zero `recovered_amount` marks
the case resolved.

**If the action failed after you accepted it**, report
`status="execution_failed"`. We recorded the step as executed on your `2xx`, so
without this the case sits on a success that never happened: the step moves to
`execution_failed`, any cost and recovery banked against it are unwound, and the
case returns to open work for the agent's next step.

Safe to call more than once: the report replaces the step's previous figures
rather than adding to them, so a retry cannot double-count a recovery.
""",
)
async def report_action_outcome(
    request: Request,
    session: DbSession,
    report: ActionReport,
    intent_id: Annotated[str, Path(description="The `intent_id` from the dispatched ActionIntent.")],
) -> CaseStepView:
    def apply() -> Any:
        return CaseStore(session).apply_host_report(
            intent_id=intent_id,
            status=report.status,
            details=report.details,
            cost=report.cost,
            recovered_amount=report.recovered_amount,
        )

    try:
        step = apply()
    except ConcurrentModificationError:
        # The scheduler advanced this case mid-report. Retry once against fresh
        # state rather than returning an error: this is the *outcome* of real
        # money moving, and the host has no obligation to call us again.
        logger.info("outcome report hit a concurrent write; retrying", extra={"intent_id": intent_id})
        session.rollback()
        try:
            step = apply()
        except ConcurrentModificationError as exc:
            raise APIError(
                status.HTTP_409_CONFLICT,
                "concurrent_modification",
                "This case is being modified concurrently. Retry the report.",
            ) from exc

    if step is None:
        raise APIError(
            status.HTTP_404_NOT_FOUND,
            "intent_not_found",
            f"No dispatched action with intent id {intent_id!r}.",
        )

    logger.info(
        "host reported action outcome",
        extra={
            "intent_id": intent_id,
            "case_id": step.case_id,
            "status": report.status,
            "recovered_amount": report.recovered_amount,
        },
    )
    await get_broadcaster(request).publish(
        "case.updated", {"case_id": step.case_id, "source": "host_report", "status": report.status}
    )
    return CaseStepView.model_validate(step)
