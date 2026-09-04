"""One error shape for the whole API.

Every non-2xx response is `{"error": {"code": ..., "message": ...}}`. A host's
engineers write one error handler, not one per endpoint — and FastAPI's default
`{"detail": ...}` (which is sometimes a string and sometimes a list of validation
objects) is not something anyone should have to branch on.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from recoveryai.core.models import ErrorEnvelope

logger = logging.getLogger(__name__)


class APIError(HTTPException):
    """An HTTPException carrying a stable machine-readable code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message


def envelope(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def error_response(description: str) -> dict[str, Any]:
    """An OpenAPI response entry that documents both the shape and the cause.

    Routes need their own descriptions ("no such case" reads better than "not
    found"), but a bare description silently drops the schema that the router's
    default supplied. This keeps both.
    """
    return {"model": ErrorEnvelope, "description": description}


#: Fallback codes for HTTPExceptions raised by FastAPI itself.
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_request: Request, exc: APIError) -> JSONResponse:
        return envelope(exc.status_code, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Flattened into a sentence: an integrator debugging a webhook payload
        # wants to know which field is wrong, not to parse a nested error tree.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}" for err in exc.errors()
        )
        return envelope(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", problems or "invalid request payload"
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Logged in full, returned as a generic message: stack traces and internal
        # identifiers are not something to hand to an unauthenticated caller.
        logger.exception("unhandled error", extra={"path": request.url.path})
        return envelope(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred. The incident has been logged.",
        )
