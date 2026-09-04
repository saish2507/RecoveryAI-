# How the agent works

One case, start to finish. Every step names the file that does it.

## Stack

| Layer | Tech |
|---|---|
| API | FastAPI + Starlette, `backend/recoveryai/api/` |
| Core logic | Plain Python, no web-framework import — `backend/recoveryai/core/` |
| DB | SQLite (WAL), SQLAlchemy ORM — `backend/recoveryai/db/` |
| LLM | Gemini (`gemini-3.5-flash`) via `google-generativeai`, forced function-calling |
| Scheduler | APScheduler, in-process background job |
| Live updates | One WebSocket (`/ws`), server → console only |
| Frontend | Vite + React, polls REST, invalidates on WS message |

`core/` has zero dependency on `api/`. Everything below is callable directly as
a Python library (`RecoveryAgent(...).handle_event(session, event)`) with no
HTTP in the loop — the API is one optional transport over it.

## 1. A case is created

Entry points: `POST /api/v1/events` (real traffic), `POST /api/v1/dev/inject`
(manual), or the background simulator loop (`api/main.py::_simulator_loop`,
10/hour, walking a fixed 10-case permutation matrix in `simulator/__init__.py`).

All three build a `RecoveryEvent` (`core/models.py`) and call
`RecoveryAgent.handle_event()` (`core/agent.py`).

`CaseStore.create_from_event()` (`core/cases.py`):
- Dedupes on `idempotency_key` (sender's `event_id` or `Idempotency-Key`
  header) — a redelivered webhook returns the existing case, no second decision.
- Runs the cheap rule-based diagnosis (`core/diagnosis.py`) once, stores it.
- Scores `priority_score = amount × urgency_multiplier` — this is what makes
  `GET /cases` a priority queue, not FIFO.
- Inserts one `Case` row, status `new`.

## 2. Diagnosis (free, always runs)

`core/diagnosis.py`, one function per vertical (`diagnose_cart`,
`diagnose_b2b`, `diagnose_autopay`). Pure pattern matching over
`vertical_metadata` — no I/O, no model. Returns `(diagnosis, confidence,
reasoning)`. Confidence is what step 3 reads to decide if a model is worth
consulting.

## 3. Routing — is this worth an LLM call?

`RecoveryAgent._route()`. Checked in order, first match wins:

1. Diagnosis is in `forced_escalation_diagnoses` (e.g. `disputed`) → no LLM,
   forced escalation. Asking would waste budget on an unusable answer.
2. Guardrail capacity for every non-terminal action is already exhausted →
   no LLM, escalation is forced anyway.
3. LLM is off or unconfigured → no LLM, run the deterministic table.
4. It's a reconsideration (step > 1) → **use LLM**. The previous action didn't
   land; that's exactly where rules are worst.
5. Rule confidence ≤ `0.7` → **use LLM**. Genuinely ambiguous signal.
6. Amount ≥ ₹10,000 on the first decision → **use LLM**. Cheap insurance on
   the expensive cases.
7. Otherwise → no LLM, rules are confident enough.

Most traffic never reaches the model. This is the whole cost-control story.

## 4. Decision

**Rule path** (`_deterministic`): a fixed `(vertical, diagnosis, ltv_tier) →
action` lookup table in `core/policy.py`.

**LLM path** (`_decide`): `core/tools.py` builds the action palette as
function-call schemas (`ToolSpec`), pruned to what the guardrail would allow
anyway (`_prune_tools` — a prompting courtesy, not the control). Outreach
actions' schemas also carry `message_subject` / `message_body` fields — the
model drafts the actual customer-facing copy in the *same* call as the
decision, not a second round-trip (`core/tools.py`, `core/drafts.py`).

The call goes through `GovernedLLM` (`core/llm/governance.py`): rate limiter
→ daily budget counter (own SQLite file, `*.llm-budget.db` — must never share
the main DB, that deadlocks) → cache (identical case+context hashes to the
same answer) → `GeminiProvider` (`core/llm/gemini.py`), forced
`function_calling_config.mode = "ANY"`.

Any LLM failure — no key, rate-limited, budget exhausted, provider error,
model invented an action outside this vertical's palette — raises
`LLMUnavailable` and falls through to the same deterministic table. The system
never crashes for want of a model; it just gets less adaptive.

Output either way: `(action, params, reasoning, confidence, source)`.

## 5. Guardrails — the actual control

`core/policy.py::check_guardrails()`. Hard-coded, not model-adjustable:

| Vertical | Limit |
|---|---|
| Cart | 1 discount per customer per 90 days |
| B2B | 3 outreach touches per invoice, then forced escalation |
| Autopay | 3 retry attempts, then forced hand-off |

Computed fresh from `case_steps` on every read (`CaseStore.capacity_for`) —
never an in-memory counter, so a restart can't reset it.

If the proposed action is blocked: walk the vertical's `fallback_chain` until
one is allowed. Every chain ends in `escalate_to_human`, which is never
blocked — a fully constrained case always has a legal move. The block is
**recorded**, not silently swallowed: proposed X, blocked for reason R,
redirected to Y — all three land in the trace.

If the action changed (blocked → fallback), the model's drafted copy for the
old action is discarded before the new one gets templated copy
(`agent.py::advance_case`) — a blocked 20%-off coupon's wording must never
survive onto the plain nudge that replaced it.

## 6. Execution

`ActionExecutor.execute(intent, event)` — `core/execution.py`. Three
implementations, selected by `ACTION_EXECUTOR`:

- **`simulated`** (default) — `core/actions.py`. Each action rolls its own
  `recovery_rate` as a landing *probability*, not a fixed fraction. Landed →
  full amount recovered, status `executed`. Missed → `0` recovered, retains
  its resting status (`scheduled` for a retry, `executed` for a nudge that
  went out but didn't convert).
- **`webhook`** — POSTs the signed intent to `ACTION_WEBHOOK_URL`; the host
  answers now or reports later via `POST /actions/{id}/report`.
- **`razorpay`** — `core/razorpay.py`. Turns the intent into a real test-mode
  Payment Link, using the model's drafted body as the link description.
  Always returns `pending_host_execution` / `recovered_amount=0` — creating a
  link is not a payment; the case resolves only when Razorpay's
  `payment_link.paid` webhook calls back with `reference_id = intent_id`.

## 7. Recording

`CaseStore.record_step()` — one append-only `CaseStep` row per decision.
Never updated in place; this row **is** the audit trail: what was proposed,
what the guardrail said, what actually ran, the full context snapshot the
decision was made from, cost, recovered amount. Rolls forward onto the `Case`:
`amount_recovered`, `cost_of_recovery`, `step_count`, `priority_score` (now
switches to the human-review formula `(1 − confidence) × amount`).

## 8. Transition — does the case move, and where

`RecoveryAgent._transition()`, checked in order:

1. `result.recovered_amount > 0` → **`resolved`**. Checked first: money
   arriving ends the case whichever lever brought it in, no matter what else
   is true.
2. Final action is `escalate_to_human` → **`escalated`** + Slack notification
   (`core/notifications.py`, silent unless `SLACK_WEBHOOK_URL` is set).
3. Final action is `close_as_unrecoverable` → **`abandoned`**.
4. `step_count >= max_case_steps` (default 6) → **`escalated`** (bounded
   workflow — an agent that can work a case forever, will) + notification.
5. Otherwise → **`in_progress`**, schedule a follow-up
   `followup_delay_seconds` out (default 4h — the real retry cadence, not a
   demo-compressed one).

## 9. Follow-up

`core/scheduler.py::FollowupScheduler`, one polling job (every 5s) reading
`WHERE next_followup_at <= now AND status NOT IN (terminal)`. Each due case
re-enters at step 3 (routing) — this is the entire retry loop: same
diagnosis, same guardrail capacity check, but capacity has moved (one more
retry spent), so eventually the guardrail redirects to hand-off. No separate
"retry" code path; it's the same decision loop run again.

To fast-forward instead of waiting real hours: `POST
/api/v1/dev/followups/run` (sweep everything due) or `POST
/api/v1/dev/cases/{id}/advance` (force one case).

## 10. Terminal

Case status is `resolved`, `escalated`, or `abandoned`. `next_followup_at`
cleared. No further steps are ever taken (`advance_case` returns `None` on a
terminal case — a follow-up firing late on an already-closed case is routine,
not an error).

## Read paths (no agent involvement)

- `GET /cases` — priority-ordered queue, `open_only` / `status` filters.
- `GET /cases/{id}` — full step-by-step trace.
- `GET /review/queue` — escalated cases ordered by `(1−confidence) × amount`.
- `GET /system/metrics` — aggregate revenue/decision stats.
- `GET /system/anomalies` — population-level spike detection (`core/anomalies.py`):
  compares the last hour against a 24h baseline per `(vertical, diagnosis)` and
  `(vertical, error_code)`, plus an escalation-rate check. Catches "40 correctly
  diagnosed cases, 1 missed incident" — no single case shows a spike.
