"""Case queue, case detail and the human review queue.

Both list endpoints are priority-ordered rather than FIFO, and paginated —
`GET /cases` with no bound is a denial-of-service waiting for the first busy day.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import func, select

from recoveryai.api.deps import DbSession
from recoveryai.api.errors import APIError, error_response
from recoveryai.core.cases import TERMINAL_STATUSES
from recoveryai.core.models import CaseDetailView, CaseStepView, CaseView, Page
from recoveryai.db.models import Case

router = APIRouter(prefix="/api/v1", tags=["cases"])

CaseId = Annotated[str, Path(description="Case identifier, e.g. `case_9f2c1a...`")]


class CasePage(Page):
    items: list[CaseView]


@router.get(
    "/cases",
    summary="List cases, highest priority first",
    description="""
Ordered by `priority_score` descending, never by arrival time.

The score carries two meanings depending on whether a decision exists yet:

* **Undecided** (`status=new`) — `amount × urgency`, so the largest at-risk
  revenue is worked first when there is more open work than throughput.
* **Decided** — `(1 − confidence) × amount`, the human-review ordering.

Filter by `status` to get one or the other unambiguously.
""",
)
def list_cases(
    session: DbSession,
    status_filter: Annotated[
        str | None, Query(alias="status", description="Filter by lifecycle status.")
    ] = None,
    vertical: Annotated[str | None, Query(description="cart | b2b | autopay")] = None,
    open_only: Annotated[bool, Query(description="Exclude resolved/escalated/abandoned cases.")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CasePage:
    filters = []
    if status_filter:
        filters.append(Case.status == status_filter)
    if vertical:
        filters.append(Case.vertical == vertical)
    if open_only:
        filters.append(Case.status.notin_(list(TERMINAL_STATUSES)))

    total = session.scalar(select(func.count(Case.id)).where(*filters)) or 0
    rows = session.scalars(
        select(Case)
        .where(*filters)
        .order_by(Case.priority_score.desc(), Case.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return CasePage(
        items=[CaseView.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/cases/{case_id}",
    summary="Case detail with its full decision trace",
    responses={404: error_response("No such case.")},
    description="""
Returns the case plus every step taken on it, in order. Each step records what
the agent proposed, what the guardrail said, what actually happened, and the
exact context snapshot the decision was made from — enough to reconstruct any
decision months later.
""",
)
def get_case(session: DbSession, case_id: CaseId) -> CaseDetailView:
    case = session.get(Case, case_id)
    if case is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "case_not_found", f"No case with id {case_id!r}.")

    detail = CaseDetailView.model_validate(case)
    detail.steps = [CaseStepView.model_validate(step) for step in case.steps]
    return detail


@router.get(
    "/review/queue",
    tags=["review"],
    summary="Human review queue, ordered by uncertainty × value",
    description="""
Cases needing a person, ordered by `(1 − confidence) × amount`.

That ordering is the point: a ₹40,000 case the agent was genuinely unsure about
outranks a ₹200 case that escalated only because it hit a guardrail ceiling.
Reviewer attention is the scarcest resource in the system, so it is spent where
uncertainty is most expensive rather than on whatever arrived first.
""",
)
def review_queue(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include: Annotated[
        Literal["escalated", "all_decided"],
        Query(description="`escalated` (default) or `all_decided` to include in-progress cases."),
    ] = "escalated",
) -> CasePage:
    statuses = ["escalated"] if include == "escalated" else ["escalated", "in_progress"]
    filters = [Case.status.in_(statuses), Case.latest_confidence.is_not(None)]

    total = session.scalar(select(func.count(Case.id)).where(*filters)) or 0
    rows = session.scalars(
        select(Case)
        .where(*filters)
        .order_by(Case.priority_score.desc(), Case.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return CasePage(
        items=[CaseView.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )
