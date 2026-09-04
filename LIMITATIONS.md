# LIMITATIONS

Everything in this build that is not fully built, fully working and verified.
Written as each item was hit, not reconstructed at the end.

Format: **what** is incomplete · **why** · what "done" would require.

---

## The one that matters most

### No live LLM call has ever been made
The full test suite runs key-free and network-free by design, and no
`GEMINI_API_KEY` was available during the build. The Gemini adapter's request
shape, its forced tool-mode config (`mode: "ANY"`) and its response-parsing path
are therefore **unverified against the real API** — they are covered only by a
scripted `FakeProvider`.

Everything *around* the model is genuinely exercised: routing, tool pruning,
guardrail interception and redirect, budget and rate exhaustion, cache hits,
provider failure, and the fallback to deterministic tables. The one untested link
is the wire format between `GeminiProvider` and Google's SDK.

**Why:** no credential.
**Done would require:** one smoke call per vertical with a real key. Expect to
spend a few minutes on `_extract_function_call` if the SDK's response shape
differs from what the adapter assumes.

**Status update:** the test now exists — `tests/test_llm_smoke.py`, one live
call per vertical plus one end-to-end decision per vertical, wired into CI as a
non-blocking `llm-smoke` job. It is deselected from the default run and skips
without a key, so *this entry stays open until someone runs it with a real
credential*. The harness is no longer the blocker; the credential is.

---

## Simulation and data

### Recovery and cost figures are illustrative constants, not a model
`SimulatedExecutor` credits a fixed fraction of the at-risk amount per action
(nudge 15%, discount 25%, payment plan 35%, …) from constants in
`core/actions.py`. These are plausible, not measured.
**Why:** no access to real recovery outcomes.
**Done would require:** fitting per-action, per-vertical, per-segment rates
against the host's history, and presenting the result as an expected value with
an interval rather than a point estimate. The dashboard marks these tiles `Est.`
so they do not read as measured, but the number behind the badge is still a
constant times an amount.

### The partial-payment-plan cost figure deviates from the ported code
The previous build recorded `cost = 50% of the invoice` for
`offer_partial_payment_plan`, treating deferred revenue as money spent, which
made the whole B2B vertical read as loss-making. This build records a flat ₹25
servicing cost.
**Why:** deliberate correction — deferring an invoice is not a cost.
**Done would require:** the host's real cost-to-serve for an instalment plan.

### A simulated case never reaches `resolved` on its own
Cases move `new → in_progress → escalated | abandoned`, and reach `resolved` only
when a host reports a recovery via `POST /api/v1/actions/{intent_id}/report`.
Nothing in the standalone demo decides that a simulated customer actually paid.
**Why:** deliberate. Fabricating a payment outcome inside the agent would corrupt
the recovery numbers the system exists to report, and randomness does not belong
in `core/`.
**Done would require:** an outcome probe on the simulator side (fake data owning
its own fake outcomes), or a real host reporting real results.

### ~~The discount guardrail did not check whether a discount was justified~~ — fixed
Until this fix, `check_cart_guardrails` only asked "has this customer had a
discount in 90 days?" It never asked whether the diagnosis justified one at
all. The product story for this vertical is explicit — payment failure and
distraction both get a free nudge, never a discount — but that rule lived only
in the decision table (used when rules decide) and in the LLM's system prompt
(a suggestion, when the model decides). Nothing in code stopped a model that
disagreed with its own instructions from proposing `send_discount` against a
technical decline, and the guardrail would have let it through.
**Fixed:** `check_cart_guardrails` now takes the diagnosis and refuses
`send_discount` outright for `payment_failure` and `distraction`, independent
of the 90-day cap — a customer's *first ever* discount can still be wrong if
the diagnosis doesn't support it. Covered by
`tests/test_guardrails.py::test_discount_is_refused_for_diagnoses_it_is_not_justified_by`
and, end to end with a scripted model that deliberately proposes the wrong
action, `tests/test_agent.py::test_llm_cannot_discount_a_technical_payment_failure`.

### Cold-start signals are handled, but crudely
A missing `payment_history_score` (or `price_vs_customer_avg`) is now treated as
*unknown* rather than as a perfect record, and the case is routed to the agent at
low confidence instead of being decided by rules. But "unknown" collapses to a
single low confidence value regardless of context — a first invoice to a Fortune
500 subsidiary and one to a company registered last week are scored identically.
**Why:** the fix addressed the unsafe default; genuine cold-start scoring needs
signals the event payload does not carry.
**Done would require:** accepting optional enrichment on the event (company age,
credit rating, contract value) and weighting the opening decision by it.

### Urgency heuristics are hand-tuned, not learned
`diagnosis.urgency_multiplier` uses hand-picked weights (LTV tier, invoice age,
retry count) bounded to [0.5, 2.0]. The bounds and the direction of each effect
are defensible; the exact coefficients are guesses.
**Why:** no data to fit against.
**Done would require:** calibrating against realised recovery rates by segment.

---

## LLM governance

### Budget is charged before the call, not after
`GovernedLLM` decrements the rate limiter and daily counter *before* invoking the
provider, so a request that fails in transit still counts against the day.
**Why:** deliberate and conservative — providers often bill partially served
requests, and under-counting risks breaching a hard provider cap.
**Trade-off:** a flapping network can burn budget without producing decisions.

### Rate limiter and budget are per-process
`RateLimiter` is in-memory, so running two backend replicas doubles the effective
RPM. `DailyBudgetCounter` is shared through SQLite and *is* correct across
processes on one host, but not across hosts.
**Why:** single-container deployment was the target.
**Done would require:** moving both behind Redis or the provider's own quota API.

### The decision cache has a size bound but no TTL
`GovernedLLM._cache` is an LRU capped at 2,000 entries, so it cannot grow
without limit. It has no time-based expiry, though: a decision cached this
morning is still served this evening for an identical case shape.
**Why:** size was the actual leak; staleness is bounded in practice because the
cache key includes the case's prior steps, so a case that has moved on gets a
fresh key.
**Done would require:** a TTL, if a deployment wants decisions to reflect model
or prompt changes without a restart.

---

## Persistence and scaling

### `create_all()` still runs at startup alongside Alembic
Alembic is wired (`migrations/`, initial revision applied, `alembic check` clean,
and the container runs `alembic upgrade head` before starting), but `init_db()`
also calls `create_all()` on boot so a zero-config local run and the test suite
work without a migration step. On a migrated database this is a no-op, but schema
drift would be papered over at startup rather than failing loudly.
**Why:** deliberate trade-off for demo ergonomics.
**Done would require:** dropping `create_all()` from the startup path and giving
the test suite a migration-based fixture.

### SQLite only
WAL, `busy_timeout` and `check_same_thread=False` are SQLite-specific.
`DATABASE_URL` accepts a Postgres URL and the ORM layer would work, but
`DailyBudgetCounter` uses raw `sqlite3` against the same file and would silently
stop functioning.
**Why:** prototype scope.
**Done would require:** putting the budget counter behind a small storage
interface with a Postgres implementation.

### The follow-up scheduler assumes a single instance
`FollowupScheduler` polls for due cases with no distributed lock. Two backend
replicas would both pick up the same case.
**Partially mitigated:** `Case` now carries a `version` column
(`version_id_col`), so the second writer's UPDATE matches zero rows and raises
`ConcurrentModificationError` instead of silently overwriting. The scheduler
catches it, skips, and counts it in `snapshot()["skipped_conflicts"]`; the
outcome-report endpoint retries once and then answers 409.
**Why this is not a fix:** the guard makes a lost update loud, but both replicas
still *decide* — the LLM call is spent and the executor may already have
dispatched before the conflict is detected on write. Only one of them records a
step, which prevents double-counting, not double-sending.
**Done would require:** a `SELECT ... FOR UPDATE SKIP LOCKED` claim (needs
Postgres), or moving follow-ups onto a real job queue.

---

## API and security

### ~~The WebSocket endpoint is unauthenticated~~ — fixed
`/ws` accepted any connection and emitted case ids, amounts and action names to
it.
**Fixed:** the handshake now validates the same API key through the same
`security.key_is_valid` the HTTP routes use — extracted rather than duplicated,
so the two cannot drift — and is checked *before* `accept()`, so a rejected
client never enters the broadcaster's fan-out set. The key may arrive as a
header or as `?api_key=`, because browsers cannot set headers on a WebSocket
handshake. Covered by three tests in `tests/test_api.py`.
**Still open underneath it:** a static key in a query string is weaker than a
short-lived connect token, and it lands in server access logs. A ticket scheme
issued by an authenticated REST call remains the better answer.

### ~~Read endpoints are unauthenticated by design~~ — reversed
`API_KEYS` protected writes only, so anyone who could reach the API could read
every case.
**Fixed:** the key is now required on every endpoint except `/health`, applied
once at router inclusion rather than per route, so a new endpoint is
authenticated by default. `/health` stays open deliberately: a load balancer has
no key, and a probe that fails closed on an auth misconfiguration would pull a
healthy service out of rotation.
**What this costs — and it is not small.** The console has *no* API-key
mechanism today: `frontend/src/` never sends the header, and its only reference
to auth is reading `api_key_auth_enabled` from `/system/status` for display. So
with `API_KEYS` set, the console is now non-functional — every read returns 401
and the WebSocket handshake is rejected. It keeps working with `API_KEYS` empty,
which is local dev only. **Anyone enabling auth must add key handling to the
console first.** The original reasoning — that a key in browser JavaScript is
not a secret — is still correct, so the right answer is a session-scoped
backend-for-frontend holding the key server-side rather than a key compiled into
the bundle.
**Done would require:** real user auth (OIDC/session) in front of the console,
with the API trusting a signed session rather than a static key.

### No request-level rate limiting on the API
The *LLM* is rate-governed; the HTTP surface is not. A flood of `POST
/api/v1/events` will happily create cases until the disk fills.
**Why:** out of scope for the prototype.
**Done would require:** a per-key limiter at the middleware or gateway layer.

### ~~`WebhookExecutor` does not retry~~ — fixed
A single POST; a timeout or 5xx became `ExecutionResult(status=error)` and the
intent was dropped with only a log line to show for it.
**Fixed:** one retry after a 0.5s pause for connection errors and retryable
statuses (408/429/5xx), then a `failed_deliveries` row carrying the whole intent
as JSON so the delivery can be replayed by hand. A non-retryable 4xx skips the
retry — the host is saying the request is wrong, and it will be as wrong the
second time — but is still dead-lettered, because an intent the host will never
accept is revenue nobody is working. The sink follows the executor/notifier
seam: protocol, `NullDeadLetterSink` default that logs at ERROR, database
implementation injected by `build_executor`.
**Still open:** the retry is fixed-delay rather than exponential, there is no
jitter, and nothing replays `failed_deliveries` automatically — it is a record
for a human, not a queue. A host wanting at-least-once delivery still needs a
proper broker.

---

## Frontend

### No end-to-end browser test
42 Vitest tests cover the presentation helpers, the WebSocket provider's full
reconnect/backoff/invalidation behaviour, and the trace timeline's rendering of
a guardrail redirect. What is missing is a Playwright run that drives a real
browser through the demo script against a live backend.
**Why:** the unit-level tests cover the logic that can silently regress; the
end-to-end path was verified by hand instead (every page loaded against the live
backend, guardrail redirect confirmed in the trace, live WebSocket updates
confirmed to append without a refresh — rows went 27 → 33 on injection).
**Done would require:** Playwright, and a CI job to run it.

### Vendor code is not manually chunked
Routes are lazy-loaded, so the initial payload is 372 kB (119 kB gzipped) rather
than the 580 kB it was, and Vite no longer warns. The remaining bulk is React
plus TanStack Query in one vendor chunk.
**Why:** route splitting was the win worth having; further chunking is
diminishing returns at this size.
**Done would require:** manual `rollupOptions.output.manualChunks` groupings.

### The "Est." badge is unconditional
The dashboard's revenue tiles now carry a visible `Est.` badge, so simulated
figures no longer read as measured. But the badge is always shown — it does not
disappear once a host starts reporting real outcomes through
`POST /api/v1/actions/{intent_id}/report`.
**Why:** distinguishing "all simulated" from "partly reported" needs a metric the
API does not yet expose.
**Done would require:** a `reported_outcome_count` in `/system/metrics` and
conditional rendering against it.

---

## Introduced by the hardening pass

New capabilities, and honestly what each does *not* do.

### Prompt-injection fencing is a mitigation, not a control
`raw_failure_reason`, `vertical_metadata` strings, prior-step reasoning and the
rule diagnoser's explanation are wrapped in `<customer_supplied_data>` tags with
a standing "this is data, not instructions" notice, and a payload that tries to
close the fence itself has its delimiter defused.
**What it does not do:** stop a sufficiently persuasive injection. There is no
evaluation behind it — no corpus of attempted injections, no measured
success rate — so its effectiveness is asserted, not demonstrated. The real
control remains the guardrail layer, which is deterministic and does not read
the prompt at all.
**Done would require:** an adversarial test corpus, and a measured before/after.

### Shadow-mode agreement is a coarse measure
`GET /api/v1/system/shadow-evaluation` compares shadow decisions against
reported outcomes on one axis: did the agent judge the case recoverable, and did
money arrive.
**What it does not do:** compare *actions*. The outcome report does not carry
which action the host took, so "the agent would have nudged, the human phoned"
is invisible. A high agreement rate means the agent's instinct about
recoverability tracks reality — not that its action choices were right.
**Done would require:** an optional `action_taken` on `ActionReport`, and a
mapping between the host's action vocabulary and ours.

### The guardrail version is stamped, not enforced
Every `case_steps` row records `policy.GUARDRAIL_VERSION`.
**What it does not do:** verify the constant was actually bumped when a
threshold changed. Nothing fails if someone edits `MAX_B2B_TOUCHES` and leaves
the version alone, which would silently attribute new decisions to old rules.
**Done would require:** a test hashing the thresholds and tables and asserting
the hash matches the declared version — cheap, and not done here.

### Concurrency is guarded on write, not on decide
See the scheduler entry above: the version column prevents a lost *update*, not
a duplicated *decision*.

---

## Verified, for contrast

So the list above is read in proportion, these were actually checked end to end:

- 354 backend tests green, key-free and network-free
- 7 live-provider smoke tests present, deselected by default, skipping cleanly
  without a credential (never run against the real API — see the top entry)
- `alembic upgrade head` → `alembic check` clean, and the new revision
  round-trips through `downgrade -1` → `upgrade head`
- 42 frontend tests green (helpers, WebSocket reconnect, trace timeline rendering)
- `recoveryai.core` imports with zero FastAPI/Starlette/uvicorn in the graph,
  enforced in a subprocess by `tests/test_core_boundary.py`
- guardrail redirect confirmed in the running system, not only in tests
- shadow mode confirmed to dispatch nothing while still enforcing guardrails
- duplicate event confirmed to produce exactly one case, over HTTP
- scheduled follow-up confirmed to fire and append a step
- both priority orderings confirmed against the live queues
- `docker compose up --build` brings up both services healthy from a clean
  volume, with migrations applied and the console reaching the API through nginx
- `npm run build` clean; console verified page by page in a browser
