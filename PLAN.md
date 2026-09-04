# RecoveryAI — Production-Grade, Integration-Ready AI Agent

> **Build target: `C:\Users\saish\Downloads\RecoveryAI v2`.** All new work goes here.
> The older `recoverAI v2` / `RecoverAI` folders are read-only reference — port the reusable
> pieces named under "What's reused vs. rebuilt", but never write to them.

## Locked-in build decisions

These refine the architecture below and take precedence where they differ:

1. **Namespaced package.** Everything lives under `recoveryai` (`from recoveryai.core.agent import ...`),
   not bare top-level `core` / `db` / `api`. A generic `import core` would collide inside a host
   fintech's codebase — the whole point is that this drops into someone else's system cleanly.
2. **`raw_failure_reason` is a plain `str`, not an enum.** Real gateways emit arbitrary codes, and
   enum-ing this field caused genuine crashes in the previous build. Unknown codes must route to the
   agent as ambiguous — that is correct behaviour, not an error path.
3. **Provider-adapter layout for the LLM.** `core/llm/base.py` (provider protocol, `ToolCall` type,
   `LLMUnavailable` error), `core/llm/gemini.py` (Gemini adapter), `core/llm/governance.py`
   (rate limiter + daily budget + cache, wrapping *any* provider). Swapping to an enterprise LLM is
   then a new adapter file, not a rewrite.
4. **`recoveryai/core/` has zero FastAPI imports**, enforced by a test. `recoveryai/api/` is the only
   place a web framework may appear.

## Progress

- [x] `pyproject.toml`, `.env.example`, `recoveryai/__init__.py`, `recoveryai/core/settings.py`
- [x] Phase 1 — backend foundation (models, db, llm adapters, tools)
- [x] Phase 2 — agent core + execution seam · core/API boundary enforced by test
- [x] Phase 3 — API + scheduler + hardening · idempotent ingest, HMAC middleware, auth, envelope, WS
- [x] Phase 4 — frontend console · 5 pages, verified live in-browser against the running backend
- [x] Phase 5 — polish + deployment · 182 tests green, ruff clean, `docker compose up` healthy

**Build complete.** See [LIMITATIONS.md](LIMITATIONS.md) for what is not done —
most importantly, no live LLM call has been made (no API key was available), so
the Gemini adapter's wire format is unverified against the real SDK.

## Reporting rule — LIMITATIONS.md is mandatory

Maintain `LIMITATIONS.md` in this folder throughout the build, not just at the end. Anything that
is **not** fully built, fully working, and verified goes in it — as you hit it, not retroactively.

That includes: parts of this plan deliberately simplified or descoped; features stubbed, mocked, or
faked; anything untested or only partially tested; known bugs shipped knowingly; anything that
works in the demo but would not survive production traffic; and any place where a dependency,
platform limit, or missing credential blocked real implementation.

For each entry, record: **what** is incomplete, **why** (time, blocker, deliberate trade-off), and
**what "done" would require**. One honest sentence each is enough.

Never silently drop a piece of scope, and never describe something as working when it is stubbed —
in the final summary, state plainly what was completed vs. what landed in LIMITATIONS.md.

## Context

The current build (just finished bug-fixing) is honestly a rules engine, not an agent: the LLM only ever classifies ~5% of ambiguous cases into a label, and a hardcoded `(diagnosis, ltv_tier) -> action` lookup table makes every actual decision. The "3 agents" are just `if/elif` branches. The user was told to build **an AI agent** for this brief:

> "AI Revenue Recovery — Find revenue that's slipping away and win it back. Build an agent that detects revenue at risk, determines the right intervention, and executes a bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables."

This is being built as a **prototype to demo to an interviewer**, with a specific bar: it must be built so that **the frontend can be dropped entirely and the agent core integrated directly into an existing fintech's system** — either as a Python library import, or over a webhook/REST API for non-Python hosts. That requirement shapes the architecture more than anything else below: the agent must *decide*, not *execute* — a real fintech has its own notification and payment infrastructure, and the agent's job is to hand off a well-reasoned, guardrail-checked decision, not to fake-send things itself. LLM access for this prototype is Gemini, but the real deployment runs on the interviewer's enterprise-grade LLM — governance is provider-agnostic and config-driven, not hardcoded to one provider's limits. The user wants the **entire platform top-notch**: security posture, observability, testing, deployment polish, not just the agent loop. Deadline: Friday evening. Implementation will be executed via Opus-backed subagents given the quality bar.

**The core fix:** make the LLM actually choose the action via tool/function-calling, with every proposed action passing through hard, code-level guardrails it cannot bypass — "autonomous but governed" — give cases a real multi-step lifecycle instead of one-shot fire-and-forget, and cleanly separate *what the agent decided* from *how it gets carried out* so the whole thing is embeddable, not just demoable.

## Architecture

**Agent loop — hand-rolled, no LangChain/CrewAI.** Auditability, latency, no black-box — and it's what keeps the governance layer provider-agnostic instead of fighting a framework's own call patterns. Uses native function-calling: the model is given a `Tool` (per vertical) and forced (`mode: "ANY"`) to emit a function call — never free text to hand-parse. Each tool's schema requires `reasoning` and `confidence` as parameters, so structured rationale comes back in the same call, no second turn needed.

**One LLM call per case-decision, not a multi-turn loop.** All relevant context (case data, prior steps, remaining guardrail capacity) goes into a single prompt; the model reasons and picks a tool in one shot. Good latency/cost practice regardless of provider — a ReAct-style multi-turn loop multiplies calls for no real gain here.

**Cheap routing before any LLM call.** Rule-based diagnosis (existing `diagnosis.py` logic, kept) resolves the obvious majority of cases at zero cost and zero latency. The agent loop is invoked for genuinely ambiguous signals, second-or-later reconsiderations of an existing case, and judgment-heavy first actions. If a customer's guardrail capacity is already exhausted, skip the LLM entirely and go straight to escalation — the outcome is forced either way.

**LLM rate/cost governance — provider-agnostic, config-driven.** `RateLimiter`/`DailyBudgetCounter` (`llm_client.py`) preserved architecturally; limits move from hardcoded Gemini-free-tier numbers to typed settings (`LLM_MAX_RPM`, `LLM_MAX_RPD`). On exhaustion, fall back to `policy.py`'s deterministic decision tables — same "never crashes without a key" guarantee, graceful degradation independent of budget size or provider.

**Guardrails intercept every proposed action, deterministically, always.** Before calling the model, prune the offered tools to exclude ones the guardrail would reject anyway (e.g. don't offer `send_discount` if this customer got one in the last 90 days) — defense in depth. After the model responds, run its choice through `check_cart_guardrails` / `check_b2b_guardrails` / `check_autopay_guardrails` (kept as-is). If blocked, redirect deterministically to a safe fallback action (no second LLM call) and log the full story: *"agent proposed X, guardrail blocked it (reason), system redirected to Y."*

**Cases are stateful, not one-shot.** A `Case` has a lifecycle (`new → in_progress → resolved | escalated | abandoned`) and a history of prior steps. APScheduler schedules a follow-up re-evaluation after a short delay (compresses "3 days later" into ~60-90s of demo time). The existing guardrail thresholds (max 1 discount/90d, max 3 B2B touches, max 3 autopay retries) become the natural step limits of this workflow.

**Risk-weighted priority score.** One computed field, `priority_score`, drives two things that would otherwise default to naive FIFO:
- Before a decision exists: `priority_score = amount × domain_urgency_heuristic` — orders the **case processing queue**, so highest-value at-risk revenue gets worked first when there's more open work than immediate throughput.
- Once a decision has a confidence score: `priority_score = (1 − confidence) × amount` — orders the **human review queue**, so a $40k case the agent was unsure about outranks a $200 case that only escalated because it hit a guardrail limit.

### Decision/execution separation — the actual integration seam

This is the piece that makes "drop the frontend, integrate the agent" literally true rather than aspirational:

- The agent's output is an **`ActionIntent`** — `{action, params, reasoning, confidence, guardrail_verdict, final_action}` — fully decided and guardrail-checked, but not yet carried out.
- Execution goes through an `ActionExecutor` protocol (`backend/core/execution.py`), with two shipped implementations:
  - **`SimulatedExecutor`** (default) — today's behavior, `[SIMULATED]` tag, so the standalone demo works with zero external dependencies. Internally reuses the existing `Action` subclasses from `actions.py` — they become the simulator's implementation detail, not the system's final word.
  - **`WebhookExecutor`** — POSTs the signed `ActionIntent` to a configured `ACTION_WEBHOOK_URL`. The host either responds synchronously with a result, or reports outcome asynchronously via `POST /api/v1/actions/{id}/report`. A Python host can instead implement `ActionExecutor` directly and inject it — no fork required.
- **Shadow mode** (`AGENT_MODE=shadow|live`, typed setting): in shadow mode the agent makes and fully logs every decision — complete trace, guardrail checks, everything — but every action resolves to `shadow_logged` instead of executing anywhere. This is the actual answer to "how would a fintech trust this before going live": pilot it against real traffic in shadow mode first, flip to live once confidence is established.

### Idempotent, integration-ready ingestion

- `POST /api/v1/events` dedupes by `event_id` (or an `Idempotency-Key` header) — real webhook senders retry on timeout/ambiguous response, so a duplicate delivery must not create a duplicate case.
- `RecoveryEvent` is the stable, documented webhook payload contract (OpenAPI schema + examples), not an internal implementation detail.
- Inbound HMAC signature verification (stdlib `hmac`/`hashlib`, header-configurable name/secret) — the pattern a real fintech webhook (Razorpay-style) requires, implemented for real this time rather than left as a documented gap.

### Core/API boundary — library usability, not just HTTP

`backend/core/` (agent loop, guardrails, tools, models, execution) has **zero FastAPI imports** — it must be usable as a plain Python package by another backend, not only reachable over HTTP. `backend/api/` is purely the transport layer on top of it. Config surface is one typed `Settings` object (`pydantic-settings`) — `LLM_MAX_RPM`, `LLM_MAX_RPD`, `ACTION_WEBHOOK_URL`, `AGENT_MODE`, `API_KEYS`, `WEBHOOK_SIGNING_SECRET` — instead of `os.environ.get(...)` scattered through the codebase.

**Real persistence.** SQLAlchemy backs two tables in `db/recoverai.db`:
- `cases` — id, vertical, customer_id, ltv_tier, amount, status, priority_score, raw_failure_reason, vertical_metadata (JSON), amount_recovered, cost_of_recovery, step_count, timestamps, next_followup_at, idempotency_key
- `case_steps` — case_id, step_number, decision_source (`rule`/`llm`/`llm_cached`/`fallback_budget`/`fallback_schema`), context_snapshot (JSON, full audit trail), proposed_action, reasoning, confidence, guardrail_verdict, guardrail_reason, final_action, action_status (`executed`/`blocked`/`scheduled`/`escalated`/`error`/`shadow_logged`/`pending_host_execution`), cost, recovered_amount, llm_call_made, timestamp

Plain string columns, not DB-level enums, validated at the Pydantic boundary — the simulator enum-mismatch crashes from this session are exactly the bug class to not reintroduce at the ORM layer. `PRAGMA journal_mode=WAL` at connect time since raw sqlite3 (budget counter) and SQLAlchemy both write to the same file. Schema managed via Alembic migrations, not `create_all()`.

### Frontend — real console, droppable by design

Built as a genuinely separate consumer of the API — nothing in `backend/` depends on the frontend existing, which is the actual proof that it can be dropped. The current hand-rolled CSS-class approach is replaced with a real component foundation:
- **shadcn/ui (Radix primitives + Tailwind)** for every interactive component
- **lucide-react** for icons
- **TanStack Table** for the Case Queue and Review Queue — real sort/filter/pagination
- **TanStack Query** for all data fetching, with the WebSocket pushing cache invalidations
- **Motion (Framer Motion)** for live-update micro-interactions — new cases animating into the queue, trace steps revealing as they stream in
- **Typography**: Inter/Geist for UI chrome, a monospace face (JetBrains Mono / IBM Plex Mono) for the agent's reasoning text in the trace timeline — visually marks it as raw model output
- Existing dark theme's color tokens migrate into shadcn's CSS-variable convention

`react-router-dom` for routing. Pages: `/` Dashboard, `/cases` Case Queue (priority-ordered), `/cases/:id` Case Detail + Trace Timeline (flagship view), `/review` Human Review Queue (uncertainty/value-ordered), `/dev` Dev Tools (manual inject + simulator controls, demoted from centerpiece).

## Production-grade hardening

- **Security:** CORS locked to a configured allow-list, not `allow_origins=["*"]`; API-key auth middleware on write endpoints (typed, multi-key-ready, not a single hardcoded token); inbound webhook HMAC verification; outbound `ActionIntent` payloads signed the same way; no secrets ever logged.
- **API design:** versioned surface (`/api/v1/...`), pagination on list endpoints, consistent error envelope (`{error: {code, message}}`), `/health` and `/api/v1/system/status`, full OpenAPI descriptions/examples — this doubles as the integration contract a fintech's engineers would actually read.
- **Observability:** structured JSON logging throughout; `case_steps` is the explainability/audit log; system status endpoint surfaces rate-governance + scheduler health.
- **Resilience:** WS client reconnection with exponential backoff; scheduler jobs get `misfire_grace_time` and per-job error isolation; DB session handling via FastAPI `Depends` with proper commit/rollback boundaries; idempotent ingestion (above).
- **Config:** one typed `Settings` object (`pydantic-settings`), documented `.env.example`.
- **Testing:** unit tests for the agent loop, routing governor, guardrail redirect, both priority-score formulas, the executor swap (fake executor proves the seam works), shadow mode (proves nothing dispatches), and idempotent ingestion (duplicate event → one case). All runnable with zero API key and zero network calls.
- **Deployment polish:** `Dockerfile` for backend and frontend, `docker-compose.yml`, README describing the architecture, run instructions, and explicit "integrate this into your system" instructions (implement `ActionExecutor` or point `ACTION_WEBHOOK_URL` at your endpoint; swap `LLM_MAX_RPM`/`LLM_MAX_RPD` and the model client for your enterprise LLM).

## What's reused vs. rebuilt

**Kept, generalized:** `RateLimiter`, `DailyBudgetCounter` (`llm_client.py`); all three guardrail checker functions and decision tables (`policy.py`); all `Action` subclasses (`actions.py`) — repurposed as `SimulatedExecutor`'s internals; the dark theme CSS tokens; the simulator's raw event-generation pools; most of `test_rate_limiter.py` and `test_guardrails.py`.

**Adapted:** `diagnosis.py`'s rule branches become the zero-cost routing hint; `backend/verticals/*.py` become per-specialist config exporters (system prompt + tool palette + guardrail + fallback chain + urgency heuristic); the simulator's ambiguous-fraction math generalizes into the routing governor.

**Retired:** in-memory guardrail-tracking dicts in `AppState`; `process_event_pipeline()`; manual-injector-as-centerpiece UI; duplicate WebSocket connections; open CORS wildcard; `Action.execute()` as the system's final word (now `SimulatedExecutor`'s internal detail behind the `ActionExecutor` seam).

**Known test breakage, handled explicitly:** `test_pipeline_integration.py`'s three guardrail tests assert bare `action_status == "blocked"`; under the new redirect behavior these get rewritten to assert the redirect.

## Build sequence

1. **Backend foundation** — `db/models.py`, `db/session.py` (WAL, Alembic init), `core/settings.py` (pydantic-settings), `core/tools.py` (function declarations per vertical), extend `LLMAbstractClient` with `decide_action()`. Verify with a monkeypatched model — no real API calls yet.
2. **Agent core + execution seam** — `core/agent.py` (routing governor, guardrail interception + redirect chains, budget short-circuit, `priority_score`), `core/execution.py` (`ActionExecutor` protocol, `SimulatedExecutor`, `WebhookExecutor`, shadow-mode handling), `core/cases.py` (DB-backed guardrail capacity, lifecycle transitions), repurpose `backend/verticals/*.py`. Rewrite the three breaking integration tests here; add executor-swap and shadow-mode tests. **Verify `core/` imports cleanly with zero FastAPI in the import graph.**
3. **API + scheduler + hardening** — versioned REST surface incl. idempotent `/api/v1/events`, priority-ordered `/api/v1/cases` and `/api/v1/review/queue`, `POST /api/v1/actions/{id}/report`, `core/scheduler.py`, inbound webhook HMAC verification, CORS allow-list + API-key middleware, error envelope, `/health`.
4. **Frontend rebuild** — router shell, shadcn/ui setup, TanStack Query/Table, shared `WsProvider` with reconnect/backoff, then Dashboard, Case Queue, Case Detail + Trace Timeline, Review Queue, Dev Tools.
5. **Polish + deployment** — full pytest run, Dockerfiles + compose, README/LIMITATIONS rewrite covering architecture, integration instructions, and the "swap the LLM" story, error/empty states, dead-code cleanup.

## Testing without depending on a live LLM

`decide_action` returns a deterministic-fallback signal immediately if no API key is set, before touching the provider SDK — the full suite runs key-free and network-free. Monkeypatch the model client with a scripted function-call response for real coverage, plus a `FakeLLMAbstractClient` and a `FakeActionExecutor` for isolating routing/guardrail/priority/execution logic independently. Priority tests: guardrail redirect fires regardless of model proposal; budget/rate exhaustion falls back cleanly; both `priority_score` formulas rank correctly against fixtures; shadow mode never calls a real executor; duplicate event ingestion produces exactly one case. One real LLM smoke call per vertical, run by hand before the actual demo.

## Verification

- `pytest tests/ -q` — full suite green
- `python -c "from core.agent import ..."` — confirm `core/` imports with no FastAPI dependency, proving library-usability
- Manual backend run, no frontend: drive the full flow via `curl`/`/docs` alone — ingest a case, get a decision, confirm guardrail redirect, confirm a scheduled follow-up fires, confirm queue ordering reflects `priority_score`, confirm shadow mode logs without executing, confirm a duplicate event doesn't duplicate a case
- Frontend: `npm run build` clean, click through Dashboard → Case Queue → Case Detail trace timeline → Review Queue, confirm live WS updates append without refresh
- `docker compose up` brings up the full stack from a clean checkout
