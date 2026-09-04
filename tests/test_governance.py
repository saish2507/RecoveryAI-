"""LLM governance: rate limiting, daily budget, caching, failure containment.

Ported and extended from the previous build's `test_rate_limiter.py`. The
substantive change is that the limits are now parameters rather than Gemini
free-tier constants, so these tests assert the *mechanism* rather than the
numbers 4 and 90.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import FakeProvider
from recoveryai.core.llm.base import LLMUnavailable, ToolCall, ToolSpec
from recoveryai.core.llm.governance import (
    DailyBudgetCounter,
    GovernedLLM,
    RateLimiter,
    hash_input,
)

TOOLS = [ToolSpec("send_nudge", "nudge", {"type": "object", "properties": {}})]


def call(llm: GovernedLLM, prompt: str = "p", **kwargs) -> ToolCall:
    return llm.choose_tool(system_prompt="s", user_prompt=prompt, tools=TOOLS, **kwargs)


# ── Rate limiter ───────────────────────────────────────────────────


def test_limits_are_configurable_not_hardcoded() -> None:
    """The point of the rewrite: an enterprise LLM's allowance is a setting."""
    enterprise = RateLimiter(max_per_minute=5_000, max_per_day=1_000_000)
    for _ in range(1_000):
        assert enterprise.can_proceed()
        enterprise.consume()
    assert enterprise.can_proceed()


def test_minute_bucket_blocks_once_drained() -> None:
    limiter = RateLimiter(max_per_minute=3, max_per_day=100)
    for _ in range(3):
        assert limiter.can_proceed()
        limiter.consume()
    assert limiter.can_proceed() is False


def test_daily_bucket_blocks_independently_of_the_minute_bucket() -> None:
    limiter = RateLimiter(max_per_minute=100, max_per_day=2)
    limiter.consume()
    limiter.consume()
    assert limiter.can_proceed() is False


def test_minute_bucket_refills_after_a_minute(monkeypatch) -> None:
    limiter = RateLimiter(max_per_minute=2, max_per_day=100)
    limiter.consume()
    limiter.consume()
    assert limiter.can_proceed() is False

    limiter._minute_last -= 61.0  # simulate a minute passing
    assert limiter.can_proceed() is True


def test_refill_never_exceeds_capacity() -> None:
    limiter = RateLimiter(max_per_minute=2, max_per_day=100)
    limiter._minute_last -= 6_000.0  # a hundred minutes of "idle"
    assert limiter.snapshot()["minute_tokens_remaining"] == 2


def test_consume_never_goes_negative() -> None:
    limiter = RateLimiter(max_per_minute=1, max_per_day=1)
    for _ in range(5):
        limiter.consume()
    snapshot = limiter.snapshot()
    assert snapshot["minute_tokens_remaining"] == 0
    assert snapshot["day_tokens_remaining"] == 0


def test_snapshot_exposes_capacity_for_the_status_endpoint() -> None:
    snapshot = RateLimiter(max_per_minute=4, max_per_day=90).snapshot()
    assert snapshot == {
        "minute_tokens_remaining": 4,
        "minute_capacity": 4,
        "day_tokens_remaining": 90,
        "day_capacity": 90,
    }


# ── Daily budget ───────────────────────────────────────────────────


def test_budget_counts_down_and_persists_across_restart(tmp_path: Path) -> None:
    """Survives a restart — otherwise a crash loop resets the spend cap."""
    db = str(tmp_path / "budget.db")
    counter = DailyBudgetCounter(db, max_per_day=5)
    assert counter.remaining == 5
    counter.record_call()
    counter.record_call()
    assert counter.remaining == 3

    reopened = DailyBudgetCounter(db, max_per_day=5)
    assert reopened.remaining == 3


def test_budget_floors_at_zero(tmp_path: Path) -> None:
    counter = DailyBudgetCounter(str(tmp_path / "b.db"), max_per_day=2)
    for _ in range(6):
        counter.record_call()
    assert counter.remaining == 0


def test_budget_resets_on_a_new_day(tmp_path: Path) -> None:
    db = str(tmp_path / "b.db")
    counter = DailyBudgetCounter(db, max_per_day=5)
    counter.record_call()

    with sqlite3.connect(counter.db_path) as conn:
        conn.execute("UPDATE llm_daily_budget SET day = '2001-01-01' WHERE id = 1")

    assert DailyBudgetCounter(db, max_per_day=5).remaining == 5


def test_budget_uses_wal_on_its_own_file(tmp_path: Path) -> None:
    """WAL still matters for the sidecar — readers (the status endpoint) must not
    block the writer. What it never bought was two *writers* on one file."""
    counter = DailyBudgetCounter(str(tmp_path / "shared.db"), max_per_day=5)
    with sqlite3.connect(counter.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# ── GovernedLLM ────────────────────────────────────────────────────


def test_unconfigured_provider_short_circuits_before_any_work() -> None:
    """No key means no network call is even attempted."""
    provider = FakeProvider([ToolCall("send_nudge")], configured=False)
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(10, 100))

    with pytest.raises(LLMUnavailable) as exc:
        call(llm)

    assert exc.value.reason == "provider_not_configured"
    assert provider.call_count == 0


# ── Runtime switch ─────────────────────────────────────────────────


def test_switching_off_short_circuits_before_any_work() -> None:
    """The operator's kill switch, distinct from having no key at all."""
    provider = FakeProvider([ToolCall("send_nudge")])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(10, 100))
    llm.enabled = False

    with pytest.raises(LLMUnavailable) as exc:
        call(llm)

    assert exc.value.reason == "llm_disabled"
    assert exc.value.budget_consumed is False
    assert provider.call_count == 0


def test_switching_off_spends_no_budget_however_hard_it_is_driven() -> None:
    provider = FakeProvider([ToolCall("send_nudge")])
    limiter = RateLimiter(4, 90)
    llm = GovernedLLM(provider, rate_limiter=limiter)
    llm.enabled = False

    for _ in range(10):
        with pytest.raises(LLMUnavailable):
            call(llm)

    assert limiter.snapshot()["day_tokens_remaining"] == 90
    assert limiter.snapshot()["minute_tokens_remaining"] == 4


def test_switching_off_also_stops_cached_answers() -> None:
    """Off must mean off. Serving from cache would look like it was still working."""
    provider = FakeProvider([ToolCall("send_nudge", {}, "cached", 0.7)])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(10, 100))
    call(llm, cache_key="k")

    llm.enabled = False

    with pytest.raises(LLMUnavailable) as exc:
        call(llm, cache_key="k")
    assert exc.value.reason == "llm_disabled"


def test_switching_back_on_restores_service() -> None:
    provider = FakeProvider([ToolCall("send_nudge", {}, "back", 0.7)])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(10, 100))
    llm.enabled = False
    with pytest.raises(LLMUnavailable):
        call(llm)

    llm.enabled = True

    assert call(llm).name == "send_nudge"


def test_snapshot_separates_having_a_key_from_being_switched_on() -> None:
    """Same symptom, different fixes — so they are reported as different fields."""
    llm = GovernedLLM(FakeProvider([]), rate_limiter=RateLimiter(10, 100))
    llm.enabled = False

    snapshot = llm.snapshot()
    assert snapshot["configured"] is True
    assert snapshot["enabled"] is False
    assert snapshot["available"] is False
    assert llm.is_configured() is True
    assert llm.is_available() is False


def test_cache_hit_costs_neither_budget_nor_a_call() -> None:
    provider = FakeProvider([ToolCall("send_nudge", {}, "first", 0.7)])
    limiter = RateLimiter(10, 100)
    llm = GovernedLLM(provider, rate_limiter=limiter)

    first = call(llm, cache_key="k")
    second = call(llm, cache_key="k")

    assert first == second
    assert provider.call_count == 1
    assert llm.last_call_was_cached is True
    assert limiter.snapshot()["minute_tokens_remaining"] == 9  # only the first spent a token


def test_cache_can_be_disabled() -> None:
    provider = FakeProvider([ToolCall("send_nudge"), ToolCall("send_nudge")])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(10, 100), cache_enabled=False)

    call(llm, cache_key="k")
    call(llm, cache_key="k")

    assert provider.call_count == 2


def test_rate_exhaustion_raises_before_touching_the_provider() -> None:
    provider = FakeProvider([ToolCall("send_nudge")] * 5)
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(max_per_minute=1, max_per_day=100))

    call(llm, prompt="a")
    with pytest.raises(LLMUnavailable) as exc:
        call(llm, prompt="b")

    assert exc.value.reason == "rate_limit_exhausted"
    assert exc.value.budget_consumed is False
    assert provider.call_count == 1


def test_budget_exhaustion_raises_before_touching_the_provider(tmp_path: Path) -> None:
    provider = FakeProvider([ToolCall("send_nudge")] * 5)
    budget = DailyBudgetCounter(str(tmp_path / "b.db"), max_per_day=1)
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(100, 100), budget=budget)

    call(llm, prompt="a")
    with pytest.raises(LLMUnavailable) as exc:
        call(llm, prompt="b")

    assert exc.value.reason == "daily_budget_exhausted"
    assert provider.call_count == 1


def test_provider_failure_is_contained_and_marked_as_spent() -> None:
    """A provider timeout consumed real quota, so the budget must reflect it."""
    provider = FakeProvider([RuntimeError("connection reset")])
    limiter = RateLimiter(10, 100)
    llm = GovernedLLM(provider, rate_limiter=limiter)

    with pytest.raises(LLMUnavailable) as exc:
        call(llm)

    assert "provider_error" in exc.value.reason
    assert exc.value.budget_consumed is True
    assert limiter.snapshot()["minute_tokens_remaining"] == 9


def test_a_failed_call_is_not_cached() -> None:
    """Caching a failure would turn a transient blip into a permanent one."""
    provider = FakeProvider([RuntimeError("blip"), ToolCall("send_nudge", {}, "recovered", 0.6)])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(10, 100))

    with pytest.raises(LLMUnavailable):
        call(llm, cache_key="k")
    assert call(llm, cache_key="k").reasoning == "recovered"


def test_governed_llm_is_itself_a_provider() -> None:
    """So it can be injected anywhere a bare provider is accepted."""
    llm = GovernedLLM(FakeProvider([]), rate_limiter=RateLimiter(1, 1))
    assert hasattr(llm, "is_configured") and hasattr(llm, "choose_tool")
    assert llm.name == "governed:fake"


# ── ToolCall parsing ───────────────────────────────────────────────


def test_tool_call_extracts_reasoning_and_confidence_from_arguments() -> None:
    """Rationale arrives inside the same call, so no second turn is needed."""
    call_obj = ToolCall.from_arguments(
        "send_discount", {"discount_percent": 10, "reasoning": "price gap", "confidence": 0.82}
    )
    assert call_obj.reasoning == "price gap"
    assert call_obj.confidence == pytest.approx(0.82)
    assert call_obj.arguments == {"discount_percent": 10}


@pytest.mark.parametrize(
    "raw,expected", [("0.9", 0.9), (1.7, 1.0), (-3, 0.0), (None, 0.5), ("not a number", 0.5)]
)
def test_confidence_is_coerced_into_range(raw, expected) -> None:
    """A model can emit anything here; it still has to end up a usable weight."""
    assert ToolCall.from_arguments("x", {"confidence": raw}).confidence == pytest.approx(expected)


# ── Cache keys ─────────────────────────────────────────────────────


def test_hash_input_is_stable_and_discriminating() -> None:
    assert hash_input({"b": 1, "a": 2}) == hash_input({"a": 2, "b": 1})
    assert hash_input("cart", 1) != hash_input("cart", 2)


def test_hash_input_handles_unserialisable_values() -> None:
    """Context snapshots carry datetimes; a cache key must not raise on them."""
    from datetime import datetime

    assert hash_input({"at": datetime(2026, 1, 1)})


# ── Cache bounds ───────────────────────────────────────────────────


def test_cache_evicts_least_recently_used_past_capacity() -> None:
    """An unbounded cache is a slow leak; a long-lived process sees many shapes."""
    provider = FakeProvider([ToolCall("send_nudge", {}, f"r{i}", 0.5) for i in range(10)])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(100, 1000), cache_max_entries=3)

    for i in range(5):
        call(llm, cache_key=f"k{i}")

    assert len(llm._cache) == 3
    assert llm.cache_peek("k0") is None  # evicted
    assert llm.cache_peek("k4") is not None  # newest retained


def test_reading_an_entry_keeps_it_alive() -> None:
    """LRU, not FIFO: a key that keeps getting hit should not be evicted."""
    provider = FakeProvider([ToolCall("send_nudge", {}, f"r{i}", 0.5) for i in range(10)])
    llm = GovernedLLM(provider, rate_limiter=RateLimiter(100, 1000), cache_max_entries=2)

    call(llm, cache_key="hot")
    call(llm, cache_key="cold")
    call(llm, cache_key="hot")  # refresh recency of "hot"
    call(llm, cache_key="new")  # should evict "cold", not "hot"

    assert llm.cache_peek("hot") is not None
    assert llm.cache_peek("cold") is None


def test_snapshot_reports_cache_capacity() -> None:
    llm = GovernedLLM(FakeProvider([]), rate_limiter=RateLimiter(4, 90), cache_max_entries=500)
    assert llm.snapshot()["cache_capacity"] == 500


# ── Budget counter file isolation ──────────────────────────────────


def test_the_budget_counter_never_shares_a_file_with_the_orm(tmp_path) -> None:
    """WAL gives one writer plus many readers — not two writers.

    Sharing the application database deadlocked the main ingestion path: a
    request holding the write lock from inserting a case would wait on a budget
    write that could not proceed until the request released it.
    """
    from recoveryai.core.llm.governance import DailyBudgetCounter

    main_db = str(tmp_path / "recoveryai.db")
    counter = DailyBudgetCounter(main_db, max_per_day=5)

    assert counter.db_path != main_db
    assert "llm-budget" in counter.db_path


def test_budget_survives_a_write_lock_held_on_the_application_database(tmp_path) -> None:
    """The exact deadlock, reproduced: `POST /events` on an LLM-routed case.

    An open write transaction on the app database used to make every budget
    write time out and raise `database is locked`, so any event that earned a
    model call died while rule-decided events sailed through.
    """
    import sqlite3

    from recoveryai.core.llm.governance import DailyBudgetCounter

    main_db = str(tmp_path / "recoveryai.db")
    holder = sqlite3.connect(main_db)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE cases (id TEXT)")
    holder.commit()

    counter = DailyBudgetCounter(main_db, max_per_day=5)

    # Take and hold the write lock, exactly as a flushed-but-uncommitted session does.
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO cases VALUES ('case_1')")
    try:
        counter.record_call()
        counter.record_call()
        assert counter.remaining == 3
    finally:
        holder.rollback()
        holder.close()


def test_a_broken_budget_file_does_not_take_the_agent_down(tmp_path, monkeypatch) -> None:
    """A counter that cannot be written is a lost count, not a lost decision."""
    import sqlite3

    from recoveryai.core.llm.governance import DailyBudgetCounter

    counter = DailyBudgetCounter(str(tmp_path / "recoveryai.db"), max_per_day=5)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(counter, "_connect", broken)

    counter.record_call()  # must not raise
    assert counter.remaining == 4  # counted in memory instead


# ── Transient failure vs. considered refusal ───────────────────────
#
# These are two different events that both end in the deterministic tables, and
# collapsing them wastes a call arguing with a limit that will not move. A
# dropped connection deserves one more attempt; an exhausted budget does not.


def test_a_dropped_connection_is_retried_once_and_then_succeeds(governed_llm) -> None:
    llm = governed_llm([TimeoutError("connection reset"), ToolCall("send_nudge", {}, "ok", 0.7)])

    result = call(llm)

    assert result.name == "send_nudge"
    assert llm.fake_provider.call_count == 2


def test_two_transient_failures_fall_back_cleanly(governed_llm) -> None:
    """One retry, not a loop: the fallback is instant and was always the
    destination, so a second retry only delays it."""
    llm = governed_llm([TimeoutError("reset"), TimeoutError("reset again")])

    with pytest.raises(LLMUnavailable) as exc:
        call(llm)

    assert exc.value.reason.startswith("transient_error")
    assert exc.value.budget_consumed is True
    assert llm.fake_provider.call_count == 2


def test_a_non_transient_error_is_not_retried(governed_llm) -> None:
    """A malformed response will be malformed the second time too."""
    llm = governed_llm([ValueError("unparseable function call"), ToolCall("send_nudge", {}, "x", 0.7)])

    with pytest.raises(LLMUnavailable) as exc:
        call(llm)

    assert exc.value.reason.startswith("provider_error")
    assert llm.fake_provider.call_count == 1


def test_an_http_503_counts_as_transient(governed_llm) -> None:
    """Providers signal overload by status far more often than by class name."""

    class Overloaded(Exception):
        status_code = 503

    llm = governed_llm([Overloaded(), ToolCall("send_nudge", {}, "ok", 0.7)])

    assert call(llm).name == "send_nudge"
    assert llm.fake_provider.call_count == 2


def test_each_attempt_is_charged_against_the_budget(governed_llm) -> None:
    """Under-counting a provider limit is how a hard cutoff arrives unannounced."""
    llm = governed_llm(
        [TimeoutError("reset"), ToolCall("send_nudge", {}, "ok", 0.7)], max_per_minute=5
    )

    before = llm.rate_limiter.snapshot()["minute_tokens_remaining"]
    call(llm)
    after = llm.rate_limiter.snapshot()["minute_tokens_remaining"]

    assert before - after == 2


def test_a_retry_is_skipped_when_the_rate_limiter_has_nothing_left(governed_llm) -> None:
    """The retry is a fresh request and needs its own headroom; queueing behind
    an empty bucket would trade an instant fallback for a stalled one."""
    llm = governed_llm([TimeoutError("reset"), ToolCall("send_nudge", {}, "ok", 0.7)], max_per_minute=1)

    with pytest.raises(LLMUnavailable) as exc:
        call(llm)

    assert exc.value.reason.startswith("transient_error")
    assert llm.fake_provider.call_count == 1
