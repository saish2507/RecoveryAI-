"""The agent loop.

One LLM call per decision, not a ReAct-style multi-turn loop. Everything the
model needs — the case, its prior steps, the guardrail headroom that remains,
the cheap rule diagnosis as a prior — goes into a single prompt, and the model
answers by calling exactly one tool. A multi-turn loop would multiply latency
and cost here for no decision quality worth paying for.

The order of operations is the whole design:

    diagnose  →  prune tools  →  decide  →  guardrail  →  redirect  →  execute
              →  record  →  reprice  →  schedule

The model decides every case. The rules still run first, but they produce a
diagnosis and a confidence that travel into the prompt as a prior the model is
told it may disagree with — they no longer answer the case themselves. An
earlier design let a confident rule resolve the case without a model call at
all, which was cheaper and meant most of this system's decisions were made by a
lookup table.

Pruning removes tools the guardrail would reject anyway, so the model is not
tempted into a decision that cannot happen. The guardrail then re-checks the
answer regardless — pruning is a courtesy, the check is the control, and defence
in depth means never relying on the courtesy.

A blocked proposal is *redirected*, never dropped: the agent proposed X, the
guardrail refused it for reason R, the system did Y instead, and all three
appear in the trace. Silently swallowing a blocked action would leave revenue
unworked and the reviewer with no idea why.

Repricing closes the loop. After the step, `economics.assess` re-estimates what
is still recoverable given the capacity that step just consumed, and that figure
sets how soon the case is looked at again — and whether it is worth looking at
at all. Priority that only sorts a queue changes nothing when there is no
backlog; priority that sets the timer changes how much of the agent's attention
each case gets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from recoveryai.core.actions import (
    ALL_ACTIONS,
    CLOSE_AS_UNRECOVERABLE,
    ESCALATE_TO_HUMAN,
    is_known_action,
)
from recoveryai.core.cases import CaseStore, intake_priority
from recoveryai.core.drafts import attach_draft
from recoveryai.core.economics import Prospects, assess, band
from recoveryai.core.execution import ActionExecutor, build_executor
from recoveryai.core.llm.base import LLMUnavailable, ToolCall
from recoveryai.core.llm.gemini import GeminiProvider
from recoveryai.core.llm.governance import (
    DailyBudgetCounter,
    GovernedLLM,
    RateLimiter,
    hash_input,
)
from recoveryai.core.models import (
    ActionIntent,
    ActionStatus,
    CaseStatus,
    DecisionSource,
    ExecutionResult,
    GuardrailVerdict,
    RecoveryEvent,
    RoutingDecision,
)
from recoveryai.core.notifications import Notifier, build_notifier
from recoveryai.core.policy import GUARDRAIL_VERSION, GuardrailCapacity, check_guardrails
from recoveryai.core.prompt_safety import UNTRUSTED_DATA_NOTICE, fence, fence_mapping
from recoveryai.core.settings import Settings, get_settings
from recoveryai.core.verticals import VerticalConfig, get_vertical
from recoveryai.db.models import Case

logger = logging.getLogger(__name__)

#: How long a case waits after the model could not decide it.
#:
#: Short on purpose. Quota and transport failures usually clear within the
#: minute, and the follow-up cadence is tuned for "has the customer acted yet",
#: which is a different question from "can we ask the model yet".
DEFERRED_RETRY_SECONDS = 60.0


class UndecidableNow(RuntimeError):
    """The model could not decide this case right now, for any reason at all.

    Raised instead of substituting a decision. The agent is what decides here, so
    an unreachable or unusable model means there is no decision *yet* — not that
    something else quietly decides in its name. The case keeps its state, nothing
    is written to the trace, and it is tried again on the next sweep.

    The cost of this design is explicit: while the model is unavailable, cases do
    not progress. That is the trade made deliberately in exchange for a trace in
    which every recorded decision was genuinely the agent's.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class AgentDecision:
    """Everything one step produced. Returned to callers; never persisted directly."""

    case_id: str
    intent: ActionIntent
    result: ExecutionResult
    routing: RoutingDecision
    case_status: str
    step_number: int
    llm_call_made: bool


class RecoveryAgent:
    """Embeddable agent core. No web framework, no global state, no I/O it does not own.

    A host fintech constructs one of these, hands it a session factory and
    optionally its own `ActionExecutor`, and drives the whole system in-process::

        agent = RecoveryAgent(executor=MyExecutor())
        with session_scope() as s:
            agent.handle_event(s, event)
    """

    def __init__(
        self,
        settings: Settings | None = None,
        executor: ActionExecutor | None = None,
        llm: GovernedLLM | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.executor = executor or build_executor(self.settings)
        self.llm = llm if llm is not None else _build_llm(self.settings)
        self.notifier = notifier or build_notifier(self.settings)

    # ── Entry points ───────────────────────────────────────────

    def handle_event(
        self,
        session,
        event: RecoveryEvent,
        idempotency_key: str | None = None,
    ) -> tuple[Case, AgentDecision | None, bool]:
        """Ingest an event and take the first decision on it.

        Returns `(case, decision, created)`. A redelivered event returns the
        original case with `created=False` and **no** second decision — the
        whole point of idempotent ingestion is that the money gets worked once.
        """
        store = CaseStore(session)
        case, created = store.create_from_event(event, idempotency_key)
        if not created:
            logger.info(
                "duplicate event ignored",
                extra={"case_id": case.id, "idempotency_key": case.idempotency_key},
            )
            return case, None, False

        decision = self.advance_case(session, case)
        return case, decision, True

    def advance_case(self, session, case: Case) -> AgentDecision | None:
        """Take exactly one decision step on an existing case.

        Called on intake and again on every scheduled follow-up. Returns `None`
        when the case is already finished — a follow-up that fires after a case
        resolved is normal, not an error.
        """
        if case.status in {CaseStatus.resolved.value, CaseStatus.escalated.value, CaseStatus.abandoned.value}:
            return None

        store = CaseStore(session)
        event = store.to_event(case)
        vertical = get_vertical(case.vertical)
        capacity = store.capacity_for(case)
        step_number = case.step_count + 1

        diagnosis, rule_confidence, rule_reason = vertical.diagnoser(event)
        case.diagnosis = diagnosis

        allowed_actions = self._prune_tools(vertical, capacity, diagnosis)
        routing = self._route(
            vertical=vertical,
            event=event,
            diagnosis=diagnosis,
            rule_confidence=rule_confidence,
            rule_reason=rule_reason,
            step_number=step_number,
            allowed_actions=allowed_actions,
        )

        context = self._build_context(
            case=case,
            event=event,
            capacity=capacity,
            diagnosis=diagnosis,
            rule_confidence=rule_confidence,
            rule_reason=rule_reason,
            step_number=step_number,
            allowed_actions=allowed_actions,
        )

        try:
            proposed, params, reasoning, confidence, source, llm_call_made = self._decide(
                vertical=vertical,
                event=event,
                case=case,
                routing=routing,
                context=context,
                allowed_actions=allowed_actions,
                diagnosis=diagnosis,
                rule_confidence=rule_confidence,
                rule_reason=rule_reason,
            )
        except UndecidableNow as exc:
            # Nothing is recorded. A step written here would be a decision the
            # agent did not make, sitting in the audit trail under its name, and
            # the trace is the entire basis for trusting this system.
            self._defer(store, case, exc.reason)
            return None

        final_action, verdict, guardrail_reason = self._apply_guardrails(
            vertical=vertical, proposed=proposed, capacity=capacity, diagnosis=diagnosis
        )

        # A redirected action discards the model's parameters — they described the
        # action the guardrail just refused, so a discount's copy must not survive
        # onto the nudge that replaced it. `attach_draft` then writes template copy
        # for whatever the case actually landed on.
        final_params = dict(params) if final_action == proposed else {}
        final_params = attach_draft(final_action, event, final_params)

        intent = ActionIntent(
            case_id=case.id,
            step_number=step_number,
            vertical=event.vertical,
            customer_id=case.customer_id,
            amount=case.amount,
            currency=case.currency,
            proposed_action=proposed,
            final_action=final_action,
            params=final_params,
            diagnosis=diagnosis,
            reasoning=reasoning,
            confidence=confidence,
            decision_source=source,
            guardrail_verdict=verdict,
            guardrail_reason=guardrail_reason,
            guardrail_version=GUARDRAIL_VERSION,
        )

        result = self._execute(intent, event)
        store.record_step(case, intent, result, context, llm_call_made)

        # Assessed *after* the step so the estimate reflects the capacity this
        # step just consumed — a case that has spent its last discount is worth
        # less to work than it was a moment ago, and the next follow-up should be
        # scheduled against what is actually left rather than what was available.
        prospects = assess(
            vertical=vertical,
            diagnosis=case.diagnosis,
            amount=case.amount,
            capacity=store.capacity_for(case),
            steps_taken=step_number,
        )
        case.expected_recovery = prospects.value
        self._transition(store, case, intent, result, step_number, vertical, prospects)

        logger.info(
            "case step decided",
            extra={
                "case_id": case.id,
                "step": step_number,
                "proposed_action": proposed,
                "final_action": final_action,
                "guardrail_verdict": verdict.value,
                "decision_source": source.value,
                "confidence": confidence,
                "status": case.status,
            },
        )

        return AgentDecision(
            case_id=case.id,
            intent=intent,
            result=result,
            routing=routing,
            case_status=case.status,
            step_number=step_number,
            llm_call_made=llm_call_made,
        )

    # ── Tool pruning ───────────────────────────────────────────

    def _prune_tools(
        self, vertical: VerticalConfig, capacity: GuardrailCapacity, diagnosis: str
    ) -> list[str]:
        """Drop tools the guardrail would reject anyway.

        Defence in depth, and better prompting: a model shown a discount it
        cannot have will sometimes pick it, and every such pick is a wasted call
        plus a redirect in the trace that teaches a reviewer nothing.
        """
        allowed = [
            name for name in vertical.tool_palette if vertical.guardrail(name, capacity, diagnosis)[0]
        ]
        if ESCALATE_TO_HUMAN not in allowed:
            # Escalation is unconditionally available; without it a fully
            # constrained case would have no legal move at all.
            allowed.append(ESCALATE_TO_HUMAN)
        return allowed

    # ── Routing governor ───────────────────────────────────────

    def _route(
        self,
        *,
        vertical: VerticalConfig,
        event: RecoveryEvent,
        diagnosis: str,
        rule_confidence: float,
        rule_reason: str,
        step_number: int,
        allowed_actions: list[str],
    ) -> RoutingDecision:
        """Every decision goes to the model. The rules inform it; they never replace it.

        This used to be a cost governor that answered most cases from a lookup
        table and spent the model only where a rule was unsure. That saved budget
        and produced a system whose *decisions* were mostly not made by the agent
        at all — a trace full of `decision_source: rule` is a workflow engine with
        an LLM attached, not an agent.

        So the rules keep their real job — diagnosis, confidence, and the
        deterministic safety net when the provider is unreachable — and lose the
        power to decide. `rule_diagnosis` and `rule_confidence` still ride along
        in the prompt as a prior the model is explicitly told it may disagree
        with.

        Two things this deliberately does *not* delegate, because a model cannot
        be the last word on either:

        * **Forced escalations.** Moved out of routing and into
          `policy.check_guardrails`, which is the layer a bad answer cannot talk
          its way around. Skipping the call was never what kept a disputed
          invoice away from an automated nudge — the guardrail is.
        * **Provider failure.** `_decide` still falls back to the policy table
          when the model is unreachable, and marks the step `fallback_*` so the
          trace says plainly that no model was consulted. Removing that would
          mean an outage stops recovery entirely rather than degrading it.
        """
        del event, step_number, allowed_actions  # every case routes to the model now
        base = {"rule_diagnosis": diagnosis, "rule_confidence": rule_confidence}

        if not self.llm.is_available():
            # Kept distinct in the trace: a reviewer reading "switched off" knows
            # the decision was a choice, not a missing deployment secret.
            why = (
                "LLM switched off; falling back to the policy table"
                if self.llm.is_configured()
                else "no LLM configured; falling back to the policy table"
            )
            return RoutingDecision(use_llm=False, reason=why, **base)

        return RoutingDecision(
            use_llm=True,
            reason=f"agent decides every case (rule prior: {diagnosis} @ {rule_confidence:.2f})",
            **base,
        )

    # ── Decision ───────────────────────────────────────────────

    def _decide(
        self,
        *,
        vertical: VerticalConfig,
        event: RecoveryEvent,
        case: Case,
        routing: RoutingDecision,
        context: dict[str, Any],
        allowed_actions: list[str],
        diagnosis: str,
        rule_confidence: float,
        rule_reason: str = "",
    ) -> tuple[str, dict[str, Any], str, float, DecisionSource, bool]:
        """Produce `(action, params, reasoning, confidence, source, llm_called)`.

        Raises `UndecidableNow` if the model cannot answer. There is no second
        decision-maker to hand off to: the agent decides or the case waits.
        """
        if not routing.use_llm:
            raise UndecidableNow(routing.reason)

        tools = vertical.tools(allowed_actions)
        cache_key = hash_input(
            vertical.name, diagnosis, case.ltv_tier, context["case"], context["prior_steps"]
        )

        try:
            call = self.llm.choose_tool(
                system_prompt=vertical.prompt,
                user_prompt=json.dumps(context, indent=2, default=str),
                tools=tools,
                timeout_seconds=self.settings.llm_timeout_seconds,
                cache_key=cache_key,
            )
        except LLMUnavailable as exc:
            # No second decision-maker. The agent is the thing that decides here,
            # so a model that cannot answer means there is no decision to record —
            # not that a lookup table quietly takes over under the agent's name.
            # The case keeps its state and is retried; nothing is written.
            logger.warning("no decision: model unavailable", extra={"reason": exc.reason})
            raise UndecidableNow(exc.reason) from exc

        action, params, reasoning, confidence = self._validate_call(call, vertical)
        return (
            action,
            params,
            reasoning,
            confidence,
            DecisionSource.llm_cached if self.llm.last_call_was_cached else DecisionSource.llm,
            not self.llm.last_call_was_cached,
        )

    def _validate_call(
        self, call: ToolCall, vertical: VerticalConfig
    ) -> tuple[str, dict[str, Any], str, float]:
        """Never trust the tool name straight from the wire.

        The line is drawn at this vertical's *full* palette, not the pruned
        subset actually offered. A model that picks a pruned tool has not
        hallucinated — it has proposed something the guardrail is about to
        refuse, and that deserves a real "proposed X, blocked for R, redirected
        to Y" trace rather than being recategorised as a malfunction. Pruning is
        a courtesy to the prompt; the guardrail immediately downstream is the
        control, and it re-checks every proposal regardless.

        A name outside the palette entirely — an invented tool, or one belonging
        to another vertical — is not a proposal the guardrail can adjudicate;
        there is no rule about a tool that does not exist here. It raises, and
        the case is retried rather than being handed a substitute decision that
        the model did not make.
        """
        if is_known_action(call.name) and call.name in vertical.tool_palette:
            return call.name, dict(call.arguments), call.reasoning, call.confidence

        logger.warning(
            "no decision: model returned an action outside the vertical palette",
            extra={"action": call.name, "vertical": vertical.name},
        )
        raise UndecidableNow(f"unusable_action:{call.name}")

    # ── Guardrails ─────────────────────────────────────────────

    def _apply_guardrails(
        self, *, vertical: VerticalConfig, proposed: str, capacity: GuardrailCapacity, diagnosis: str
    ) -> tuple[str, GuardrailVerdict, str | None]:
        """Check the proposal and, if refused, redirect deterministically.

        This is the check that actually matters — pruning in `_prune_tools` is
        only a courtesy to the prompt. A model can still be handed a diagnosis
        that makes a discount unjustified and choose it anyway; this call is
        what stops that from ever reaching the executor.

        No second model call on a block. The fallback chain is fixed per vertical
        precisely so that a refusal has a predictable, auditable consequence
        instead of another round of negotiation with the model.
        """
        allowed, reason = check_guardrails(vertical.name, proposed, capacity, diagnosis)
        if allowed:
            return proposed, GuardrailVerdict.allowed, None

        for candidate in vertical.fallback_chain:
            if check_guardrails(vertical.name, candidate, capacity, diagnosis)[0]:
                return candidate, GuardrailVerdict.blocked, reason

        # Every vertical's chain ends in escalation, which is never blocked, so
        # this is unreachable by construction — kept as a fail-safe rather than
        # trusting that every future vertical config gets the invariant right.
        return ESCALATE_TO_HUMAN, GuardrailVerdict.blocked, reason

    # ── Execution and lifecycle ────────────────────────────────

    def _execute(self, intent: ActionIntent, event: RecoveryEvent) -> ExecutionResult:
        try:
            return self.executor.execute(intent, event)
        except Exception as exc:
            # The protocol says executors do not raise; a third-party one might.
            # Losing the decision because its delivery failed would be the worse
            # outcome, so the failure is recorded and the case continues.
            logger.exception("executor raised", extra={"intent_id": str(intent.intent_id)})
            return ExecutionResult(
                status=ActionStatus.error,
                details=f"executor raised {type(exc).__name__}",
                executor=getattr(self.executor, "name", "unknown"),
            )

    def _transition(
        self,
        store: CaseStore,
        case: Case,
        intent: ActionIntent,
        result: ExecutionResult,
        step_number: int,
        vertical: VerticalConfig,
        prospects: Prospects,
    ) -> None:
        """Move the case along and decide whether, and how soon, it gets another look."""
        if result.recovered_amount > 0:
            # Money arriving ends the case, whichever lever brought it in. Checked
            # before anything else because a paid invoice is not escalated, not
            # abandoned, and not owed another step.
            store.set_status(case, CaseStatus.resolved)
            return

        if intent.final_action == ESCALATE_TO_HUMAN:
            store.set_status(case, CaseStatus.escalated)
            self._announce_escalation(
                case, intent, intent.guardrail_reason or "the agent asked for a human"
            )
            return

        if intent.final_action == CLOSE_AS_UNRECOVERABLE:
            store.set_status(case, CaseStatus.abandoned)
            return

        if step_number >= vertical.max_steps:
            # "Bounded recovery workflow" means bounded. An agent that can work a
            # case forever is an agent that will, and a human should own what is
            # left rather than the loop quietly continuing to spend.
            store.set_status(case, CaseStatus.escalated)
            case.diagnosis = case.diagnosis or "step_limit_reached"
            self._announce_escalation(
                case, intent, f"step limit reached ({vertical.max_steps} steps, unresolved)"
            )
            return

        if not prospects.worth_pursuing:
            # Every remaining lever is expected to cost more than it recovers.
            # Continuing would be spending real money to chase less of it, which
            # is a decision worth recording rather than a loop worth running.
            store.set_status(case, CaseStatus.abandoned)
            logger.info(
                "case abandoned on economics",
                extra={
                    "case_id": case.id,
                    "expected_recovery": prospects.value,
                    "next_action_cost": prospects.cost,
                },
            )
            return

        store.set_status(case, CaseStatus.in_progress)
        if result.status != ActionStatus.error:
            store.schedule_followup(case, self._followup_delay(prospects))

    def _followup_delay(self, prospects: Prospects) -> float:
        """How long before this case is looked at again.

        The configured delay is what an ordinary case gets; the band scales it.
        Attention is finite, so a case with real money still recoverable earns
        more of it than one worth a few hundred rupees — expressed as *how often
        the agent comes back*, which is the only lever that actually changes
        behaviour when there is no backlog to reorder.
        """
        _label, multiplier = band(prospects.value)
        return self.settings.followup_delay_seconds * multiplier

    def _defer(self, store: CaseStore, case: Case, reason: str) -> None:
        """Put the case back in the queue, unchanged, to be decided later.

        Deliberately short — a model that is rate-limited is usually available
        again within the minute, and a case that waits four hours because the
        provider hiccuped is a worse outcome than one retried promptly. The case
        keeps its status and its step count; only its next check-in moves.
        """
        when = store.schedule_followup(case, DEFERRED_RETRY_SECONDS)
        logger.info(
            "case deferred, no decision recorded",
            extra={"case_id": case.id, "reason": reason, "retry_at": when.isoformat()},
        )

    def _announce_escalation(self, case: Case, intent: ActionIntent, reason: str) -> None:
        """Tell a human. Never let the telling undo the escalation itself."""
        try:
            self.notifier.case_escalated(case=case, intent=intent, reason=reason)
        except Exception:
            # The protocol says notifiers do not raise; a third-party one might.
            logger.exception("notifier raised", extra={"case_id": case.id})

    # ── Prompt context ─────────────────────────────────────────

    def _build_context(
        self,
        *,
        case: Case,
        event: RecoveryEvent,
        capacity: GuardrailCapacity,
        diagnosis: str,
        rule_confidence: float,
        rule_reason: str,
        step_number: int,
        allowed_actions: list[str],
    ) -> dict[str, Any]:
        """The full decision context — also the audit snapshot.

        One structure serves both the prompt and `case_steps.context_snapshot`,
        which is what makes a stored decision genuinely reproducible: the record
        shows precisely what the model was looking at, not an approximation
        rebuilt afterwards.
        """
        prior_steps = [
            {
                "step": s.step_number,
                "action": s.final_action,
                "proposed": s.proposed_action,
                "guardrail": s.guardrail_verdict,
                "guardrail_reason": s.guardrail_reason,
                "outcome": s.action_status,
                # Fenced: free text written by an earlier model turn, which may
                # itself be quoting the untrusted input it was reasoning about.
                # Unfenced, a case's own history becomes a laundering route for
                # an injection that was correctly fenced on the step that saw it.
                "reasoning": fence(s.reasoning),
                "at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in sorted(case.steps, key=lambda s: s.step_number)
        ]

        return {
            # Standing instruction for the fenced spans below. First key in the
            # structure so the model reads the rule before the data it governs.
            "data_handling": UNTRUSTED_DATA_NOTICE,
            "case": {
                "case_id": case.id,
                "vertical": case.vertical,
                "customer_id": case.customer_id,
                "customer_ltv_tier": case.ltv_tier,
                "amount_at_risk": case.amount,
                "currency": case.currency,
                # Fenced: both fields are free-form by contract, which makes them
                # the two places an outside party controls prompt bytes.
                "raw_failure_reason": fence(case.raw_failure_reason) or "(none supplied)",
                "signals": fence_mapping(case.vertical_metadata),
                "opened_at": case.created_at.isoformat() if case.created_at else None,
            },
            "rule_based_prior": {
                "diagnosis": diagnosis,
                "confidence": rule_confidence,
                # Fenced even though we compose this string ourselves: the
                # diagnosers quote the gateway code back verbatim ("unrecognised
                # gateway code 'X'"), so the same attacker-controlled bytes
                # reappear here. Fencing only the original field would leave an
                # unfenced copy one key away, which is no mitigation at all.
                "explanation": fence(rule_reason),
                "note": "A cheap heuristic, not ground truth. Disagree with it if the signals warrant.",
            },
            "step_number": step_number,
            "max_steps": get_vertical(case.vertical).max_steps,
            "prior_steps": prior_steps,
            "guardrail_headroom": {
                **capacity.remaining_for(case.vertical),
                "note": (
                    "Hard limits enforced in code. Anything you choose beyond them is "
                    "blocked and redirected, so choosing it wastes the step."
                ),
            },
            "available_actions": allowed_actions,
            "intake_priority": intake_priority(event),
        }

    # ── Introspection ──────────────────────────────────────────

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "agent_mode": self.settings.agent_mode,
            "executor": getattr(self.executor, "name", "unknown"),
            "max_steps_by_vertical": {
                name: get_vertical(name).max_steps for name in ("cart", "b2b", "autopay")
            },
            "followup_delay_seconds": self.settings.followup_delay_seconds,
            "known_actions": list(ALL_ACTIONS),
            "llm": self.llm.snapshot(),
        }


def _build_llm(settings: Settings) -> GovernedLLM:
    """Wire the configured provider behind the governance layer.

    The budget counter shares the application database file rather than a
    sidecar, so "how much model budget is left" survives restarts and is visible
    to anyone with the DB — including the ops dashboard.
    """
    provider = GeminiProvider(api_key=settings.gemini_api_key, model=settings.llm_model)
    db_path = settings.resolved_database_url.replace("sqlite:///", "")
    budget: DailyBudgetCounter | None
    try:
        budget = DailyBudgetCounter(db_path, max_per_day=settings.llm_max_rpd)
    except Exception:
        # A non-SQLite database, or an unwritable path. Rate limiting still
        # applies; only the persistent daily cap is lost, which is a degradation
        # worth taking over refusing to start.
        logger.warning("daily budget counter unavailable; rate limiter still active")
        budget = None

    return GovernedLLM(
        provider,
        rate_limiter=RateLimiter(max_per_minute=settings.llm_max_rpm, max_per_day=settings.llm_max_rpd),
        budget=budget,
    )
