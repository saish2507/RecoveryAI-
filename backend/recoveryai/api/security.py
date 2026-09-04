"""API-key auth and inbound webhook signature verification.

Two independent controls, deliberately:

* **API key** — proves the *caller* is allowed to use this API at all. Applied to
  every endpoint except `/health`, reads included: a case list is a list of
  customers, amounts and failure reasons, and "it is only readable" is not a
  reason to publish it. The console therefore needs a key of its own; see
  README's Security section for how to hold one without embedding it in
  browser JavaScript.
* **HMAC signature** — proves the *payload* came from the party holding the
  signing secret and was not altered. This is the control a Razorpay-style
  webhook integration actually requires, and it is what the previous build left
  as a documented gap.

Both fail open only when explicitly unconfigured, and both say so loudly at
startup, because an auth control that is silently disabled is worse than none.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Request, status

from recoveryai.api.errors import APIError
from recoveryai.core.settings import Settings
from recoveryai.core.signing import verify_signature

logger = logging.getLogger(__name__)


def key_is_valid(settings: Settings, provided: str | None) -> bool:
    """Whether `provided` is one of the configured keys.

    The single definition of "valid key" in the codebase. HTTP routes and the
    WebSocket handshake both call it rather than each re-deriving the check —
    two copies of an auth predicate is two places for one of them to be fixed.

    Compared with `compare_digest` against every configured key, so neither the
    key's value nor which key matched leaks through response timing. Returns
    `True` when auth is disabled, which is the documented unconfigured posture
    and is shouted about at startup by `warn_if_unsecured`.
    """
    if not settings.auth_enabled:
        return True
    if not provided:
        return False
    return any(hmac.compare_digest(provided, key) for key in settings.api_keys)


def require_api_key(request: Request) -> None:
    """Reject a request from an unknown caller."""
    settings: Settings = request.app.state.settings
    if key_is_valid(settings, request.headers.get(settings.api_key_header)):
        return

    logger.warning(
        "rejected request with a missing or invalid api key",
        extra={"path": request.url.path, "key_present": bool(request.headers.get(settings.api_key_header))},
    )
    raise APIError(
        status.HTTP_401_UNAUTHORIZED,
        "unauthorized",
        f"A valid {settings.api_key_header} header is required for this endpoint.",
    )


async def verify_webhook_signature(request: Request, body: bytes) -> None:
    """Reject an unsigned or mis-signed inbound event.

    Takes the raw body rather than the parsed model on purpose: re-serialising
    would change key order and whitespace and invalidate every signature.
    """
    settings: Settings = request.app.state.settings
    if not settings.webhook_signing_secret:
        return

    provided = request.headers.get(settings.webhook_signature_header)
    if not verify_signature(settings.webhook_signing_secret, body, provided):
        logger.warning(
            "rejected event with an invalid signature",
            extra={"path": request.url.path, "signature_present": bool(provided)},
        )
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_signature",
            f"The {settings.webhook_signature_header} header is missing or does not match the payload.",
        )


def warn_if_unsecured(settings: Settings) -> list[str]:
    """Startup posture check. Returns warnings; also surfaced by `/api/v1/system/status`.

    Deliberately visible rather than silent: "we thought auth was on" is a common
    and expensive way to run an unprotected service.
    """
    warnings: list[str] = []
    if not settings.auth_enabled:
        warnings.append(
            "API_KEYS is empty — every endpoint, reads included, is unauthenticated. Local dev only."
        )
    if not settings.webhook_signing_secret:
        warnings.append(
            "WEBHOOK_SIGNING_SECRET is empty — inbound events are not verified. Local dev only."
        )
    if "*" in settings.cors_origins:
        warnings.append("CORS_ORIGINS contains '*' — any site can call this API from a browser.")
    for warning in warnings:
        logger.warning("security posture", extra={"warning": warning})
    return warnings
