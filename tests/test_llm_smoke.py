"""The one thing the key-free suite cannot prove: the wire format is right.

Everything *around* the model is covered offline — routing, pruning, guardrail
interception, budget exhaustion, cache hits, provider failure, the fallback to
deterministic tables. What no `FakeProvider` can check is whether
`GeminiProvider` builds a request the real SDK accepts and parses the response
it actually returns. That gap is the top entry in LIMITATIONS.md, and this file
is what closes it.

Excluded from the default run by `addopts = -m 'not llm_smoke'`, and skipped
rather than failed when no key is present. Both matter:

* **Excluded**, so the offline suite stays offline. A test that quietly reaches
  the network the moment a developer's `.env` has a key is a test that makes the
  suite non-deterministic on exactly one machine.
* **Skipped, not failed**, so a fork or a contributor without a credential gets
  a green build. CI that fails for lack of a secret trains people to ignore red.

Run deliberately::

    GEMINI_API_KEY=... pytest -m llm_smoke

These make real API calls and cost real quota — one per vertical, deliberately
the smallest useful number.
"""

from __future__ import annotations

import os

import pytest
from conftest import make_event
from recoveryai.core.actions import is_known_action
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.llm.gemini import GeminiProvider
from recoveryai.core.models import DecisionSource
from recoveryai.core.settings import Settings
from recoveryai.core.verticals import get_vertical

pytestmark = pytest.mark.llm_smoke

# Read from the environment directly, not from `Settings`: the suite points
# `RECOVERYAI_ENV_FILE` at a file that does not exist precisely so a developer's
# real key cannot leak into the offline tests.
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

requires_key = pytest.mark.skipif(
    not API_KEY, reason="GEMINI_API_KEY is not set; the live smoke test is opt-in"
)


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    return Settings(
        gemini_api_key=API_KEY,
        database_url=f"sqlite:///{tmp_path / 'smoke.db'}",
        simulator_enabled=False,
        api_keys=[],
        webhook_signing_secret="",
    )


@requires_key
def test_the_provider_reports_itself_configured(live_settings: Settings) -> None:
    """Fails fast and legibly if the key is present but malformed."""
    provider = GeminiProvider(api_key=live_settings.gemini_api_key, model=live_settings.llm_model)
    assert provider.is_configured()


@requires_key
@pytest.mark.parametrize(
    ("vertical", "event_kwargs"),
    [
        # Each is deliberately ambiguous, so the routing governor actually
        # spends the call rather than answering from the rule tables.
        ("cart", {"amount": 3_400.0, "payment_gateway_error_code": "ISSUER_RISK_HOLD_B7"}),
        (
            "b2b",
            {
                "amount": 41_000.0,
                "dispute_flag": False,
                "days_overdue": 34,
                "payment_history_score": 0.66,
            },
        ),
        ("autopay", {"amount": 720.0, "bank_error_code": "NACH_RETURN_UNSPECIFIED", "retry_count": 3}),
    ],
)
def test_each_vertical_gets_a_usable_tool_call(vertical: str, event_kwargs: dict) -> None:
    """One real call per vertical, checked for the properties the agent relies on.

    Asserts the *shape* of the answer, never which action was chosen. Pinning a
    specific choice would make this fail whenever the model is updated, which
    would say nothing about whether the integration works.
    """
    provider = GeminiProvider(api_key=API_KEY, model=Settings(gemini_api_key=API_KEY).llm_model)
    config = get_vertical(vertical)

    call = provider.choose_tool(
        system_prompt=config.system_prompt,
        user_prompt=(
            "Decide the next recovery action for this case. "
            f"Vertical: {vertical}. Signals: {event_kwargs!r}."
        ),
        tools=config.tools(),
        timeout_seconds=30.0,
    )

    assert is_known_action(call.name), f"unknown action from the model: {call.name!r}"
    assert call.name in config.tool_palette
    # The two fields every schema requires, which is what makes the rationale
    # arrive in the same call as the decision instead of costing a second turn.
    assert call.reasoning
    assert 0.0 <= call.confidence <= 1.0


@requires_key
@pytest.mark.parametrize("vertical", ["cart", "b2b", "autopay"])
def test_a_live_decision_flows_all_the_way_through(vertical: str, live_settings, session) -> None:
    """End to end against the real provider: the answer survives validation, the
    guardrails, execution and the audit trail — not just the adapter."""
    events = {
        "cart": make_event("cart", 3_400, "medium", payment_gateway_error_code="ISSUER_RISK_HOLD_B7"),
        "b2b": make_event("b2b", 41_000, "high", days_overdue=34, payment_history_score=0.66),
        "autopay": make_event(
            "autopay", 720, "low", bank_error_code="NACH_RETURN_UNSPECIFIED", retry_count=3
        ),
    }

    agent = RecoveryAgent(settings=live_settings)
    _case, decision, _ = agent.handle_event(session, events[vertical])

    assert decision is not None
    assert is_known_action(decision.intent.final_action)
    assert decision.intent.reasoning
    # If this came back as a fallback, the model was never really consulted and
    # the test would be passing on the deterministic path it cannot prove.
    assert decision.intent.decision_source in {DecisionSource.llm, DecisionSource.llm_cached}
