"""The Razorpay seam: real API shape, test-mode safety, and honest revenue.

No network. Every test substitutes the transport, because a test suite that
needs live credentials is a test suite nobody runs — and one that needs *live*
credentials is one that eventually charges somebody.
"""

from __future__ import annotations

import base64
import json

import pytest
from conftest import make_event
from recoveryai.core.actions import (
    ESCALATE_TO_HUMAN,
    OFFER_PARTIAL_PAYMENT_PLAN,
    RETRY_CHARGE,
    SEND_DISCOUNT,
    SEND_NUDGE,
)
from recoveryai.core.execution import ActionExecutor, build_executor
from recoveryai.core.models import ActionIntent, ActionStatus, Vertical
from recoveryai.core.razorpay import RazorpayError, RazorpayExecutor, _to_paise

TEST_KEY = "rzp_test_ABC123"
SECRET = "shhh"


def make_intent(action: str = SEND_NUDGE, amount: float = 1_000.0, **params) -> ActionIntent:
    return ActionIntent(
        case_id="case_x",
        step_number=1,
        vertical=Vertical.cart,
        customer_id="cust_1",
        amount=amount,
        proposed_action=action,
        final_action=action,
        params={"message_subject": "Your payment did not go through",
                "message_body": "Finish it whenever suits you.", **params},
    )


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "id": "plink_ExjpAUN3gVHrPJ",
            "short_url": "https://rzp.io/i/nxrHnLJ",
            "status": "created",
        }

    def json(self):
        return self._payload


def capture(monkeypatch, response: Response | None = None) -> dict:
    captured: dict = {}

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, body=json.loads(content), headers=headers)
        return response or Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def executor(**kwargs) -> RazorpayExecutor:
    return RazorpayExecutor(TEST_KEY, SECRET, **kwargs)


# ── Live-key safety ────────────────────────────────────────────────


def test_a_live_key_is_refused_by_default() -> None:
    """The failure guarded against: a production .env on a machine running the loop."""
    with pytest.raises(RazorpayError, match="refusing to use non-test"):
        RazorpayExecutor("rzp_live_REAL", SECRET)


def test_a_live_key_works_only_when_said_twice() -> None:
    assert RazorpayExecutor("rzp_live_REAL", SECRET, allow_live=True).test_mode is False


def test_a_test_key_needs_no_ceremony() -> None:
    assert executor().test_mode is True


# ── Protocol and wiring ────────────────────────────────────────────


def test_it_satisfies_the_executor_protocol() -> None:
    assert isinstance(executor(), ActionExecutor)


def test_settings_can_select_it(settings) -> None:
    settings.action_executor = "razorpay"
    settings.razorpay_key_id = TEST_KEY
    settings.razorpay_key_secret = SECRET
    assert build_executor(settings).name == "razorpay"


# ── Request shape ──────────────────────────────────────────────────


def test_it_calls_the_payment_links_endpoint_with_basic_auth(monkeypatch) -> None:
    captured = capture(monkeypatch)
    executor().execute(make_intent(), make_event("cart", 1_000))

    assert captured["url"] == "https://api.razorpay.com/v1/payment_links"
    expected = base64.b64encode(f"{TEST_KEY}:{SECRET}".encode()).decode()
    assert captured["headers"]["Authorization"] == f"Basic {expected}"


def test_the_amount_is_sent_in_paise(monkeypatch) -> None:
    captured = capture(monkeypatch)
    executor().execute(make_intent(amount=1_450.55), make_event("autopay", 1_450.55))
    assert captured["body"]["amount"] == 145_055


def test_paise_conversion_survives_binary_floats() -> None:
    """int(18.5 * 100) is 1849. Under-charging by a paise per link is a real bug."""
    assert _to_paise(18.5) == 1850
    assert _to_paise(1_450.55) == 145_055


def test_the_drafted_copy_becomes_the_link_description(monkeypatch) -> None:
    """The model's words end up on the page the customer actually opens."""
    captured = capture(monkeypatch)
    intent = make_intent()
    intent.params["message_body"] = "Your card was declined. Tap to pay with another."
    executor().execute(intent, make_event("cart", 1_000))

    assert captured["body"]["description"] == "Your card was declined. Tap to pay with another."


def test_the_intent_id_rides_along_so_the_webhook_can_find_the_case(monkeypatch) -> None:
    captured = capture(monkeypatch)
    intent = make_intent()
    executor().execute(intent, make_event("cart", 1_000))

    assert captured["body"]["reference_id"] == str(intent.intent_id)
    assert captured["body"]["notes"]["case_id"] == "case_x"


def test_a_discount_reduces_what_is_actually_asked_for(monkeypatch) -> None:
    captured = capture(monkeypatch)
    executor().execute(
        make_intent(SEND_DISCOUNT, amount=1_000.0, discount_percent=20),
        make_event("cart", 1_000),
    )
    assert captured["body"]["amount"] == 80_000  # ₹800 in paise


def test_a_runaway_discount_is_clamped_here_too(monkeypatch) -> None:
    captured = capture(monkeypatch)
    executor().execute(
        make_intent(SEND_DISCOUNT, amount=1_000.0, discount_percent=90),
        make_event("cart", 1_000),
    )
    assert captured["body"]["amount"] == 75_000  # 25% ceiling, not 90%


def test_a_payment_plan_accepts_partial_payment(monkeypatch) -> None:
    """The whole point of the action is that they cannot pay it all at once."""
    captured = capture(monkeypatch)
    executor().execute(
        make_intent(OFFER_PARTIAL_PAYMENT_PLAN, amount=20_000.0), make_event("b2b", 20_000)
    )
    assert captured["body"]["accept_partial"] is True
    assert captured["body"]["first_min_partial_amount"] == 1_000_000


def test_notification_is_off_unless_a_channel_is_known(monkeypatch) -> None:
    captured = capture(monkeypatch)
    executor().execute(make_intent(), make_event("cart", 1_000))
    assert captured["body"]["notify"] == {"email": False, "sms": False}


def test_a_known_email_turns_on_email_delivery(monkeypatch) -> None:
    captured = capture(monkeypatch)
    executor().execute(
        make_intent(), make_event("cart", 1_000, customer_email="a@example.com")
    )
    assert captured["body"]["customer"]["email"] == "a@example.com"
    assert captured["body"]["notify"]["email"] is True


# ── Revenue honesty ────────────────────────────────────────────────


def test_creating_a_link_recovers_nothing(monkeypatch) -> None:
    """A link is not a payment. The case resolves on the paid webhook, not here."""
    capture(monkeypatch)
    result = executor().execute(make_intent(), make_event("cart", 1_000))

    assert result.status is ActionStatus.pending_host_execution
    assert result.recovered_amount == 0.0
    assert "plink_ExjpAUN3gVHrPJ" in result.details
    assert "[TEST]" in result.details


# ── Refusals and failures ──────────────────────────────────────────


def test_retry_charge_is_refused_rather_than_substituted() -> None:
    """Quietly sending a link instead would put a decision in the trace that never happened."""
    result = executor().execute(make_intent(RETRY_CHARGE), make_event("autopay", 1_000))
    assert result.status is ActionStatus.error
    assert "stored mandate" in result.details


def test_an_escalation_makes_no_provider_call(monkeypatch) -> None:
    def explode(*a, **k):
        raise AssertionError("no HTTP call should be made for an escalation")

    import httpx

    monkeypatch.setattr(httpx, "post", explode)
    result = executor().execute(make_intent(ESCALATE_TO_HUMAN), make_event("b2b", 9_000))
    assert result.status is ActionStatus.escalated


def test_missing_credentials_are_an_error_not_a_crash() -> None:
    result = RazorpayExecutor("", "").execute(make_intent(), make_event("cart", 1_000))
    assert result.status is ActionStatus.error
    assert "not configured" in result.details


def test_an_api_rejection_surfaces_razorpays_own_reason(monkeypatch) -> None:
    capture(monkeypatch, Response(400, {"error": {"description": "amount must be at least 100"}}))
    result = executor().execute(make_intent(), make_event("cart", 1_000))

    assert result.status is ActionStatus.error
    assert "amount must be at least 100" in result.details


def test_a_transport_failure_degrades_to_error(monkeypatch) -> None:
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", boom)
    result = executor().execute(make_intent(), make_event("cart", 1_000))

    assert result.status is ActionStatus.error
    assert "ConnectError" in result.details
