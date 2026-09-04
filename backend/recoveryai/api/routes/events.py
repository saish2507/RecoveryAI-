"""Event ingestion — the integration front door.

This endpoint is a public contract, and its OpenAPI entry doubles as the
integration document a host fintech's engineers will actually read. The body is a
declared `RecoveryEvent`, so the schema, its examples and its field descriptions
are all published rather than described in prose.

Signature verification happens *below* this route, in
`api.middleware.WebhookSignatureMiddleware`, so untrusted bytes are checked
before anything parses them.

The property that matters most here is idempotency. Real webhook senders retry on
timeout and on any ambiguous response — including requests we processed
successfully but answered too slowly. A redelivered event must return the
original case, not create a second one and chase the customer twice.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Header, Request, Response, status

from recoveryai.api.deps import Agent, DbSession, get_broadcaster
from recoveryai.api.errors import error_response
from recoveryai.api.security import require_api_key
from recoveryai.core.models import CaseView, RecoveryEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["ingestion"])


@router.post(
    "/events",
    response_model=CaseView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
    summary="Submit a revenue-at-risk event",
    response_description="The case created (201), or the existing one (200 on redelivery).",
    responses={
        200: {"description": "Duplicate delivery — the existing case is returned unchanged."},
        401: error_response("Missing API key, or an invalid webhook signature."),
        422: error_response("The payload did not match the RecoveryEvent schema."),
    },
    description="""
Submit a payment failure, abandoned checkout or overdue invoice for recovery. The
agent diagnoses it, decides a guardrail-checked action synchronously, and
schedules a follow-up re-evaluation.

**Idempotency.** Deduplicated by `event_id`, or by an `Idempotency-Key` header if
you send one. Redelivering an event returns `200` with the original case and does
no further work — safe to retry on any timeout or ambiguous response.

**Signature.** When `WEBHOOK_SIGNING_SECRET` is configured, send
`X-RecoveryAI-Signature: sha256=<hex>`, an HMAC-SHA256 over the exact request
body bytes.

**`raw_failure_reason` accepts any string.** Unrecognised gateway or bank codes
are routed to the agent as ambiguous signals rather than rejected, so you do not
need to map your codes onto ours before integrating. This is deliberate: an enum
here turns "a code we have not seen" into a dropped payment.
""",
)
async def ingest_event(
    request: Request,
    response: Response,
    session: DbSession,
    agent: Agent,
    event: RecoveryEvent = Body(...),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Overrides `event_id` for deduplication. Use your own order or invoice id.",
    ),
) -> CaseView:
    case, decision, created = agent.handle_event(session, event, idempotency_key=idempotency_key)
    session.flush()
    view = CaseView.model_validate(case)

    if not created:
        # 200 rather than 201: nothing was created, and a sender distinguishing
        # the two can tell a retry succeeded from a first delivery.
        response.status_code = status.HTTP_200_OK
        logger.info("duplicate delivery", extra={"case_id": case.id})
        return view

    await get_broadcaster(request).publish(
        "case.created",
        {
            "case_id": case.id,
            "vertical": case.vertical,
            "amount": case.amount,
            "priority_score": case.priority_score,
            "final_action": decision.intent.final_action if decision else None,
            "guardrail_verdict": decision.intent.guardrail_verdict.value if decision else None,
        },
    )
    return view
