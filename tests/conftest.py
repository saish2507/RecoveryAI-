"""Shared fixtures.

Every test in this suite runs with **no API key and no network access**. That is
a deliberate property, not a convenience: a test suite that needs a live model is
a test suite nobody runs, and the graceful-degradation paths are exactly the ones
most worth covering.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Point Settings at a file that does not exist, so a developer's real .env can
# never leak an API key into a test run.
os.environ.setdefault("RECOVERYAI_ENV_FILE", str(Path(__file__).parent / "nonexistent.env"))

from recoveryai.core.llm.base import LLMUnavailable, ToolCall, ToolSpec  # noqa: E402
from recoveryai.core.llm.governance import GovernedLLM, RateLimiter  # noqa: E402
from recoveryai.core.models import (  # noqa: E402
    ActionIntent,
    ActionStatus,
    ExecutionResult,
    LTVTier,
    RecoveryEvent,
    Vertical,
)
from recoveryai.core.settings import Settings  # noqa: E402

# ── Fakes ──────────────────────────────────────────────────────────


class FakeProvider:
    """A scripted `LLMProvider`. Returns queued tool calls; records what it saw."""

    name = "fake"

    def __init__(self, script: list[ToolCall | Exception] | None = None, configured: bool = True) -> None:
        self.script: list[ToolCall | Exception] = list(script or [])
        self.configured = configured
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self.configured

    def choose_tool(
        self, *, system_prompt: str, user_prompt: str, tools: list[ToolSpec], timeout_seconds: float
    ) -> ToolCall:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": [t.name for t in tools],
            }
        )
        if not self.script:
            raise LLMUnavailable("fake_provider_script_exhausted")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeExecutor:
    """Records intents instead of acting on them. Proves the seam is real.

    Recovers nothing by default. Any non-zero `recovered_amount` now resolves the
    case, so a fixture that always reported a recovery would end every case on
    its first step and quietly make the multi-step lifecycle tests vacuous. Tests
    that want a landed payment opt in with `FakeExecutor(recovered_amount=...)`.
    """

    name = "fake"

    def __init__(
        self,
        status: ActionStatus = ActionStatus.executed,
        recovered_amount: float = 0.0,
    ) -> None:
        self.status = status
        self.recovered_amount = recovered_amount
        self.executed: list[ActionIntent] = []

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        self.executed.append(intent)
        return ExecutionResult(
            status=self.status,
            details=f"[FAKE] {intent.final_action}",
            cost=1.0,
            recovered_amount=self.recovered_amount,
            executor=self.name,
        )

    @property
    def actions(self) -> list[str]:
        return [i.final_action for i in self.executed]


class CompetentOfflineProvider:
    """An offline stand-in for the model that always returns a usable answer.

    The suite runs with no API key and no network, and the agent no longer has a
    policy table to decide in the model's absence — so without a provider like
    this, every case would defer and there would be nothing to assert on.

    It reads the diagnosis out of the prompt and picks whichever offered tool has
    the best odds against it, falling back to escalation when nothing on offer
    can collect. That is deterministic, always inside the palette, and close
    enough to a sensible agent that tests about *lifecycle* do not have to script
    a decision. Tests about *which* action gets chosen script `governed_llm`.
    """

    name = "offline"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    def choose_tool(self, *, system_prompt, user_prompt, tools, timeout_seconds) -> ToolCall:
        import json as _json

        from recoveryai.core.economics import success_rate

        self.calls.append({"tools": [t.name for t in tools], "user_prompt": user_prompt})
        try:
            diagnosis = _json.loads(user_prompt)["rule_based_prior"]["diagnosis"]
        except Exception:
            diagnosis = "unknown"

        offered = [t.name for t in tools]
        collecting = [n for n in offered if success_rate(n, diagnosis) > 0]
        best = (
            max(collecting, key=lambda n: success_rate(n, diagnosis))
            if collecting
            else ("escalate_to_human" if "escalate_to_human" in offered else offered[0])
        )
        return ToolCall(
            name=best,
            arguments={"discount_percent": 10, "split": "50% now, 50% in 30 days",
                       "retry_window": "next_salary_date", "escalation_reason": "needs a human",
                       "close_reason": "not worth pursuing"},
            reasoning=f"Offline stand-in: best available lever for {diagnosis}.",
            confidence=0.7,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ExplodingExecutor:
    """Raises, to prove a misbehaving host executor cannot lose a decision."""

    name = "exploding"

    def execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        raise RuntimeError("host executor blew up")


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings on a throwaway SQLite file."""
    return Settings(
        gemini_api_key="",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        agent_mode="live",
        action_executor="simulated",
        followup_delay_seconds=60.0,
        simulator_enabled=False,
        api_keys=[],
        webhook_signing_secret="",
    )


@pytest.fixture
def db(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """A live schema on the isolated database, with the engine cache reset."""
    from recoveryai.core import settings as settings_module
    from recoveryai.db import session as session_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(session_module, "get_settings", lambda: settings)
    session_module.reset_engine()
    session_module.init_db()
    yield session_module
    session_module.reset_engine()


@pytest.fixture
def session(db):
    s = db.get_session_factory()()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def outcome():
    """Force whether simulated actions land, instead of leaving it to the RNG.

    `SimulatedExecutor` rolls against each action's `recovery_rate`, so anything
    asserting on a case's status or revenue is otherwise flaky by construction —
    a 15% nudge would resolve the case in roughly one run of every seven.

        def test_x(outcome):
            outcome(True)   # every attempt lands
    """

    def _set(lands: bool, monkeypatch: pytest.MonkeyPatch = None) -> None:  # noqa: ANN001
        from recoveryai.core import actions

        # Signature mirrors the real `lands`, including the step gate, so a test
        # that forces success still cannot fabricate a first-step recovery.
        actions.lands = (
            lambda probability, step_number=actions.FIRST_OBSERVABLE_STEP: (
                lands and probability > 0 and step_number >= actions.FIRST_OBSERVABLE_STEP
            )
        )

    original = None
    try:
        from recoveryai.core import actions

        original = actions.lands
        yield _set
    finally:
        if original is not None:
            from recoveryai.core import actions

            actions.lands = original


@pytest.fixture(autouse=True)
def _no_accidental_recoveries(request):
    """Default every test to "nothing lands" unless it opts in via `outcome`.

    Determinism by default: a test that never mentions revenue should not fail
    once a fortnight because a simulated nudge happened to succeed.
    """
    if "outcome" in request.fixturenames:
        yield
        return

    from recoveryai.core import actions

    original = actions.lands
    actions.lands = lambda probability, step_number=actions.FIRST_OBSERVABLE_STEP: False
    try:
        yield
    finally:
        actions.lands = original


@pytest.fixture
def client(db, settings, monkeypatch):
    """A TestClient on an isolated database, with background traffic disabled.

    Lives here rather than in `test_api.py` so every module that needs to drive
    the HTTP surface shares one definition of "the app under test" — two
    subtly-different app fixtures is two different systems being tested.
    """
    from fastapi.testclient import TestClient
    from recoveryai.api import main as main_module

    settings.simulator_enabled = False
    settings.log_json = False
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    app = main_module.create_app(settings)
    with TestClient(app) as test_client:
        # Give the app a model. Without one nothing decides, because there is no
        # longer a policy table standing behind an unavailable provider.
        app.state.agent.llm = GovernedLLM(
            CompetentOfflineProvider(),
            rate_limiter=RateLimiter(max_per_minute=10_000, max_per_day=10_000),
            budget=None,
            cache_enabled=False,
        )
        # Stop the scheduler thread: these tests drive follow-ups explicitly, and
        # a background sweep would advance cases mid-assertion.
        app.state.scheduler.shutdown()
        yield test_client


@pytest.fixture
def governed_llm():
    """Factory: build a `GovernedLLM` around a scripted provider."""

    def _make(script: list[ToolCall | Exception] | None = None, **kwargs: Any) -> GovernedLLM:
        provider = FakeProvider(script, configured=kwargs.pop("configured", True))
        llm = GovernedLLM(
            provider,
            rate_limiter=RateLimiter(
                max_per_minute=kwargs.pop("max_per_minute", 100),
                max_per_day=kwargs.pop("max_per_day", 1000),
            ),
            budget=None,
            cache_enabled=kwargs.pop("cache_enabled", True),
            transient_max_retries=kwargs.pop("transient_max_retries", 1),
            # Real backoff is 500ms; no test should pay that to prove a retry
            # fired, and sleeping for it would be testing `time.sleep`.
            transient_backoff_seconds=kwargs.pop("transient_backoff_seconds", 0.0),
        )
        llm.fake_provider = provider  # type: ignore[attr-defined]
        return llm

    return _make


# ── Event builders ─────────────────────────────────────────────────


def make_event(
    vertical: str = "cart",
    amount: float = 500.0,
    ltv: str = "medium",
    customer_id: str = "cust_test",
    raw_failure_reason: str = "",
    **metadata: Any,
) -> RecoveryEvent:
    return RecoveryEvent(
        vertical=Vertical(vertical),
        customer_id=customer_id,
        customer_ltv_tier=LTVTier(ltv),
        amount=amount,
        raw_failure_reason=raw_failure_reason,
        vertical_metadata=metadata,
    )
