"""HMAC-SHA256 payload signing, both directions.

Inbound: verify that a webhook really came from the host fintech.
Outbound: sign the `ActionIntent` we POST to the host, so they can verify us.

Stdlib only, and deliberately symmetric — the same scheme in both directions is
one thing for an integrator to implement rather than two. `compare_digest` is
used for the comparison so verification does not leak the secret through timing.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def sign_payload(secret: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of the **raw** body bytes, prefixed with the algorithm.

    Signing raw bytes rather than re-serialised JSON matters: any difference in
    key order or whitespace between sender and receiver would otherwise break
    every signature.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, provided: str | None) -> bool:
    """Constant-time signature check. Missing or malformed signature → False."""
    if not secret:
        # No secret configured means verification is disabled upstream; this
        # function never silently passes an unverifiable payload.
        return False
    if not provided:
        return False
    expected = sign_payload(secret, body)
    candidate = provided.strip()
    if not candidate.startswith(SIGNATURE_PREFIX):
        candidate = f"{SIGNATURE_PREFIX}{candidate}"
    return hmac.compare_digest(expected, candidate)
