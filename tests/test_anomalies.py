"""Population-level detection: the incident forty correct decisions cannot see.

These build cases directly rather than through the agent. The unit under test is
the comparison between two time windows, and driving that through the full
decision loop would make the arrangement of the data — the actual subject — the
hardest part of each test to read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from recoveryai.core.anomalies import MIN_RECENT_COUNT, detect_anomalies
from recoveryai.db.models import Case


def add_case(
    session,
    *,
    vertical: str = "autopay",
    diagnosis: str = "mandate_broken",
    code: str | None = "MANDATE_REVOKED",
    amount: float = 1_000.0,
    minutes_ago: float = 5,
    status: str = "in_progress",
    suffix: str = "",
) -> Case:
    now = datetime.now(UTC)
    created = now - timedelta(minutes=minutes_ago)
    key = f"{vertical}-{diagnosis}-{minutes_ago}-{amount}-{suffix}-{id(session)}"
    case = Case(
        id=f"case_{abs(hash(key)):016x}"[:21],
        idempotency_key=key,
        event_id=key,
        vertical=vertical,
        customer_id=f"cust_{abs(hash(key)) % 10_000}",
        ltv_tier="medium",
        amount=amount,
        status=status,
        diagnosis=diagnosis,
        raw_failure_reason=code or "",
        vertical_metadata={"bank_error_code": code} if code else {},
        created_at=created,
        updated_at=created,
    )
    session.add(case)
    session.flush()
    return case


def test_quiet_traffic_raises_nothing(session) -> None:
    for i in range(2):
        add_case(session, minutes_ago=5, suffix=f"q{i}")
    assert detect_anomalies(session) == []


def test_a_burst_against_a_calm_baseline_is_flagged(session) -> None:
    """Twenty mandate revocations in an hour, against a trickle the day before."""
    for i in range(4):
        add_case(session, minutes_ago=600 + i * 30, suffix=f"base{i}")
    for i in range(20):
        add_case(session, minutes_ago=5 + i, suffix=f"spike{i}")

    found = detect_anomalies(session)
    keys = {a.key for a in found}

    assert "autopay/mandate_broken" in keys
    assert "autopay/MANDATE_REVOKED" in keys
    spike = next(a for a in found if a.key == "autopay/mandate_broken")
    assert spike.recent_count == 20
    assert spike.factor > 3.0


def test_a_signal_never_seen_before_needs_more_than_a_couple(session) -> None:
    """First-ever occurrence is the noisiest signal there is; two is not an incident."""
    for i in range(2):
        add_case(session, code="PSP_ERR_9001", diagnosis="suspicious", minutes_ago=5, suffix=f"n{i}")
    assert detect_anomalies(session) == []

    for i in range(4):
        add_case(session, code="PSP_ERR_9001", diagnosis="suspicious", minutes_ago=6, suffix=f"m{i}")
    assert any(a.key == "autopay/PSP_ERR_9001" for a in detect_anomalies(session))


def test_a_quiet_hour_does_not_cry_wolf(session) -> None:
    """One extra case in a dead window is a 300% rise and means nothing."""
    add_case(session, minutes_ago=600, suffix="base")
    for i in range(MIN_RECENT_COUNT - 1):
        add_case(session, minutes_ago=5, suffix=f"few{i}")
    assert detect_anomalies(session) == []


def test_findings_are_ordered_by_money_not_by_ratio(session) -> None:
    """A 12× spike in ₹200 carts matters less than a 3× spike in ₹40,000 invoices."""
    for i in range(6):
        add_case(session, vertical="cart", diagnosis="distraction", code="C1",
                 amount=200, minutes_ago=5 + i, suffix=f"cart{i}")
    for i in range(6):
        add_case(session, vertical="b2b", diagnosis="cash_flow_trouble", code=None,
                 amount=40_000, minutes_ago=5 + i, suffix=f"b2b{i}")

    found = detect_anomalies(session)
    assert found, "expected both groups to be flagged"
    assert found[0].amount_at_risk >= found[-1].amount_at_risk
    assert found[0].key.startswith("b2b/")


def test_an_escalation_collapse_is_visible_even_when_the_mix_is_normal(session) -> None:
    """A guardrail misconfiguration shows up here and in no per-case view."""
    for i in range(10):
        add_case(session, minutes_ago=600 + i, status="resolved", suffix=f"okbase{i}")
    for i in range(8):
        add_case(session, minutes_ago=5 + i, status="escalated", suffix=f"esc{i}")

    assert any(a.kind == "escalation_spike" for a in detect_anomalies(session))


def test_severity_reflects_exposure(session) -> None:
    for i in range(4):
        add_case(session, minutes_ago=600 + i, suffix=f"sbase{i}")
    for i in range(12):
        add_case(session, amount=20_000, minutes_ago=5 + i, suffix=f"shigh{i}")

    top = detect_anomalies(session)[0]
    assert top.severity == "high"
    assert top.as_dict()["severity"] == "high"


def test_the_window_is_configurable(session) -> None:
    """Widening the window pulls older traffic into 'recent'."""
    for i in range(8):
        add_case(session, minutes_ago=120 + i, suffix=f"old{i}")

    assert detect_anomalies(session, window_minutes=60) == []
    assert detect_anomalies(session, window_minutes=240) != []
