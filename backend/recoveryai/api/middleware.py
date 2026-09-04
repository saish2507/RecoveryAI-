"""Inbound signature verification, at the ASGI layer.

This sits below routing on purpose. Verifying a payload's signature *before*
parsing it means untrusted bytes are never handed to a validator, and it lets the
route declare a normal typed `RecoveryEvent` body — which is what puts the
ingestion contract into the OpenAPI schema where integrators can read it.

Doing it inside the handler instead would force the route to take raw bytes,
and the schema an integrator most needs would be absent from the docs.

The body is buffered and replayed rather than consumed: an ASGI request body is a
one-shot stream, so reading it here without replacing `receive` would leave the
route with nothing to parse.
"""

from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recoveryai.core.settings import Settings
from recoveryai.core.signing import verify_signature

logger = logging.getLogger(__name__)

#: Only ingestion is signed. Internal endpoints are protected by the API key.
SIGNED_PATHS = ("/api/v1/events",)


class WebhookSignatureMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not self.settings.webhook_signing_secret
            or scope["path"] not in SIGNED_PATHS
        ):
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        header_name = self.settings.webhook_signature_header.lower().encode()
        provided = next(
            (v.decode() for k, v in scope.get("headers", []) if k.lower() == header_name), None
        )

        if not verify_signature(self.settings.webhook_signing_secret, body, provided):
            logger.warning(
                "rejected event with an invalid signature",
                extra={"path": scope["path"], "signature_present": bool(provided)},
            )
            await _reject(
                send,
                "invalid_signature",
                f"The {self.settings.webhook_signature_header} header is missing or "
                "does not match the payload.",
            )
            return

        await self.app(scope, _replay(body), send)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _reject(send: Send, code: str, message: str) -> None:
    payload = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
