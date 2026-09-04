"""FastAPI application — the transport layer, and the only place a web framework appears.

Everything below this file is a plain Python package. This module wires it to
HTTP: routing, auth, CORS, error envelopes, WebSocket fan-out and process
lifecycle. Delete it and the agent still works; that is the point.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from recoveryai.api.errors import register_error_handlers
from recoveryai.api.middleware import WebhookSignatureMiddleware
from recoveryai.api.routes import actions, cases, dev, events, system
from recoveryai.api.security import key_is_valid, require_api_key, warn_if_unsecured
from recoveryai.api.ws import Broadcaster
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.logging_config import configure_logging
from recoveryai.core.models import ErrorEnvelope
from recoveryai.core.scheduler import FollowupScheduler
from recoveryai.core.settings import Settings, get_settings
from recoveryai.db.models import Case
from recoveryai.db.session import init_db, session_scope
from recoveryai.simulator import (
    PERMUTATION_CYCLE_LENGTH,
    build_permutation_event,
    permutation_customer_pattern,
)

logger = logging.getLogger(__name__)

DESCRIPTION = """
An autonomous, governed revenue-recovery agent.

It detects revenue at risk, decides an intervention with an LLM choosing from a
guardrail-bounded action palette, and hands off a fully reasoned `ActionIntent` —
it decides, it does not execute. Your infrastructure carries out the action.

### Integrating

* **As a Python library** — `from recoveryai.core.agent import RecoveryAgent`.
  `recoveryai.core` has no web-framework dependency; this API is optional.
* **Over HTTP** — POST events to `/api/v1/events`, point `ACTION_WEBHOOK_URL` at
  your endpoint, and report outcomes to `/api/v1/actions/{intent_id}/report`.

### Before you go live

Run with `AGENT_MODE=shadow`. The agent reasons over your real traffic and logs
every decision in full, but dispatches nothing. Read the trace, then flip to
`live`.
""".strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        warn_if_unsecured(settings)

        app.state.settings = settings
        app.state.broadcaster = Broadcaster()
        app.state.broadcaster.bind_loop(asyncio.get_running_loop())
        app.state.agent = RecoveryAgent(settings=settings)
        app.state.scheduler = FollowupScheduler(
            agent=app.state.agent,
            session_scope=session_scope,
            on_decision=lambda d: app.state.broadcaster.publish_threadsafe(
                "case.updated",
                {
                    "case_id": d.case_id,
                    "step": d.step_number,
                    "final_action": d.intent.final_action,
                    "status": d.case_status,
                    "source": "followup",
                },
            ),
        )
        app.state.scheduler.start()

        app.state.simulator_task = None
        if settings.simulator_enabled:
            app.state.simulator_task = asyncio.create_task(_simulator_loop(app, settings))

        logger.info(
            "RecoveryAI started",
            extra={"mode": settings.agent_mode, "executor": app.state.agent.executor.name},
        )
        try:
            yield
        finally:
            if app.state.simulator_task is not None:
                app.state.simulator_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await app.state.simulator_task
            app.state.scheduler.shutdown()
            logger.info("RecoveryAI stopped")

    app = FastAPI(
        title="RecoveryAI",
        version="2.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # Below routing: untrusted bytes are verified before anything parses them.
    app.add_middleware(WebhookSignatureMiddleware, settings=settings)

    # An allow-list, never a wildcard. The console is a known origin; anything
    # else calling this API from a browser is something we did not intend.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", settings.api_key_header, "Idempotency-Key",
                       settings.webhook_signature_header],
    )

    register_error_handlers(app)

    # Publish the error envelope on every route, so an integrator reading the
    # schema sees the one shape all failures take instead of discovering it by
    # triggering errors in production.
    error_responses = {
        400: {"model": ErrorEnvelope, "description": "Malformed request."},
        401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials."},
        404: {"model": ErrorEnvelope, "description": "No such resource."},
        422: {"model": ErrorEnvelope, "description": "Payload failed schema validation."},
        500: {"model": ErrorEnvelope, "description": "Unexpected server error."},
    }
    # Liveness only. No credentials, no data.
    app.include_router(system.public_router, responses=error_responses)

    # Everything else, reads included. The API key is applied once here rather
    # than route by route so a new endpoint is authenticated by default —
    # forgetting a decorator should not be able to publish case data.
    for router in (system.router, events.router, cases.router, actions.router, dev.router):
        app.include_router(
            router, responses=error_responses, dependencies=[Depends(require_api_key)]
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Live console feed.

        Messages are invalidation hints (`{"type": "case.created", ...}`), not
        full state — the console re-fetches over REST, so there is one source of
        truth and the socket cannot drift out of sync with the API.

        Authenticated with the same key as every other endpoint, checked *before*
        `accept()`: a rejected handshake never becomes a connection, so an
        unauthorised client cannot sit in the broadcaster's fan-out set reading
        case ids and amounts while we decide what to do with it.

        The key may arrive as a header or as a `?api_key=` query parameter.
        Browsers cannot set headers on a WebSocket handshake, so header-only
        would mean the console could not connect at all.
        """
        ws_settings: Settings = websocket.app.state.settings
        provided = websocket.headers.get(ws_settings.api_key_header) or websocket.query_params.get(
            "api_key"
        )
        if not key_is_valid(ws_settings, provided):
            logger.warning(
                "rejected websocket handshake with a missing or invalid api key",
                extra={"path": "/ws", "key_present": bool(provided)},
            )
            # Close without accepting. Starlette turns this into an HTTP 403 on
            # the upgrade request, which is what a client should see.
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        broadcaster: Broadcaster = websocket.app.state.broadcaster
        await broadcaster.connect(websocket)
        try:
            while True:
                # Read-and-discard: this keeps the connection alive and lets a
                # client heartbeat, but the socket is deliberately one-way.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("websocket closed unexpectedly", exc_info=True)
        finally:
            await broadcaster.disconnect(websocket)

    return app


def _resume_permutation_index() -> int:
    """Where the permutation walk left off, counted from what is already stored.

    The walk's position has to survive a restart. Holding it only in memory meant
    every restart re-emitted lap zero — duplicate cases in the queue, and a
    "fresh" customer who had in fact already spent their one 90-day discount in
    an earlier run, so the guardrail fired for reasons the case itself did not
    explain.

    Counting rows is enough here and needs no extra state to keep in sync: the
    walk emits exactly one case per step, so the number emitted *is* the number
    stored. A wiped database correctly restarts at zero, because there is then
    nothing left to collide with.
    """
    try:
        with session_scope() as session:
            emitted = session.scalar(
                select(func.count(Case.id)).where(
                    Case.customer_id.like(permutation_customer_pattern(), escape="\\")
                )
            )
        return int(emitted or 0)
    except Exception:
        # A counting failure must not stop demo traffic; the cost of guessing
        # zero is duplicate ids, not a broken system.
        logger.warning("could not resume the permutation walk; starting from zero", exc_info=True)
        return 0


async def _simulator_loop(app: FastAPI, settings: Settings) -> None:
    """Background demo traffic, so the console has something to show.

    Off by default in any real deployment (`SIMULATOR_ENABLED=false`) — a system
    that invents its own events is a demo, not a product.

    Walks `PERMUTATION_MATRIX` in order rather than sampling randomly. At a
    watchable rate the difference matters: random sampling at ten cases an hour
    would show four cart declines and nothing else for the first half hour,
    while the walk guarantees every diagnosis path appears exactly once per
    ten-case cycle.
    """
    await asyncio.sleep(2.0)  # let startup settle before generating load
    index = _resume_permutation_index()
    while True:
        try:
            await asyncio.sleep(settings.simulator_interval_seconds * random.uniform(0.9, 1.1))
            event, label = build_permutation_event(index)
            position = f"{index % PERMUTATION_CYCLE_LENGTH + 1}/{PERMUTATION_CYCLE_LENGTH}"
            index += 1
            with session_scope() as session:
                case, decision, _ = app.state.agent.handle_event(session, event)
                payload = {
                    "case_id": case.id,
                    "vertical": case.vertical,
                    "amount": case.amount,
                    "priority_score": case.priority_score,
                    "final_action": decision.intent.final_action if decision else None,
                    "source": "simulator",
                }
                logger.info(
                    "simulator injected permutation case",
                    extra={
                        "case_id": case.id,
                        "permutation": label,
                        "cycle_position": position,
                        "diagnosis": case.diagnosis,
                        "final_action": payload["final_action"],
                    },
                )
            await app.state.broadcaster.publish("case.created", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad generated event must not kill the traffic stream.
            logger.exception("simulator iteration failed")


app = create_app()
