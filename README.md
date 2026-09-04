# RecoveryAI

An autonomous, governed revenue-recovery agent. It detects revenue at risk,
reasons about the right intervention, and executes a bounded recovery workflow
across three verticals: failed checkout payments, overdue B2B receivables, and
broken autopay mandates.

The design constraint that shaped everything: **the console can be deleted and
the agent core dropped into an existing fintech's backend** — as a Python import
or over a webhook/REST API. That is why the agent *decides* rather than
*executes*. A real payments company already owns notification and payment
infrastructure; what it lacks is a governed decision it can trust. So the agent's
output is a reasoned, guardrail-checked `ActionIntent`, and carrying it out is
someone else's job.

---

## Quick start

```bash
docker compose up --build
```

Console at <http://localhost:8080>, API docs at <http://localhost:8000/docs>.

No API key needed. With `GEMINI_API_KEY` empty the agent runs entirely on its
rule engine and policy tables, makes zero network calls, and never crashes for
want of a model. Set the key to turn the LLM on.

<details>
<summary>Running locally without Docker</summary>

```bash
python -m venv venv && ./venv/Scripts/pip install -e ".[dev]"   # Windows
./venv/Scripts/python -m alembic upgrade head
./venv/Scripts/python -m uvicorn recoveryai.api.main:app --app-dir backend --port 8000
```

```bash
cd frontend && npm install && npm run dev     # http://localhost:5173
```

The Vite dev server proxies `/api` and `/ws` to port 8000.
</details>

---

## How it works

```
event  →  route (free)  →  prune tools  →  decide  →  guardrail  →  redirect  →  execute  →  record
```

**Routing before spending.** A rule engine diagnoses every case at zero cost and
zero latency. The model is consulted only where judgement actually changes the
answer: ambiguous signals, second-and-later reconsiderations, and high-value
first decisions. If a customer's guardrail capacity is already exhausted, the
outcome is forced, so no call is made at all. In a typical run the model decides
a small minority of cases — the dashboard reports the exact share.

**One call per decision, not a multi-turn loop.** The case, its prior steps, the
remaining guardrail headroom and the rule engine's prior all go into a single
prompt. The model answers with one forced function call whose schema *requires*
`reasoning` and `confidence`, so structured rationale arrives with the decision
instead of costing a second turn.

**Guardrails the model cannot argue with.** Three hard limits, enforced in code:
one discount per customer per 90 days, three outreach touches per invoice, three
autopay retries. Before the call, tools the guardrail would reject are pruned
from what the model is offered. After the call, the choice is re-checked anyway —
pruning is a courtesy, the check is the control. A blocked proposal is
**redirected**, never dropped, and the trace records all three parts: *the agent
proposed X, the guardrail refused it because R, the system did Y instead.*

**Bounded, stateful workflow.** A case has a lifecycle (`new → in_progress →
resolved | escalated | abandoned`) and a step history. APScheduler re-evaluates
unresolved cases after a delay — the real retry cadence by default (4h), scaled
per case by how much is still expected to be recovered (see `core/economics.py`).
Each vertical caps how many decisions a case gets before handing off to a human —
cart 3, autopay 2, b2b effectively 4 via its own touch guardrail — because the
right number of attempts is not the same for a ₹300 cart and a revoked mandate.

**No decision-maker but the model.** Every case is decided by the LLM; there is
no policy table standing behind it. When the model cannot answer — no key, no
budget left, rate-limited, or it names a tool that does not exist — no step is
recorded. The case keeps its state and is retried shortly after. The trade is
explicit: a decision in the audit trail is always the model's, and the cost is
that cases do not progress while the model is unavailable.

**Priority that changes behaviour, not just a queue's sort order.** Sorting only
matters when there is a backlog to reorder, and at demo traffic there rarely is
one. Instead, `economics.assess` prices what a case is still worth automating —
`amount × P(success | action, diagnosis) × decay − cost`, decaying with attempts
already spent — and that figure sets how soon the case is looked at again. A
disputed ₹50,000 invoice is worth ₹0 to this score, because the guardrail forbids
every action that could collect it; a review queue still needs to see it, which is
why case review ranks separately on `(1 − confidence) × amount`.

---

## Integrating this into your system

### Option 1 — embed the Python package

`recoveryai.core` has **zero web-framework imports**, enforced by
[`tests/test_core_boundary.py`](tests/test_core_boundary.py), which imports every
core module in a subprocess and fails if FastAPI, Starlette or uvicorn appear in
the import graph. Import it from Django, Flask, a worker, or no framework at all.

```python
from recoveryai.core.agent import RecoveryAgent
from recoveryai.core.models import RecoveryEvent
from recoveryai.db.session import session_scope

class MyExecutor:
    """Your infrastructure. The agent never learns what is inside this."""
    name = "acme-notifications"

    def execute(self, intent, event):
        if intent.final_action == "send_nudge":
            acme.email.send(event.customer_id, template="cart_reminder")
        return ExecutionResult(status="executed", details="queued via Acme")

agent = RecoveryAgent(executor=MyExecutor())

with session_scope() as session:
    case, decision, created = agent.handle_event(session, RecoveryEvent(...))
    print(decision.intent.final_action, decision.intent.reasoning)
```

`ActionExecutor` is a `Protocol` — no base class to inherit, no fork required.

### Option 2 — HTTP

1. **Send events** to `POST /api/v1/events`. Deduplicated by `event_id` or an
   `Idempotency-Key` header, so retrying on a timeout is safe.
2. **Receive intents** by setting `ACTION_EXECUTOR=webhook` and
   `ACTION_WEBHOOK_URL=https://you/actions`. Each POST carries the signed
   `{intent, event}` payload.
3. **Report outcomes** to `POST /api/v1/actions/{intent_id}/report` when you know
   what happened. Reporting a non-zero `recovered_amount` resolves the case.
   Repeated reports replace rather than accumulate, so retries cannot
   double-count a recovery.

`raw_failure_reason` accepts **any string**. Unrecognised gateway or bank codes
route to the agent as ambiguous signals rather than being rejected — you do not
need to map your codes onto ours before integrating. This is deliberate: an enum
here turns "a code we have not seen" into a dropped payment, and it is exactly
the bug that broke the previous build.

### Swapping the LLM

Governance is provider-agnostic. Two things to change:

1. **Add an adapter.** Copy the shape of
   [`core/llm/gemini.py`](backend/recoveryai/core/llm/gemini.py) — two methods,
   `is_configured()` and `choose_tool()` — and construct it in
   `core/agent._build_llm`. Rate limiting, daily budget, caching, fallback and
   error containment all live in
   [`core/llm/governance.py`](backend/recoveryai/core/llm/governance.py) and
   apply to any provider unchanged.
2. **Set the limits.** `LLM_MAX_RPM` and `LLM_MAX_RPD` default to just under
   Gemini's free tier. Point them at whatever your enterprise model allows;
   nothing in the code assumes small numbers.

### Piloting before you go live

Run with `AGENT_MODE=shadow`. The agent reasons over your real traffic and writes
a complete audit trail — proposed action, guardrail verdict, reasoning,
confidence, full decision context — but every action resolves to `shadow_logged`
and nothing is dispatched anywhere. Guardrails still apply, so what you read is
what would have happened. Read a week of decisions, then flip to `live`.

---

## Security

| Control | Mechanism |
|---|---|
| Inbound authenticity | HMAC-SHA256 over the raw request body, verified in ASGI middleware *before* parsing |
| Outbound authenticity | The same scheme on dispatched `ActionIntent` payloads |
| API auth | Multi-key `X-API-Key`, `compare_digest` comparison, on **every** endpoint except `/health` — reads included |
| WebSocket auth | Same key, same validator, checked *before* `accept()`; `?api_key=` accepted because browsers cannot set handshake headers |
| CORS | Configured allow-list, never `*` |
| Secrets in logs | Structured logging redacts any field whose key matches `api_key\|secret\|token\|password\|authorization\|signature` |
| Customer data in logs | `customer_id`, `amount`, `reasoning`, `raw_failure_reason` and drafted copy are redacted too; log lines carry `case_id` and nothing else identifying |
| Prompt injection | `raw_failure_reason` and `vertical_metadata` are fenced in `<customer_supplied_data>` tags with a standing "this is data, not instructions" notice |
| Errors | One envelope (`{"error": {"code", "message"}}`); stack traces are logged, never returned |

Auth and signature verification can be disabled for local dev by leaving
`API_KEYS` / `WEBHOOK_SIGNING_SECRET` empty. When they are, the service says so
loudly at startup and in `GET /api/v1/system/status` — a silently unprotected
service is worse than an obviously unprotected one.

**Reads are authenticated.** An earlier version left `GET` endpoints open so the
console could render without a key. That was the wrong trade: a case list is a
list of customers, amounts and failure reasons, and "it is only readable" is not
a reason to publish it. The console now needs a credential of its own — serve it
from a session-scoped backend-for-frontend rather than embedding a static key in
browser JavaScript, where it would not be a secret anyway.

**Prompt injection is mitigated, not solved.** Fencing raises the cost of an
attack and makes one visible in the stored `context_snapshot`. It is not a
proof. The actual control is that the guardrail layer is deterministic code the
model cannot talk past, however persuasive the text inside the fence is — a
model convinced to propose an unjustified discount still gets refused and
redirected, and the attempt is recorded.

**Multi-tenancy is out of scope for this prototype.** There is no tenant
boundary anywhere in the data model: one deployment serves one host fintech, and
any caller holding a valid API key can read every case in the database. This is
a scoping decision rather than an oversight — see *Known limitations / next
steps at scale* below for what adding one would involve.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/events` | Ingest a revenue-at-risk event. Idempotent. |
| `GET /api/v1/cases` | Work queue, priority-ordered, filtered, paginated. |
| `GET /api/v1/cases/{id}` | Case detail with the full decision trace. |
| `GET /api/v1/review/queue` | Human review queue, uncertainty × value ordered. |
| `POST /api/v1/actions/{intent_id}/report` | Report a real execution outcome. |
| `GET /api/v1/system/status` | LLM budget headroom, scheduler health, security posture. |
| `GET /api/v1/system/metrics` | Portfolio metrics for the dashboard. |
| `GET /api/v1/system/anomalies` | Population-level signals that spiked recently. |
| `GET /api/v1/system/shadow-evaluation` | Shadow decisions scored against reported outcomes. |
| `GET /health` | Liveness. The only unauthenticated endpoint. No database. |
| `WS /ws` | Live cache-invalidation hints for the console. |
| `POST /api/v1/dev/*` | Inject scenarios, force follow-ups, advance a case. |

Full descriptions and examples at `/docs`. The OpenAPI schema is written to serve
as the integration contract, not as an afterthought.

---

## Testing

```bash
./venv/Scripts/python -m pytest tests/ -q     # 354 backend tests, offline
cd frontend && npm test                       # 42 console tests
```

The backend suite runs with **no API key and no network access** — a suite that
needs a live model is a suite nobody runs, and the graceful-degradation paths are
the ones most worth covering. A scripted `FakeProvider` gives real coverage of
the model path.

The live provider tests are the one exception, and they are opted *into*:

```bash
GEMINI_API_KEY=... ./venv/Scripts/python -m pytest tests/ -m llm_smoke -q
```

They are deselected by default (`addopts = -m 'not llm_smoke'`) and **skip**
rather than fail when no key is present, so a checkout without a credential
still goes green. CI runs them in a separate non-blocking job.

What the priority tests actually assert:

- the guardrail redirect fires *regardless of what the model proposes* — several
  tests script the model into choosing a forbidden action on purpose
- an action outside the vertical's palette is a schema failure, not a decision,
  and never reaches the executor
- untrusted text is fenced before it reaches the prompt — including where a
  diagnoser quotes it back into a field that looks like ours
- budget/rate exhaustion and provider failure fall back cleanly, and a transient
  network failure is retried once before it does
- both priority formulas rank correctly, and a decision nobody scored sorts to
  the top of the review queue rather than crashing or defaulting to the middle
- a lost update is refused rather than silently winning
- an undeliverable intent is retried, then dead-lettered, never dropped
- shadow mode never reaches a real executor, and still enforces guardrails
- a duplicate event produces exactly one case
- a substituted executor receives a fully decided intent
- `recoveryai.core` imports with no web framework in the graph

On the console side: the WebSocket provider's reconnect, exponential backoff,
jitter and cache-invalidation behaviour, and the trace timeline's rendering of a
guardrail redirect — the view that carries the product's central claim.

---

## Layout

```
backend/recoveryai/
  core/            ← no web framework, ever
    agent.py         routing governor, guardrail interception, redirect, lifecycle
    policy.py        hard guardrails — what the model cannot argue with
    economics.py     what a case is still worth, and how that paces follow-up
    diagnosis.py     zero-cost rule diagnosis + urgency heuristics
    execution.py     ActionExecutor protocol · Simulated · Webhook · Shadow
    cases.py         lifecycle, DB-backed guardrail capacity, priority scoring
    tools.py         function-call declarations (reasoning + confidence required)
    verticals/       per-specialist config: prompt, palette, guardrail, fallbacks
    llm/             base.py (contract) · gemini.py (adapter) · governance.py
    scheduler.py     follow-up sweep
  db/              SQLAlchemy schema + WAL session management
  api/             the only place FastAPI appears
  simulator/       demo traffic + named demo scenarios
frontend/src/      React console — a separate consumer of the API
  lib/               api client, WebSocket provider, formatting, types
  components/        UI primitives, case table, trace timeline
  pages/             Dashboard, Case Queue, Case Detail, Review, Dev Tools
migrations/        Alembic
```

---

## Demo script

1. **Dev Tools → `cart_price_sensitive` → Inject, twice.** The first case sends a
   discount. The second proposes the same discount, the 90-day guardrail refuses
   it, and the system redirects to a free nudge. Open the second case's trace:
   the proposal, the refusal reason and the redirect are all there.
2. **`autopay_retries_exhausted`.** Three bank-side retries already burned, so
   the retry cap redirects to asking the customer for a new instrument — showing
   that guardrail state includes what the *host* reports, not just what we did.
3. **`b2b_disputed`.** Escalates without spending an LLM call. The outcome is
   forced, so asking the model would waste budget.
4. **Case Queue.** Ordered by value at risk, not arrival.
5. **Review Queue.** Ordered by uncertainty × value — a large uncertain case
   outranks a small forced escalation.
6. **Dev Tools → Run follow-up sweep.** Watch a case take its next step and the
   trace grow.

---

## Known limitations

[LIMITATIONS.md](LIMITATIONS.md) records everything that is incomplete, stubbed,
simplified or untested, with the reason and what "done" would take. The most
important one: **no live LLM call has been made** — the Gemini adapter is covered
only by scripted fakes, because no API key was available during the build. The
`llm-smoke` CI job closes that gap the moment a `GEMINI_API_KEY` secret exists;
run it locally with `pytest -m llm_smoke`.

---

## Known limitations / next steps at scale

Four things this build deliberately does not attempt. Each is a real ceiling
rather than a rough edge, and each is listed with what crossing it would take.

**SQLite → Postgres.** The ORM layer is already portable and Alembic owns the
schema, so the migration is smaller than it looks: change `DATABASE_URL`, drop
the SQLite-specific pragmas in `db/session.py` (`journal_mode=WAL`,
`synchronous=NORMAL`, `busy_timeout`, `check_same_thread=False`), and run
`alembic upgrade head` against the new database — the existing revisions were
generated with `render_as_batch=True` but use no SQLite-only constructs. The one
piece that does *not* travel is `llm.governance.DailyBudgetCounter`, which talks
raw `sqlite3` to a sidecar file on purpose (a shared file deadlocked against the
ORM's write lock). Postgres would need that counter behind a small storage
interface, or moved to Redis, which is the same change multi-process rate
limiting needs anyway.

**Distributed scheduling.** `FollowupScheduler` polls with no distributed lock,
so two replicas would both claim the same due case. The optimistic-concurrency
version column added in this pass turns that from a silent double-action into a
loud conflict — the second writer is refused and skips — but a refusal is a
safety net, not a scheduler. Doing it properly means `SELECT ... FOR UPDATE SKIP
LOCKED` (which needs Postgres) or moving follow-ups onto a real job queue.

**Load testing.** None has been done, so every throughput claim here would be a
guess. The known bottleneck is not the agent loop but SQLite's single-writer
constraint, and measuring before that is replaced would mostly characterise the
database rather than the system.

**Multi-tenancy.** No tenant boundary exists anywhere: no tenant column, no
per-tenant key scoping, no query filter. Adding one is not a middleware change —
it reaches into the guardrail counters (a discount cap is per customer *per
tenant*), the anomaly detection population, the priority queues and every
aggregate in `/system/metrics`. Retrofitting it later is substantially more work
than designing for it, which is worth knowing before this is pointed at more
than one host fintech.
