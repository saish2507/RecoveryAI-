"""HTTP surface: the integration contract a host fintech's engineers would read.

These tests exercise the transport layer's own guarantees — idempotency,
signature verification, auth, error envelope, pagination, ordering — rather than
re-testing agent behaviour that `test_agent.py` already covers.
"""

from __future__ import annotations

import json
from datetime import UTC
from uuid import uuid4

import pytest
from conftest import make_event
from recoveryai.core.signing import sign_payload


def payload(**overrides) -> dict:
    """A valid event body. A fresh `event_id` each call, so distinct calls are
    distinct events; reuse the returned dict to simulate a redelivery."""
    body = {
        "event_id": str(uuid4()),
        "vertical": "cart",
        "customer_id": "cust_api",
        "customer_ltv_tier": "high",
        "amount": 1500.0,
        "raw_failure_reason": "card_declined",
        "vertical_metadata": {"payment_gateway_error_code": "card_declined"},
    }
    body.update(overrides)
    return body


# ── Health and status ──────────────────────────────────────────────


def test_health_is_cheap_and_unauthenticated(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_status_reports_governance_and_posture(client) -> None:
    body = client.get("/api/v1/system/status").json()

    assert body["agent"]["executor"] == "simulated"
    assert body["agent"]["llm"]["configured"] is True  # the suite supplies an offline model
    assert "minute_tokens_remaining" in body["agent"]["llm"]
    assert body["security"]["api_key_auth_enabled"] is False
    # An unsecured deployment says so out loud rather than looking healthy.
    assert any("API_KEYS" in w for w in body["security"]["warnings"])


def test_llm_switch_flips_and_is_visible_in_status(client) -> None:
    """The console's switch: one POST, reflected on the next status read."""
    assert client.get("/api/v1/system/status").json()["agent"]["llm"]["enabled"] is True

    body = client.post("/api/v1/system/llm", json={"enabled": False}).json()
    assert body["enabled"] is False
    assert body["available"] is False

    assert client.get("/api/v1/system/status").json()["agent"]["llm"]["enabled"] is False

    client.post("/api/v1/system/llm", json={"enabled": True})
    assert client.get("/api/v1/system/status").json()["agent"]["llm"]["enabled"] is True


def test_llm_switch_rejects_a_non_boolean(client) -> None:
    assert client.post("/api/v1/system/llm", json={"enabled": "maybe"}).status_code == 422


def test_openapi_documents_the_ingestion_contract(client) -> None:
    """The schema is the integration doc; if it is thin, integrators guess."""
    spec = client.get("/openapi.json").json()
    events = spec["paths"]["/api/v1/events"]["post"]

    assert "Idempot" in events["description"]
    assert "raw_failure_reason" in events["description"]
    schema = spec["components"]["schemas"]["RecoveryEvent"]
    assert schema["properties"]["raw_failure_reason"]["type"] == "string"  # not an enum
    assert "examples" in schema


# ── Ingestion ──────────────────────────────────────────────────────


def test_event_creates_a_case_and_decides_immediately(client) -> None:
    response = client.post("/api/v1/events", json=payload())

    assert response.status_code == 201
    body = response.json()
    assert body["vertical"] == "cart"
    assert body["priority_score"] > 0

    detail = client.get(f"/api/v1/cases/{body['id']}").json()
    assert len(detail["steps"]) == 1
    assert detail["steps"][0]["final_action"]
    assert detail["steps"][0]["reasoning"]


def test_redelivery_returns_the_original_case_without_new_work(client) -> None:
    """Webhook senders retry. The money gets worked once."""
    body = payload()
    first = client.post("/api/v1/events", json=body)
    second = client.post("/api/v1/events", json=body)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    assert client.get("/api/v1/cases").json()["total"] == 1
    assert len(client.get(f"/api/v1/cases/{first.json()['id']}").json()["steps"]) == 1


def test_idempotency_key_header_takes_precedence(client) -> None:
    headers = {"Idempotency-Key": "order-771"}
    first = client.post("/api/v1/events", json=payload(), headers=headers)
    second = client.post("/api/v1/events", json=payload(), headers=headers)

    assert first.json()["id"] == second.json()["id"]
    assert second.status_code == 200


def test_unknown_failure_code_is_accepted_not_rejected(client) -> None:
    """The contract's central promise: you need not map your codes onto ours."""
    response = client.post(
        "/api/v1/events", json=payload(raw_failure_reason="TOTALLY_NOVEL_BANK_CODE_2031")
    )
    assert response.status_code == 201
    assert response.json()["raw_failure_reason"] == "TOTALLY_NOVEL_BANK_CODE_2031"


def test_missing_failure_reason_is_accepted(client) -> None:
    body = payload()
    del body["raw_failure_reason"]
    assert client.post("/api/v1/events", json=body).status_code == 201


# ── Error envelope ─────────────────────────────────────────────────


def test_validation_error_uses_the_standard_envelope(client) -> None:
    response = client.post("/api/v1/events", json=payload(amount=-5))

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "amount" in body["error"]["message"]


def test_malformed_json_still_uses_the_envelope(client) -> None:
    response = client.post(
        "/api/v1/events", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_omitting_event_id_forfeits_deduplication(client) -> None:
    """Documents a real sharp edge rather than pretending it does not exist.

    `event_id` defaults to a fresh UUID, so a sender that omits it gets a new
    case on every delivery. Generating one beats rejecting the payload, but a
    sender who wants retry safety must supply `event_id` or `Idempotency-Key`.
    """
    body = payload()
    del body["event_id"]

    first = client.post("/api/v1/events", json=body)
    second = client.post("/api/v1/events", json=body)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    # ...and the documented remedy works.
    headers = {"Idempotency-Key": "invoice-4471"}
    a = client.post("/api/v1/events", json=body, headers=headers)
    b = client.post("/api/v1/events", json=body, headers=headers)
    assert a.json()["id"] == b.json()["id"]
    assert b.status_code == 200


def test_unknown_vertical_is_rejected(client) -> None:
    """`vertical` is a closed vocabulary we own, unlike `raw_failure_reason`."""
    response = client.post("/api/v1/events", json=payload(vertical="crypto"))
    assert response.status_code == 422


def test_missing_case_returns_the_envelope(client) -> None:
    body = client.get("/api/v1/cases/case_does_not_exist").json()
    assert body["error"]["code"] == "case_not_found"


# ── Auth ───────────────────────────────────────────────────────────


def test_writes_require_a_key_when_configured(client, settings) -> None:
    settings.api_keys = ["sk_live_abc"]

    assert client.post("/api/v1/events", json=payload()).status_code == 401
    assert (
        client.post("/api/v1/events", json=payload(), headers={"X-API-Key": "wrong"}).status_code == 401
    )
    assert (
        client.post("/api/v1/events", json=payload(), headers={"X-API-Key": "sk_live_abc"}).status_code
        == 201
    )


def test_reads_are_authenticated_too(client, settings) -> None:
    """A case list is a list of customers, amounts and failure reasons.
    "Only readable" is not a reason to publish it."""
    settings.api_keys = ["sk_live_abc"]

    assert client.get("/api/v1/cases").status_code == 401
    assert client.get("/api/v1/system/status").status_code == 401
    assert client.get("/api/v1/system/metrics").status_code == 401
    assert client.get("/api/v1/cases", headers={"X-API-Key": "sk_live_abc"}).status_code == 200


def test_a_wrong_key_is_refused_on_a_read(client, settings) -> None:
    settings.api_keys = ["sk_live_abc"]
    assert client.get("/api/v1/cases", headers={"X-API-Key": "sk_wrong"}).status_code == 401


def test_health_stays_open_for_load_balancers(client, settings) -> None:
    """A probe has no key, and a health check that fails closed on an auth
    misconfiguration would pull a healthy service out of rotation."""
    settings.api_keys = ["sk_live_abc"]
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_any_configured_key_is_accepted(client, settings) -> None:
    """Multi-key by design, so credentials can be rotated without downtime."""
    settings.api_keys = ["sk_old", "sk_new"]
    for key in settings.api_keys:
        assert client.post("/api/v1/events", json=payload(), headers={"X-API-Key": key}).status_code == 201


# ── Webhook signature ──────────────────────────────────────────────


def test_signed_events_are_accepted_and_unsigned_ones_rejected(client, settings) -> None:
    settings.webhook_signing_secret = "whsec_test"
    body = json.dumps(payload()).encode()
    headers = {"Content-Type": "application/json"}

    unsigned = client.post("/api/v1/events", content=body, headers=headers)
    assert unsigned.status_code == 401
    assert unsigned.json()["error"]["code"] == "invalid_signature"

    signed = client.post(
        "/api/v1/events",
        content=body,
        headers={**headers, "X-RecoveryAI-Signature": sign_payload("whsec_test", body)},
    )
    assert signed.status_code == 201


def test_a_tampered_body_fails_verification(client, settings) -> None:
    settings.webhook_signing_secret = "whsec_test"
    original = json.dumps(payload()).encode()
    tampered = json.dumps(payload(amount=999_999.0)).encode()

    response = client.post(
        "/api/v1/events",
        content=tampered,
        headers={
            "Content-Type": "application/json",
            "X-RecoveryAI-Signature": sign_payload("whsec_test", original),
        },
    )
    assert response.status_code == 401


# ── Queues ─────────────────────────────────────────────────────────


def test_case_queue_is_priority_ordered_not_fifo(client) -> None:
    for amount in (150.0, 47_000.0, 2_800.0):
        client.post("/api/v1/events", json=payload(amount=amount, customer_id=f"c_{amount}"))

    items = client.get("/api/v1/cases").json()["items"]
    scores = [item["priority_score"] for item in items]
    assert scores == sorted(scores, reverse=True)


def test_case_queue_paginates(client) -> None:
    for i in range(7):
        client.post("/api/v1/events", json=payload(customer_id=f"cust_{i}", amount=100.0 * (i + 1)))

    page = client.get("/api/v1/cases", params={"limit": 3, "offset": 0}).json()
    assert page["total"] == 7
    assert len(page["items"]) == 3

    rest = client.get("/api/v1/cases", params={"limit": 3, "offset": 3}).json()
    assert {i["id"] for i in page["items"]}.isdisjoint({i["id"] for i in rest["items"]})


def test_case_queue_filters(client) -> None:
    client.post("/api/v1/events", json=payload(vertical="cart", customer_id="a"))
    client.post(
        "/api/v1/events",
        json=payload(vertical="b2b", customer_id="b", raw_failure_reason="dispute_initiated",
                     vertical_metadata={"dispute_flag": True, "days_overdue": 10}),
    )

    assert client.get("/api/v1/cases", params={"vertical": "b2b"}).json()["total"] == 1
    assert client.get("/api/v1/cases", params={"status": "escalated"}).json()["total"] == 1
    assert client.get("/api/v1/cases", params={"open_only": True}).json()["total"] == 1


def test_review_queue_ranks_uncertain_value_above_confident_noise(client) -> None:
    """A big case the agent was unsure about must outrank a small forced escalation."""
    client.post(
        "/api/v1/events",
        json=payload(
            vertical="b2b", customer_id="small_dispute", amount=180.0,
            raw_failure_reason="dispute_initiated",
            vertical_metadata={"dispute_flag": True, "days_overdue": 5},
        ),
    )
    client.post(
        "/api/v1/events",
        json=payload(
            vertical="b2b", customer_id="big_unclear", amount=60_000.0,
            raw_failure_reason="payment_pending",
            vertical_metadata={"dispute_flag": False, "days_overdue": 30,
                               "payment_history_score": 0.65, "previous_touches": 3},
        ),
    )

    items = client.get("/api/v1/review/queue").json()["items"]
    assert len(items) == 2
    assert items[0]["customer_id"] == "big_unclear"
    assert items[0]["priority_score"] > items[1]["priority_score"]


def test_review_queue_excludes_cases_with_no_decision_yet(client, session) -> None:
    from recoveryai.core.cases import CaseStore

    CaseStore(session).create_from_event(make_event("cart", 5_000))
    session.commit()
    assert client.get("/api/v1/review/queue").json()["total"] == 0


# ── Host outcome reporting ─────────────────────────────────────────


def test_host_report_updates_the_case_and_resolves_it(client) -> None:
    case_id = client.post("/api/v1/events", json=payload()).json()["id"]
    step = client.get(f"/api/v1/cases/{case_id}").json()["steps"][0]

    response = client.post(
        f"/api/v1/actions/{step['intent_id']}/report",
        json={"status": "executed", "details": "customer paid", "recovered_amount": 1500.0, "cost": 0.0},
    )
    assert response.status_code == 200

    case = client.get(f"/api/v1/cases/{case_id}").json()
    assert case["status"] == "resolved"
    assert case["amount_recovered"] == 1500.0


def test_repeated_host_reports_do_not_double_count(client) -> None:
    """Retries are expected; a recovery must not be counted twice."""
    case_id = client.post("/api/v1/events", json=payload()).json()["id"]
    intent_id = client.get(f"/api/v1/cases/{case_id}").json()["steps"][0]["intent_id"]
    report = {"status": "executed", "recovered_amount": 500.0, "cost": 10.0}

    client.post(f"/api/v1/actions/{intent_id}/report", json=report)
    client.post(f"/api/v1/actions/{intent_id}/report", json=report)

    case = client.get(f"/api/v1/cases/{case_id}").json()
    assert case["amount_recovered"] == 500.0
    assert case["cost_of_recovery"] == 10.0


def test_report_for_an_unknown_intent_is_a_404(client) -> None:
    response = client.post(
        "/api/v1/actions/00000000-0000-0000-0000-000000000000/report", json={"status": "executed"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "intent_not_found"


# ── Dev tools ──────────────────────────────────────────────────────


def test_scenarios_are_listed_with_descriptions(client) -> None:
    scenarios = client.get("/api/v1/dev/scenarios").json()
    assert "cart_price_sensitive" in scenarios
    assert all(isinstance(v, str) and v for v in scenarios.values())


def test_injecting_the_discount_scenario_twice_spends_the_only_coupon(client) -> None:
    """The demo moment: same input, second time there is no coupon left.

    Note *where* the limit bites. A model that only picks from the tools it was
    offered never proposes the second discount at all, because `_prune_tools`
    removed it — so the trace shows a different action rather than a blocked one.
    A visible "proposed X, blocked, redirected to Y" needs a model that reaches
    outside the pruned set, which is covered in `test_agent.py` with a scripted
    one. Both paths are real; this is the one a well-behaved model produces.
    """
    first_id = client.post("/api/v1/dev/inject", params={"scenario": "cart_price_sensitive"}).json()["id"]
    second_id = client.post("/api/v1/dev/inject", params={"scenario": "cart_price_sensitive"}).json()["id"]
    assert first_id != second_id  # distinct events, same customer

    first_step = client.get(f"/api/v1/cases/{first_id}").json()["steps"][0]
    second_step = client.get(f"/api/v1/cases/{second_id}").json()["steps"][0]

    assert first_step["final_action"] == "send_discount"
    assert second_step["final_action"] != "send_discount"
    # The reason is legible in the snapshot the decision was made from.
    headroom = second_step["context_snapshot"]["guardrail_headroom"]
    assert headroom["discounts_remaining"] == 0
    assert "send_discount" not in second_step["context_snapshot"]["available_actions"]


def test_unknown_scenario_is_a_clean_400(client) -> None:
    response = client.post("/api/v1/dev/inject", params={"scenario": "nope"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_scenario"


def test_advancing_a_case_appends_a_step(client) -> None:
    case_id = client.post("/api/v1/events", json=payload()).json()["id"]
    body = client.post(f"/api/v1/dev/cases/{case_id}/advance").json()

    assert body["step"] == 2
    assert len(client.get(f"/api/v1/cases/{case_id}").json()["steps"]) == 2


def test_advancing_a_closed_case_is_a_conflict(client) -> None:
    case_id = client.post(
        "/api/v1/dev/inject", params={"scenario": "b2b_disputed"}
    ).json()["id"]
    response = client.post(f"/api/v1/dev/cases/{case_id}/advance")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "case_closed"


def test_followup_sweep_advances_due_cases(client) -> None:
    """The scheduled path, driven by hand so the assertion is deterministic."""
    from datetime import datetime, timedelta

    from recoveryai.db.models import Case
    from recoveryai.db.session import session_scope

    case_id = client.post("/api/v1/events", json=payload()).json()["id"]
    with session_scope() as session:
        session.get(Case, case_id).next_followup_at = datetime.now(UTC) - timedelta(seconds=1)

    body = client.post("/api/v1/dev/followups/run").json()

    assert body["advanced"] == 1
    assert body["cases"][0]["case_id"] == case_id
    assert body["cases"][0]["step"] == 2


# ── CORS ───────────────────────────────────────────────────────────


def test_cors_allows_the_console_origin_and_refuses_others(client, settings) -> None:
    def preflight(origin: str):
        return client.options(
            "/api/v1/cases",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )

    allowed = preflight(settings.cors_origins[0])
    assert allowed.headers.get("access-control-allow-origin") == settings.cors_origins[0]

    # An unlisted origin gets no allow header, so the browser blocks the read.
    assert "access-control-allow-origin" not in preflight("https://evil.example").headers


# ── WebSocket ──────────────────────────────────────────────────────


def test_websocket_pushes_an_invalidation_hint_on_ingestion(client) -> None:
    with client.websocket_connect("/ws") as ws:
        client.post("/api/v1/events", json=payload())
        message = ws.receive_json()

    assert message["type"] == "case.created"
    assert message["payload"]["vertical"] == "cart"
    assert message["payload"]["final_action"]


def test_websocket_handshake_is_rejected_without_a_key(client, settings) -> None:
    """Rejected before `accept()`: an unauthorised client must never reach the
    broadcaster's fan-out set, where it would read case ids and amounts."""
    from starlette.websockets import WebSocketDisconnect

    settings.api_keys = ["sk_live_abc"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_websocket_handshake_is_rejected_with_a_wrong_key(client, settings) -> None:
    from starlette.websockets import WebSocketDisconnect

    settings.api_keys = ["sk_live_abc"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?api_key=sk_wrong") as ws:
            ws.receive_json()


def test_websocket_accepts_a_valid_key_as_a_query_parameter(client, settings) -> None:
    """Query parameter, not just a header: browsers cannot set headers on a
    WebSocket handshake, so header-only would lock the console out entirely."""
    settings.api_keys = ["sk_live_abc"]

    with client.websocket_connect("/ws?api_key=sk_live_abc") as ws:
        client.post("/api/v1/events", json=payload(), headers={"X-API-Key": "sk_live_abc"})
        message = ws.receive_json()

    assert message["type"] == "case.created"


# ── Timestamps ─────────────────────────────────────────────────────


def test_timestamps_are_serialised_as_utc(client) -> None:
    """Regression: naive timestamps rendered a fresh case as "6h ago" in IST.

    SQLite drops tzinfo, so an offset-less ISO string reaches the browser and
    `new Date(...)` reads it as local time. Every read-model timestamp must carry
    an explicit offset.
    """
    from datetime import datetime

    case_id = client.post("/api/v1/events", json=payload()).json()["id"]
    detail = client.get(f"/api/v1/cases/{case_id}").json()

    stamps = [detail["created_at"], detail["updated_at"], detail["steps"][0]["created_at"]]
    for stamp in stamps:
        assert stamp.endswith("Z") or "+" in stamp[10:], f"{stamp} has no timezone offset"
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        # And it must actually be *now*, not now-shifted-by-a-timezone.
        drift = abs((datetime.now(UTC) - parsed).total_seconds())
        assert drift < 60, f"{stamp} is {drift:.0f}s from now — timezone was lost"


def test_error_envelope_is_published_in_the_schema(client) -> None:
    """The failure contract belongs in the docs, not in an integrator's incident log."""
    spec = client.get("/openapi.json").json()

    assert "ErrorEnvelope" in spec["components"]["schemas"]
    for code in ("401", "422"):
        ref = spec["paths"]["/api/v1/events"]["post"]["responses"][code]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert ref.endswith("/ErrorEnvelope")
