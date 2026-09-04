"""Rate, cost and cache governance — wraps *any* `LLMProvider`.

Ported from the previous build's `RateLimiter` / `DailyBudgetCounter`, with the
Gemini-free-tier numbers lifted out into typed settings. The limits are now
arguments, so pointing this at an enterprise model with a 10k RPM allowance is a
config change, not a code change.

Four guarantees, in order of cheapness:
  0. switched off       → no cache lookup, no budget, no network
  1. cache hit          → no budget, no network
  2. rate/budget check  → refuse *before* the network call, never after
  3. provider failure   → `LLMUnavailable`, never an SDK exception escaping
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import UTC, date, datetime
from typing import Any

from recoveryai.core.llm.base import (
    LLMProvider,
    LLMUnavailable,
    ToolCall,
    ToolSpec,
    is_transient_error,
)

logger = logging.getLogger(__name__)

#: Cache entries retained before the least-recently-used one is evicted. Sized
#: so a busy day of distinct cases fits comfortably while the memory cost stays
#: trivial — a `ToolCall` is a name, a small dict and two scalars.
CACHE_MAX_ENTRIES = 2_000

#: Extra attempts after a transient failure. One, not three.
#:
#: A recovery decision is not latency-free work happening in the background — an
#: HTTP request is waiting on it. One retry covers the overwhelmingly common
#: case (a single dropped connection) without turning a provider outage into a
#: pile-up of requests each holding a connection open for a multiple of the
#: timeout. Beyond one, the deterministic tables are the better answer: they are
#: instant, and they were always going to be the destination anyway.
TRANSIENT_MAX_RETRIES = 1

#: Pause before the retry. Long enough to clear a momentary blip, short enough
#: that a caller waiting on the decision does not notice it.
TRANSIENT_BACKOFF_SECONDS = 0.5

#: `LLMUnavailable.reason` prefix for a transport failure that outlived its
#: retry. Callers key the `fallback_transient` decision source off this.
TRANSIENT_REASON_PREFIX = "transient_error"


# ── Token-bucket rate limiter ──────────────────────────────────────


class RateLimiter:
    """Process-wide token bucket over a rolling minute and a calendar day.

    Thread-safe: APScheduler follow-up jobs and request handlers hit this
    concurrently.
    """

    def __init__(self, max_per_minute: int = 4, max_per_day: int = 90) -> None:
        self.max_per_minute = max_per_minute
        self.max_per_day = max_per_day
        self._lock = threading.Lock()
        self._minute_tokens = max_per_minute
        self._minute_last = time.monotonic()
        self._day_tokens = max_per_day
        self._day_date = datetime.now(UTC).date()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._minute_last
        if elapsed >= 60.0:
            buckets = int(elapsed / 60.0)
            self._minute_tokens = min(self.max_per_minute, self._minute_tokens + buckets)
            self._minute_last = now - (elapsed % 60.0)

        today = datetime.now(UTC).date()
        if today > self._day_date:
            self._day_tokens = self.max_per_day
            self._day_date = today

    def can_proceed(self) -> bool:
        with self._lock:
            self._refill_locked()
            return self._day_tokens > 0 and self._minute_tokens > 0

    def consume(self) -> None:
        with self._lock:
            self._refill_locked()
            self._minute_tokens = max(0, self._minute_tokens - 1)
            self._day_tokens = max(0, self._day_tokens - 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._refill_locked()
            return {
                "minute_tokens_remaining": self._minute_tokens,
                "minute_capacity": self.max_per_minute,
                "day_tokens_remaining": self._day_tokens,
                "day_capacity": self.max_per_day,
            }


# ── Persistent daily budget ────────────────────────────────────────


class DailyBudgetCounter:
    """Calls-per-day counter persisted in SQLite so it survives a restart.

    Uses raw sqlite3 rather than the SQLAlchemy session on purpose: the budget
    must be readable and writable even when the ORM layer is mid-transaction or
    unavailable.

    **It must not share a file with the ORM.** An earlier version pointed at the
    application database, on the theory that WAL mode lets two writers coexist.
    WAL permits one writer plus many concurrent *readers* — not two writers. The
    consequence was a reliable deadlock on the main ingestion path: `handle_event`
    flushes the new case, which takes SQLite's write lock; the agent then routes
    to the model; `record_call` opens its own connection and waits for a lock the
    request itself is holding and will not release until it finishes. Every event
    that earned an LLM call died after the busy timeout with "database is locked",
    while rule-decided events — which never touch the counter — passed. Hence a
    sidecar file, sitting beside the main database rather than inside it.
    """

    def __init__(self, db_path: str, max_per_day: int = 90) -> None:
        self.db_path = self.sidecar_path(db_path)
        self.max_per_day = max_per_day
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()
        self._today, self._calls = self._read()

    @staticmethod
    def sidecar_path(db_path: str) -> str:
        """`.../recoveryai.db` → `.../recoveryai.llm-budget.db`.

        Derived from the application database rather than configured separately,
        so the budget still travels with the deployment it governs and stays
        obvious to anyone looking in that directory.
        """
        if not db_path or db_path == ":memory:":
            return db_path
        base, _, ext = db_path.rpartition(".")
        return f"{base}.llm-budget.{ext}" if base else f"{db_path}.llm-budget"

    def _connect(self) -> sqlite3.Connection:
        # Short timeout: nothing else writes this file, so waiting long means
        # something is wrong rather than merely busy.
        conn = sqlite3.connect(self.db_path, timeout=3.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_daily_budget (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    day TEXT NOT NULL,
                    calls_made INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO llm_daily_budget (id, day, calls_made) VALUES (1, ?, 0) "
                "ON CONFLICT(id) DO NOTHING",
                (date.today().isoformat(),),
            )

    def _read(self) -> tuple[str, int]:
        today = date.today().isoformat()
        with self._connect() as conn:
            row = conn.execute("SELECT day, calls_made FROM llm_daily_budget WHERE id = 1").fetchone()
        if row is None or row[0] != today:
            return today, 0
        return row[0], int(row[1] or 0)

    @property
    def remaining(self) -> int:
        """Headroom left today. Falls back to the in-memory count if the file is
        unreadable — an unreadable counter must not take the agent down with it."""
        with self._lock:
            try:
                today, calls = self._read()
            except sqlite3.Error:
                logger.warning("llm budget read failed; using in-memory count", exc_info=True)
                today, calls = self._today, self._calls
            self._today, self._calls = today, calls
            return max(0, self.max_per_day - calls)

    def record_call(self) -> None:
        """Bank one call against today's budget.

        Never raises. The call it is counting has already been made and its
        answer is about to drive a decision; throwing here would discard real
        work to protect a counter. A failed write costs at most an overcount of
        remaining budget until the next successful one.
        """
        with self._lock:
            today = date.today().isoformat()
            try:
                stored_day, calls = self._read()
                calls = calls + 1 if stored_day == today else 1
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE llm_daily_budget SET day = ?, calls_made = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                        (today, calls),
                    )
            except sqlite3.Error:
                logger.warning("llm budget write failed; counting in memory only", exc_info=True)
                calls = self._calls + 1 if self._today == today else 1
            self._today, self._calls = today, calls

    def snapshot(self) -> dict[str, int]:
        return {"day_calls_made": self.max_per_day - self.remaining, "day_budget": self.max_per_day}


# ── Cache key ──────────────────────────────────────────────────────


def hash_input(*parts: Any) -> str:
    """Stable hash of an LLM input, for cache keying."""
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# ── The wrapper ────────────────────────────────────────────────────


class GovernedLLM:
    """Adds cache + rate limiting + daily budget in front of any provider.

    Implements `LLMProvider` itself, so it is a drop-in substitute anywhere a
    bare provider is accepted — including in front of another `GovernedLLM`,
    though there is no reason to do that.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter,
        budget: DailyBudgetCounter | None = None,
        cache_enabled: bool = True,
        cache_max_entries: int = CACHE_MAX_ENTRIES,
        enabled: bool = True,
        transient_max_retries: int = TRANSIENT_MAX_RETRIES,
        transient_backoff_seconds: float = TRANSIENT_BACKOFF_SECONDS,
    ) -> None:
        self.provider = provider
        self.name = f"governed:{provider.name}"
        self.rate_limiter = rate_limiter
        self.budget = budget
        self.cache_enabled = cache_enabled
        self.cache_max_entries = cache_max_entries
        self._enabled = enabled
        self.transient_max_retries = transient_max_retries
        self.transient_backoff_seconds = transient_backoff_seconds
        # An OrderedDict used as an LRU. An unbounded dict here is a slow leak:
        # every distinct case shape adds an entry that is never reclaimed, and a
        # long-running process sees a lot of distinct case shapes.
        self._cache: OrderedDict[str, ToolCall] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.last_call_was_cached = False

    @property
    def enabled(self) -> bool:
        """Runtime kill switch, independent of whether a key is present.

        Deliberately not persisted. This is an operator's "stop spending on the
        model right now", and the safe state after a restart is whatever the
        deployed configuration says — not whatever someone clicked during an
        incident three days ago.
        """
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def is_configured(self) -> bool:
        """Whether a usable key exists. Reported as-is, regardless of the switch."""
        return self.provider.is_configured()

    def is_available(self) -> bool:
        """Configured *and* switched on — what callers ask before routing to a model.

        Kept separate from `is_configured` so the status endpoint can tell
        "nobody gave me a key" apart from "someone turned me off", which are the
        same symptom with very different fixes.
        """
        return self._enabled and self.provider.is_configured()

    def headroom(self) -> int:
        """How many calls can be made right now without being refused.

        Exists so callers can size a batch to what the provider will actually
        accept, rather than starting work and discovering the ceiling partway
        through. The follow-up sweep used to take twenty-five due cases and
        advance them all: the first few reached the model and the rest fell back
        to the policy tables, so whether a case got the agent's judgement came
        down to its position in a list.

        Deliberately the *minimum* of the two windows. Per-minute headroom alone
        would happily start work the daily budget cannot finish.
        """
        limits = self.rate_limiter.snapshot()
        available = min(limits["minute_tokens_remaining"], limits["day_tokens_remaining"])
        if self.budget is not None:
            available = min(available, self.budget.remaining)
        return max(0, available)

    def cache_peek(self, key: str) -> ToolCall | None:
        if not self.cache_enabled:
            return None
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)  # mark as recently used
            return hit

    def _cache_store(self, key: str, value: ToolCall) -> None:
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max_entries:
                self._cache.popitem(last=False)  # evict least recently used

    def choose_tool(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        timeout_seconds: float = 30.0,
        cache_key: str | None = None,
    ) -> ToolCall:
        self.last_call_was_cached = False

        # Checked before the cache: a switched-off model should not answer even
        # from memory, or turning it off would look like it was still working.
        if not self._enabled:
            raise LLMUnavailable("llm_disabled")
        if not self.provider.is_configured():
            raise LLMUnavailable("provider_not_configured")

        key = cache_key or hash_input(system_prompt, user_prompt, sorted(t.name for t in tools))
        cached = self.cache_peek(key)
        if cached is not None:
            self.last_call_was_cached = True
            return cached

        # Refuse before spending anything, not after.
        if self.budget is not None and self.budget.remaining <= 0:
            raise LLMUnavailable("daily_budget_exhausted")
        if not self.rate_limiter.can_proceed():
            raise LLMUnavailable("rate_limit_exhausted")

        result = self._call_with_transient_retry(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            timeout_seconds=timeout_seconds,
        )

        if self.cache_enabled:
            self._cache_store(key, result)
        return result

    def _call_with_transient_retry(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        timeout_seconds: float,
    ) -> ToolCall:
        """Invoke the provider, retrying once if the failure was transport-level.

        Only transport failures are retried, and the distinction is the whole
        point: a dropped connection and an exhausted budget both end in the
        deterministic tables, but one of them is worth asking again and the other
        is a decision the governance layer already made. Collapsing them — as a
        blanket `except Exception: retry` would — spends a second call arguing
        with a limit that is not going to move.

        Every attempt is charged separately. The provider served each request as
        far as it got, and under-counting a limit is the failure mode that ends
        in a hard provider cutoff.
        """
        attempts = 0
        last_exc: Exception | None = None

        while attempts <= self.transient_max_retries:
            if attempts > 0:
                # The retry is a fresh request and needs its own headroom. If
                # the bucket emptied since the first attempt, stop here: the
                # fallback is instant and a queued retry is not.
                if not self.rate_limiter.can_proceed():
                    break
                time.sleep(self.transient_backoff_seconds)

            # Charge before the call, not after — a provider timeout still
            # counts, which is the conservative reading and matches how
            # providers actually bill partially-served requests.
            self.rate_limiter.consume()
            if self.budget is not None:
                self.budget.record_call()
            attempts += 1

            try:
                return self.provider.choose_tool(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    timeout_seconds=timeout_seconds,
                )
            except LLMUnavailable as exc:
                # The provider itself declined in a way it has already
                # classified. Not ours to second-guess or retry.
                exc.budget_consumed = True
                raise
            except Exception as exc:  # provider SDKs raise anything; contain it here
                if not is_transient_error(exc):
                    logger.warning(
                        "llm provider raised, falling back",
                        extra={"provider": self.provider.name, "error": type(exc).__name__},
                    )
                    raise LLMUnavailable(
                        f"provider_error: {type(exc).__name__}", budget_consumed=True
                    ) from exc
                last_exc = exc
                logger.warning(
                    "llm provider transient failure",
                    extra={
                        "provider": self.provider.name,
                        "error": type(exc).__name__,
                        "attempt": attempts,
                    },
                )

        name = type(last_exc).__name__ if last_exc is not None else "unknown"
        raise LLMUnavailable(
            f"{TRANSIENT_REASON_PREFIX}: {name} after {attempts} attempt(s)",
            budget_consumed=True,
        ) from last_exc

    def snapshot(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "provider": self.provider.name,
            "configured": self.provider.is_configured(),
            "enabled": self._enabled,
            "available": self.is_available(),
            "cache_entries": len(self._cache),
            "cache_capacity": self.cache_max_entries,
            **self.rate_limiter.snapshot(),
        }
        if self.budget is not None:
            state.update(self.budget.snapshot())
        return state
