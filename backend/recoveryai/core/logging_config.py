"""Structured JSON logging.

Machine-readable logs are what make an agent's behaviour queryable after the
fact — "show me every case where a guardrail redirected a discount last week" is
a log query, not an archaeology project. The `extra=` dicts scattered through the
agent are the fields this formatter promotes to top level.

The redaction filter is not decorative, and it covers two different things.
Config objects with API keys and signing secrets pass close to log statements,
and a secret in a log file is a secret leaked to everyone with log access. The
second category is customer data — identifiers, amounts, model reasoning —
which is redacted for a different reason: it is not needed. A log line's job is
to say *which case*, and `case_id` does that while staying meaningless to anyone
without database access.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

#: Attributes `logging` puts on every record. Anything else came from `extra=`.
_STANDARD = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno lineno
    module msecs message msg name pathname process processName relativeCreated stack_info
    thread threadName taskName""".split()
)

_SECRET_KEYS = re.compile(
    r"(api[_-]?key|secret|token|password|authorization|signature)", re.IGNORECASE
)

#: Fields that identify a customer or reveal their commercial relationship.
#:
#: Logs live longer, spread wider and are read by more people than the database
#: is: shipped to an aggregator, tailed in a terminal, pasted into a ticket.
#: None of that is a reason to hold a customer identifier or an invoice amount,
#: because the case id already answers every operational question a log line is
#: for — "which case was this" — and resolves to the full record for anyone
#: with actual database access.
#:
#: `reasoning` is here because it is free-form model output *about* a named
#: customer's finances, which is the last thing that should sit unstructured in
#: a log aggregator.
_SENSITIVE_KEYS = frozenset(
    {
        "customer_id",
        "amount",
        "amount_at_risk",
        "recovered_amount",
        "reasoning",
        "raw_failure_reason",
        "message_body",
        "message_subject",
    }
)

REDACTED = "***redacted***"


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEYS.search(key):
        return REDACTED
    if key in _SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = _redact(value, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable, for local development. Same redaction guarantee."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        extras = {
            k: _redact(v, k)
            for k, v in record.__dict__.items()
            if k not in _STANDARD and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Install the formatter on the root logger. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # APScheduler logs every tick of a 5-second poll at INFO. That is noise that
    # would bury the decisions we actually want to read.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
