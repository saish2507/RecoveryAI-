"""Domain models — the vocabulary shared by the agent, the store and the API.

These are plain Pydantic models with no web-framework dependency, so a host
application can import them directly to build the webhook payload it sends us.

Design note on `raw_failure_reason`: it is a **plain `str`**, deliberately not an
enum. Real gateways emit arbitrary, undocumented and version-drifting codes; an
enum turns "a code we have not seen before" into a 422 at the ingestion boundary
and drops revenue on the floor. An unrecognised code is not an error — it is
exactly the ambiguous signal the agent exists to reason about, so it routes to
the LLM instead of raising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Force a timestamp to be timezone-aware UTC.

    SQLite has no native timestamp type, so SQLAlchemy hands back naive
    datetimes even for `DateTime(timezone=True)` columns. Serialised without an
    offset, those get parsed by the browser as *local* time — which is how a case
    created two seconds ago renders as "6h ago" in IST. Normalising at the
    Pydantic boundary fixes it for every consumer at once, rather than asking
    each client to guess.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


# ── Closed vocabularies ────────────────────────────────────────────
# Only fields whose value space *we* own are enums. Anything a third party
# controls stays a string.


class Vertical(str, Enum):
    cart = "cart"
    b2b = "b2b"
    autopay = "autopay"


class LTVTier(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CaseStatus(str, Enum):
    new = "new"
    in_progress = "in_progress"
    resolved = "resolved"
    escalated = "escalated"
    abandoned = "abandoned"


class DecisionSource(str, Enum):
    """How this step's proposed action was arrived at.

    Two values, because there are two ways a decision gets made: the model
    answered, or the model answered earlier and the answer was reused. There is
    no third option. When the model cannot answer, no step is recorded at all
    and the case is retried — so a stored decision is always the agent's.
    """

    llm = "llm"
    llm_cached = "llm_cached"


class GuardrailVerdict(str, Enum):
    allowed = "allowed"
    blocked = "blocked"


class ActionStatus(str, Enum):
    executed = "executed"
    blocked = "blocked"
    scheduled = "scheduled"
    escalated = "escalated"
    error = "error"
    shadow_logged = "shadow_logged"
    pending_host_execution = "pending_host_execution"
    # A host reported failure *after* we had already recorded the action as
    # done. Distinct from `error`, which means the dispatch itself failed:
    # here the dispatch succeeded and the execution did not, so any cost or
    # recovery banked on the optimistic reading has to be unwound.
    execution_failed = "execution_failed"


# ── Ingestion contract ─────────────────────────────────────────────


class RecoveryEvent(BaseModel):
    """The webhook payload contract. This is a public, stable API surface.

    A host fintech POSTs this to `/api/v1/events` (or constructs it in-process
    and hands it to `RecoveryAgent.handle_event`). Everything downstream is
    derived from it.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event_id": "3f1a8c22-0c6e-4f3f-9b3a-2c9a5f7e1d40",
                    "vertical": "cart",
                    "customer_id": "cust_10482",
                    "customer_ltv_tier": "high",
                    "amount": 1499.0,
                    "currency": "INR",
                    "raw_failure_reason": "GATEWAY_TIMEOUT_V2",
                    "vertical_metadata": {
                        "payment_gateway_error_code": None,
                        "session_duration_seconds": 95,
                        "price_vs_customer_avg": 1.2,
                    },
                }
            ]
        }
    )

    event_id: UUID = Field(
        default_factory=uuid4,
        description=(
            "Sender-supplied unique id, used for idempotent ingestion: redelivering the "
            "same event_id returns the original case instead of creating a second one. "
            "**Send it.** If omitted it defaults to a fresh UUID, which means a retry "
            "will be treated as a new event — supply this, or an `Idempotency-Key` "
            "header, if you retry on timeout."
        ),
    )
    vertical: Vertical = Field(description="Which recovery specialist handles this signal.")
    timestamp: datetime = Field(default_factory=utcnow)
    customer_id: str = Field(min_length=1, description="Stable customer identifier in the host system.")
    customer_ltv_tier: LTVTier
    amount: float = Field(gt=0, description="Revenue at risk, in `currency` units.")
    currency: str = Field(default="INR", min_length=3, max_length=3)
    raw_failure_reason: str = Field(
        default="",
        description=(
            "Verbatim gateway/bank failure code. Free-form on purpose: unrecognised "
            "codes are routed to the agent as ambiguous rather than rejected."
        ),
    )
    vertical_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Vertical-specific signals (session duration, days overdue, retry count, ...).",
    )

    @field_validator("vertical_metadata", mode="before")
    @classmethod
    def _ensure_dict(cls, v: Any) -> dict[str, Any]:
        return v or {}

    @field_validator("currency", mode="before")
    @classmethod
    def _upper_currency(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


# ── Agent decision types ───────────────────────────────────────────


class RoutingDecision(BaseModel):
    """Why this case went where it did, and what the rules thought before it did.

    `use_llm` is false only when the provider is unreachable — the record of a
    degradation, not of a cost decision. The rule diagnosis and confidence ride
    along because a reviewer comparing them against what the model chose is how
    you tell a model that is adding judgement from one that is agreeing with a
    lookup table at ten times the price.
    """

    use_llm: bool
    reason: str
    rule_diagnosis: str = Field(
        default="unknown",
        description="Cheap rule-based diagnosis, always computed. Used as the LLM's prior, "
        "or as the decision itself when the LLM is not consulted.",
    )
    rule_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ActionIntent(BaseModel):
    """A fully decided, guardrail-checked action that has **not** been carried out.

    This is the integration seam. The agent's job ends here; how the intent is
    carried out is the `ActionExecutor`'s problem, and in a real deployment that
    is the host's own notification/payment infrastructure.
    """

    intent_id: UUID = Field(default_factory=uuid4)
    case_id: str
    step_number: int
    vertical: Vertical
    customer_id: str
    amount: float
    currency: str = "INR"

    proposed_action: str = Field(description="What the decision-maker (LLM or rules) chose.")
    final_action: str = Field(description="What will actually happen, after guardrail interception.")
    params: dict[str, Any] = Field(default_factory=dict)
    diagnosis: str = Field(
        default="unknown",
        description=(
            "What the agent concluded is wrong with this payment. Carried on the "
            "intent because an action's odds depend on it — re-presenting a mandate "
            "recovers half of temporary balance shortfalls and none of the revoked "
            "ones — so an executor cannot reason about the attempt without it."
        ),
    )

    reasoning: str = ""
    #: `None` means "this decision produced no confidence at all" — a forced
    #: escalation, say — as opposed to "the decider was unsure", which is a low
    #: float. The review queue treats the two very differently; see
    #: `cases.effective_confidence`.
    confidence: float | None = Field(default=0.5, ge=0.0, le=1.0)
    decision_source: DecisionSource = DecisionSource.llm

    guardrail_verdict: GuardrailVerdict = GuardrailVerdict.allowed
    guardrail_reason: str | None = None
    #: Revision of the guardrail rules that produced `guardrail_verdict`. Stamped
    #: by the agent at decision time; `policy.GUARDRAIL_VERSION` is the source.
    #: Not defaulted from that constant here, because `policy` imports the action
    #: catalogue which imports this module.
    guardrail_version: str = ""

    created_at: datetime = Field(default_factory=utcnow)

    @property
    def was_redirected(self) -> bool:
        return self.proposed_action != self.final_action


class ExecutionResult(BaseModel):
    """What the executor reports back after (attempting to) carry out an intent."""

    status: ActionStatus
    details: str = ""
    cost: float = Field(default=0.0, ge=0.0)
    recovered_amount: float = Field(default=0.0, ge=0.0)
    executor: str = "simulated"


# ── Read models (what the API and the console see) ─────────────────


class CaseStepView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: str
    step_number: int
    decision_source: str
    # The handle a host quotes back when reporting the real outcome of this
    # action via POST /api/v1/actions/{intent_id}/report.
    intent_id: str
    proposed_action: str
    final_action: str
    action_params: dict[str, Any] = Field(default_factory=dict)
    reasoning: str
    confidence: float | None
    guardrail_verdict: str
    guardrail_reason: str | None
    action_status: str
    action_details: str
    cost: float
    recovered_amount: float
    llm_call_made: bool
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime


class CaseView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_id: str
    vertical: str
    customer_id: str
    ltv_tier: str
    amount: float
    currency: str
    status: str
    priority_score: float
    # What automation can still expect to collect, net of the next attempt's
    # cost. Drives how soon the agent returns to the case, so it belongs in the
    # read model beside the score that only orders a human's queue.
    expected_recovery: float = 0.0
    raw_failure_reason: str
    diagnosis: str
    vertical_metadata: dict[str, Any] = Field(default_factory=dict)
    amount_recovered: float
    cost_of_recovery: float
    step_count: int
    latest_confidence: float | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    next_followup_at: UtcDatetime | None = None


class CaseDetailView(CaseView):
    steps: list[CaseStepView] = Field(default_factory=list)


class Page(BaseModel):
    """Envelope for every list endpoint."""

    items: list[Any]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Every non-2xx response from the API has this shape."""

    error: ErrorBody


class ActionReport(BaseModel):
    """Async outcome report from a host that executed an intent on its own side."""

    status: Literal["executed", "scheduled", "error", "escalated", "execution_failed"]
    details: str = ""
    cost: float = Field(default=0.0, ge=0.0)
    recovered_amount: float = Field(default=0.0, ge=0.0)


class LLMToggle(BaseModel):
    """Flip model consultation on or off without restarting the process."""

    enabled: bool
