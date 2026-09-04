"""FastAPI dependencies — the transport layer's only view into core.

Note what is *not* here: any business logic. Every dependency either hands over a
session with a proper commit/rollback boundary, or hands over an object built at
startup. The agent itself is constructed once in `main.lifespan` and reused,
because it owns the rate limiter and the LLM cache, and rebuilding it per request
would reset both — which is how a "rate-limited" service ends up making unbounded
calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.scheduler import FollowupScheduler
from recoveryai.core.settings import Settings
from recoveryai.db.session import get_session_factory


def get_db(request: Request) -> Iterator[Session]:
    """One transaction per request. Commits on success, rolls back on any error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_agent(request: Request) -> RecoveryAgent:
    return request.app.state.agent


def get_scheduler(request: Request) -> FollowupScheduler:
    return request.app.state.scheduler


def get_broadcaster(request: Request):
    return request.app.state.broadcaster


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]
Agent = Annotated[RecoveryAgent, Depends(get_agent)]
Scheduler = Annotated[FollowupScheduler, Depends(get_scheduler)]
