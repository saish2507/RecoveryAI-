"""What must never reach a log line.

Two categories, redacted for two different reasons. Secrets, because a log
aggregator is not a vault. Customer data, because a log line's job is to say
*which case* — and `case_id` does that while meaning nothing to anyone without
database access.

These assert on the rendered output rather than on `_redact` directly: the
formatter is what actually ships bytes, and a redaction that works in isolation
but is bypassed by the formatter's own field handling is not a redaction.
"""

from __future__ import annotations

import json
import logging

import pytest
from recoveryai.core.logging_config import REDACTED, JsonFormatter, TextFormatter


def render(formatter: logging.Formatter, **extra) -> str:
    record = logging.LogRecord(
        name="recoveryai.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="case step decided",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return formatter.format(record)


# ── Customer data ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("customer_id", "cust_10482"),
        ("amount", 41000.0),
        ("reasoning", "They abandoned a large cart twice this week; try a nudge first."),
        ("raw_failure_reason", "ISSUER_RISK_HOLD_B7"),
    ],
)
def test_sensitive_fields_never_reach_the_json_log(field: str, value: object) -> None:
    line = render(JsonFormatter(), case_id="case_abc123", **{field: value})

    assert str(value) not in line
    assert json.loads(line)[field] == REDACTED


def test_the_case_id_survives_because_it_is_the_whole_point() -> None:
    """Redaction that also removed the handle would make logs useless."""
    payload = json.loads(render(JsonFormatter(), case_id="case_abc123", amount=41_000.0))

    assert payload["case_id"] == "case_abc123"
    assert payload["message"] == "case step decided"


def test_nested_customer_data_is_redacted_too() -> None:
    """Context snapshots and intent dumps arrive as nested dicts, not flat kwargs."""
    line = render(
        JsonFormatter(),
        case_id="case_abc123",
        intent={"customer_id": "cust_10482", "amount": 41_000.0, "final_action": "send_nudge"},
    )

    payload = json.loads(line)
    assert payload["intent"]["customer_id"] == REDACTED
    assert payload["intent"]["amount"] == REDACTED
    # The operational field is untouched.
    assert payload["intent"]["final_action"] == "send_nudge"


def test_drafted_customer_copy_is_redacted() -> None:
    """Outreach copy is written *to* a named person about their finances."""
    body = "Your payment failed because your bank placed a security hold on the card."
    line = render(JsonFormatter(), case_id="case_abc123", message_body=body)
    assert body not in line


# ── Secrets ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    ["api_key", "gemini_api_key", "webhook_signing_secret", "authorization", "X-RecoveryAI-Signature"],
)
def test_secret_shaped_keys_are_redacted(field: str) -> None:
    line = render(JsonFormatter(), **{field: "sk_live_do_not_log_me"})
    assert "sk_live_do_not_log_me" not in line


# ── The human-readable formatter makes the same promise ────────────


def test_the_text_formatter_redacts_identically() -> None:
    """Local development reads these; the guarantee cannot depend on LOG_JSON."""
    line = render(TextFormatter(), case_id="case_abc123", customer_id="cust_10482", amount=41_000.0)

    assert "cust_10482" not in line
    assert "41000" not in line
    assert "case_abc123" in line
