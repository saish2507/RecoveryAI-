"""Undeliverable intents, and where they go instead of nowhere.

A dispatch that fails silently is a decision the system made, believes it acted
on, and did not act on — the case moves along, the money goes unworked, and the
only record is a warning nobody greps for. The retry covers the blip; the
dead-letter record covers everything the retry does not.

`httpx.post` is monkeypatched rather than served by a real socket: what is under
test is our retry and dead-letter logic, not httpx's.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import make_event
from recoveryai.core.execution import (
    DatabaseDeadLetterSink,
    NullDeadLetterSink,
    WebhookExecutor,
)
from recoveryai.core.models import ActionIntent, ActionStatus, Vertical
from recoveryai.db.models import FailedDelivery


class Recorder:
    """A dead-letter sink that keeps its records in memory."""

    name = "recorder"

    def __init__(self) -> None:
        self.records: list[tuple[ActionIntent, str, int]] = []

    def record(self, intent: ActionIntent, error: str, attempts: int) -> bool:
        self.records.append((intent, error, attempts))
        return True


def intent(action: str = "send_nudge") -> ActionIntent:
    return ActionIntent(
        case_id="case_deadletter",
        step_number=1,
        vertical=Vertical.cart,
        customer_id="cust_1",
        amount=2_500.0,
        proposed_action=action,
        final_action=action,
        params={"message_body": "your payment did not go through"},
        confidence=0.7,
    )


def executor(sink=None, **kwargs) -> WebhookExecutor:
    return WebhookExecutor(
        url="https://host.example/actions",
        dead_letters=sink or Recorder(),
        # Real backoff is 500ms; no test should pay it to prove a retry fired.
        backoff_seconds=0.0,
        **kwargs,
    )


def responder(monkeypatch, *outcomes):
    """Queue per-attempt outcomes: an int status code, or an exception to raise."""
    calls: list[int] = []
    queue = list(outcomes)

    def fake_post(url, **kwargs):  # noqa: ANN001, ARG001
        calls.append(1)
        outcome = queue.pop(0) if queue else queue_default
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"status": "executed", "details": "done"})

    queue_default = outcomes[-1] if outcomes else 200
    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


# ── Retry ──────────────────────────────────────────────────────────


def test_a_dropped_connection_is_retried_once_and_then_succeeds(monkeypatch) -> None:
    calls = responder(monkeypatch, httpx.ConnectError("connection refused"), 200)
    sink = Recorder()

    result = executor(sink).execute(intent(), make_event("cart", 2_500))

    assert result.status is ActionStatus.executed
    assert len(calls) == 2
    assert sink.records == []  # nothing was lost, so nothing to dead-letter


def test_a_503_is_retried(monkeypatch) -> None:
    """Providers signal overload by status far more often than by exception."""
    calls = responder(monkeypatch, 503, 200)

    result = executor().execute(intent(), make_event("cart", 2_500))

    assert result.status is ActionStatus.executed
    assert len(calls) == 2


def test_a_400_is_not_retried(monkeypatch) -> None:
    """The host is saying the request is wrong; it will be as wrong next time."""
    calls = responder(monkeypatch, 400, 400)
    sink = Recorder()

    result = executor(sink).execute(intent(), make_event("cart", 2_500))

    assert result.status is ActionStatus.error
    assert len(calls) == 1
    # Still dead-lettered: an intent the host will never accept is one nobody
    # is working, which is the fact worth surfacing.
    assert len(sink.records) == 1


# ── Dead letters ───────────────────────────────────────────────────


def test_a_persistently_failing_host_produces_a_dead_letter(monkeypatch) -> None:
    calls = responder(monkeypatch, httpx.ConnectError("down"), httpx.ConnectError("still down"))
    sink = Recorder()

    result = executor(sink).execute(intent(), make_event("cart", 2_500))

    assert result.status is ActionStatus.error
    assert len(calls) == 2
    assert len(sink.records) == 1

    recorded_intent, error, attempts = sink.records[0]
    assert recorded_intent.case_id == "case_deadletter"
    assert attempts == 2
    assert "ConnectError" in error


def test_the_dead_letter_row_carries_enough_to_replay_the_delivery(
    session, db, monkeypatch
) -> None:
    """A record you cannot act on is a slower way of dropping the intent."""
    responder(monkeypatch, httpx.ConnectError("down"), httpx.ConnectError("still down"))
    sent = intent()

    executor(DatabaseDeadLetterSink(db.session_scope)).execute(sent, make_event("cart", 2_500))

    row = session.query(FailedDelivery).one()
    assert row.case_id == "case_deadletter"
    assert row.intent_id == str(sent.intent_id)
    assert row.action == "send_nudge"
    assert row.attempts == 2
    assert "ConnectError" in row.error
    # The whole intent, so a replay does not have to rebuild it from a case
    # that has since moved on.
    assert row.action_intent["final_action"] == "send_nudge"
    assert row.action_intent["params"]["message_body"]


def test_a_broken_sink_cannot_take_the_decision_down_with_it(monkeypatch) -> None:
    """The sink exists to stop a loss; it must not become one."""

    class Exploding:
        name = "exploding"

        def record(self, intent, error, attempts):  # noqa: ANN001, ARG002
            raise RuntimeError("the dead-letter table is on fire")

    responder(monkeypatch, httpx.ConnectError("down"), httpx.ConnectError("still down"))

    with pytest.raises(RuntimeError):
        # The protocol says sinks do not raise, and this one violates it — the
        # database sink below is the one that has to survive its own failures.
        executor(Exploding()).execute(intent(), make_event("cart", 2_500))


def test_the_database_sink_swallows_its_own_failures(monkeypatch) -> None:
    def broken_scope():
        raise RuntimeError("no database")

    sink = DatabaseDeadLetterSink(broken_scope)
    assert sink.record(intent(), "boom", 2) is False


def test_the_default_sink_is_a_no_op_that_still_shouts(caplog) -> None:
    """An embedding host keeps its own storage, but a dropped intent is revenue
    nobody is working, so the default is loud rather than silent."""
    with caplog.at_level("ERROR"):
        assert NullDeadLetterSink().record(intent(), "host unreachable", 2) is False

    assert any(record.levelname == "ERROR" for record in caplog.records)
