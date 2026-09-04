"""The execution seam — where "what we decided" stops and "how it happens" starts.

This is the file that makes "drop the frontend and embed the agent" literal
rather than aspirational. The agent produces an `ActionIntent` and hands it to an
`ActionExecutor`. It never sends an email, never charges a card, never knows how
either is done.

Three implementations ship:

* `SimulatedExecutor` — default. Runs the `actions.py` catalogue and tags every
  outcome `[SIMULATED]`. Zero external dependencies, so the demo and the whole
  test suite work offline.
* `WebhookExecutor` — POSTs the signed intent to the host's endpoint. The host
  either answers synchronously or reports later via
  `POST /api/v1/actions/{id}/report`.
* `ShadowExecutor` — wraps any executor and dispatches nothing. This is the
  honest answer to "how would we trust this before going live": run it against
  real traffic, read the decisions it *would* have made, then flip the switch.

A Python host that wants neither can implement the protocol themselves and
inject it — `RecoveryAgent(executor=MyExecutor())`. No fork.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, runtime_checkable

from recoveryai.core.actions import get_action
from recoveryai.core.economics import success_rate
from recoveryai.core.models import ActionIntent, ActionStatus, ExecutionResult, RecoveryEvent
from recoveryai.core.signing import sign_payload

logger = logging.getLogger(__name__)

#: Extra delivery attempts after the first. One.
#:
#: This is a *transport* retry, not a recovery strategy: the case's own
#: follow-up loop already re-decides later, so a delivery that cannot get
#: through in two tries is better dead-lettered than retried into a queue.
WEBHOOK_MAX_RETRIES = 1

#: Pause before the retry, in seconds.
WEBHOOK_RETRY_BACKOFF_SECONDS = 0.5

#: HTTP statuses worth a second attempt. A 4xx is the host telling us the
#: request is wrong, and sending it again unchanged is just noise.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


@runtime_checkable
class DeadLetterSink(Protocol):
    """Where an undeliverable intent goes instead of nowhere.

    Same shape as the notifier and executor seams: a protocol, a no-op default,
    and a real implementation injected rather than imported.
    """

    name: str

    def record(self, intent: ActionIntent, error: str, attempts: int) -> bool:
        """Returns whether anything was persisted. Must not raise."""
        ...


class NullDeadLetterSink:
    """Default. Logs the loss and stores nothing.

    Deliberately the default so an embedding host that brought its own storage
    is not forced into our tables — but it logs at ERROR, because a dropped
    intent is real revenue nobody is working.
    """

    name = "none"

    def record(self, intent: ActionIntent, error: str, attempts: int) -> bool:
        logger.error(
            "action intent undeliverable and not persisted (no dead-letter sink configured)",
            extra={
                "case_id": intent.case_id,
                "intent_id": str(intent.intent_id),
                "action": intent.final_action,
                "attempts": attempts,
                "error": error,
            },
        )
        return False


class DatabaseDeadLetterSink:
    """Writes the failed intent to `failed_deliveries` so it can be replayed.

    The whole intent is stored as JSON rather than a foreign key onto the step:
    dispatch can fail before a step row exists, and a dead-letter record that
    cannot be written because its parent is missing defeats the point.
    """

    name = "database"

    def __init__(self, session_scope: Any) -> None:
        self.session_scope = session_scope

    def record(self, intent: ActionIntent, error: str, attempts: int) -> bool:
        # Lazy: keeps `core.execution` importable without the ORM configured,
        # which the executor protocol's other implementations rely on.
        from recoveryai.db.models import FailedDelivery  # noqa: PLC0415

        try:
            with self.session_scope() as session:
                session.add(
                    FailedDelivery(
                        case_id=intent.case_id,
                        intent_id=str(intent.intent_id),
                        action=intent.final_action,
                        action_intent=json.loads(intent.model_dump_json()),
                        error=error,
                        attempts=attempts,
                    )
                )
            return True
        except Exception:
            # A sink that raises would turn "the action failed" into "the whole
            # decision was lost", which is the outcome this exists to prevent.
            logger.exception(
                "failed to dead-letter an undeliverable intent",
                extra={"intent_id": str(intent.intent_id)},
            )
            return False


@runtime_checkable
class ActionExecutor(Protocol):
    """Carry out a decided intent and report what happened.

    Implementations must not raise. A failure is an `ExecutionResult` with
    `status=error` — an exception here would lose the decision *and* its audit
    trail, which is strictly worse than an action that failed loudly.
    """

    name: str

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult: ...


class SimulatedExecutor:
    """Runs the built-in action catalogue. Recovery figures are illustrative.

    Odds come from `economics.success_rate`, keyed on the action *and* the
    diagnosis, so a simulated run reproduces the fact that a retry against a
    revoked mandate never succeeds. A flat per-action rate would have this
    executor cheerfully collect on cases the real world cannot.
    """

    name = "simulated"

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        action = get_action(intent.final_action)
        if action is None:
            # Only reachable if a caller hand-built an intent; the agent itself
            # validates against the palette before ever getting here.
            return ExecutionResult(
                status=ActionStatus.error,
                details=f"unknown action {intent.final_action!r}",
                executor=self.name,
            )
        outcome = action.simulate(
            event,
            intent.params,
            step_number=intent.step_number,
            probability=success_rate(intent.final_action, intent.diagnosis),
        )
        return ExecutionResult(
            status=ActionStatus(outcome["status"]),
            details=outcome["details"],
            cost=outcome["cost"],
            recovered_amount=outcome["recovered_amount"],
            executor=self.name,
        )


class WebhookExecutor:
    """Hands the intent to the host system over HTTP.

    The host's response is treated as advisory: a 2xx with no usable body means
    "accepted, outcome to follow", which becomes `pending_host_execution` rather
    than a fabricated success. Reporting a recovery that may not have happened
    would corrupt the very numbers this system exists to produce.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        secret: str = "",
        timeout_seconds: float = 10.0,
        dead_letters: DeadLetterSink | None = None,
        max_retries: int = WEBHOOK_MAX_RETRIES,
        backoff_seconds: float = WEBHOOK_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.url = url
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.dead_letters = dead_letters or NullDeadLetterSink()
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def _payload(self, intent: ActionIntent, event: RecoveryEvent) -> bytes:
        body: dict[str, Any] = {
            "intent": json.loads(intent.model_dump_json()),
            "event": json.loads(event.model_dump_json()),
        }
        return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        if not self.url:
            return ExecutionResult(
                status=ActionStatus.error,
                details="ACTION_WEBHOOK_URL is not configured",
                executor=self.name,
            )

        body = self._payload(intent, event)
        headers = {"Content-Type": "application/json", "X-RecoveryAI-Intent-Id": str(intent.intent_id)}
        if self.secret:
            headers["X-RecoveryAI-Signature"] = sign_payload(self.secret, body)

        import httpx  # noqa: PLC0415 — lazy so core imports without httpx

        attempts = 0
        failure = ""

        while attempts <= self.max_retries:
            if attempts > 0:
                time.sleep(self.backoff_seconds)
            attempts += 1

            try:
                response = httpx.post(
                    self.url, content=body, headers=headers, timeout=self.timeout_seconds
                )
            except Exception as exc:
                failure = f"webhook dispatch failed: {type(exc).__name__}"
                logger.warning(
                    "action webhook failed",
                    extra={
                        "intent_id": str(intent.intent_id),
                        "error": type(exc).__name__,
                        "attempt": attempts,
                    },
                )
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                failure = f"host returned HTTP {response.status_code}"
                logger.warning(
                    "action webhook returned a retryable status",
                    extra={
                        "intent_id": str(intent.intent_id),
                        "status": response.status_code,
                        "attempt": attempts,
                    },
                )
                continue

            if response.status_code >= 400:
                # A non-retryable 4xx: the host is telling us this request is
                # wrong, and it will be exactly as wrong the second time. Still
                # dead-lettered — an intent the host will never accept is one
                # nobody is working, which is the thing worth surfacing.
                failure = f"host returned HTTP {response.status_code}"
                break

            return self._interpret(response)

        self.dead_letters.record(intent, failure, attempts)
        return ExecutionResult(
            status=ActionStatus.error,
            details=f"{failure} after {attempts} attempt(s); recorded as a failed delivery",
            executor=self.name,
        )

    def _interpret(self, response: Any) -> ExecutionResult:
        try:
            data = response.json()
        except Exception:
            data = None

        if not isinstance(data, dict) or "status" not in data:
            return ExecutionResult(
                status=ActionStatus.pending_host_execution,
                details="host accepted the intent; awaiting an outcome report",
                executor=self.name,
            )

        try:
            status = ActionStatus(str(data.get("status")))
        except ValueError:
            status = ActionStatus.pending_host_execution

        return ExecutionResult(
            status=status,
            details=str(data.get("details", "") or "reported by host"),
            cost=float(data.get("cost", 0.0) or 0.0),
            recovered_amount=float(data.get("recovered_amount", 0.0) or 0.0),
            executor=self.name,
        )


class ShadowExecutor:
    """Decorator that decides everything and dispatches nothing.

    Wrapping rather than replacing matters: the wrapped executor's identity is
    recorded, so the shadow log says what *would* have been used, not merely
    that something was suppressed.
    """

    name = "shadow"

    def __init__(self, wrapped: ActionExecutor | None = None) -> None:
        self.wrapped = wrapped

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        target = self.wrapped.name if self.wrapped else "none"
        return ExecutionResult(
            status=ActionStatus.shadow_logged,
            details=(
                f"[SHADOW] would have run {intent.final_action!r} via {target} "
                f"for {event.customer_id}; nothing was dispatched"
            ),
            cost=0.0,
            recovered_amount=0.0,
            executor=self.name,
        )


def build_executor(settings: Any) -> ActionExecutor:
    """Assemble the executor described by settings.

    Shadow mode is applied *outside* the chosen executor, so flipping
    `AGENT_MODE` never changes which executor was configured — it only stops it
    from firing.
    """
    if settings.action_executor == "webhook":
        from recoveryai.db.session import session_scope  # noqa: PLC0415 — lazy; see sink docstring

        base: ActionExecutor = WebhookExecutor(
            url=settings.action_webhook_url,
            secret=settings.webhook_signing_secret,
            timeout_seconds=settings.action_webhook_timeout_seconds,
            # Its own session, not the caller's: the dead-letter record has to
            # survive even when the surrounding transaction is being rolled back,
            # which is exactly the situation that produced it.
            dead_letters=DatabaseDeadLetterSink(session_scope),
        )
    elif settings.action_executor == "razorpay":
        from recoveryai.core.razorpay import RazorpayExecutor  # noqa: PLC0415 — optional dependency

        base = RazorpayExecutor(
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret,
            allow_live=settings.razorpay_allow_live,
            callback_url=settings.razorpay_callback_url,
            timeout_seconds=settings.razorpay_timeout_seconds,
        )
    else:
        base = SimulatedExecutor()

    if settings.shadow_mode:
        return ShadowExecutor(base)
    return base
