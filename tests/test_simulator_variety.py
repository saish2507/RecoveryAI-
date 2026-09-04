"""Demo traffic must cover the decision space and still look like real data.

Two properties in tension. Coverage wants fixed archetypes; realism wants
variation. The split is that an archetype fixes only what *determines the
diagnosis* — the error code, the dispute flag, which side of a threshold a
signal falls on — and everything else is drawn per case.
"""

from __future__ import annotations

import collections

from recoveryai.core.diagnosis import diagnose
from recoveryai.simulator import (
    PERMUTATION_CYCLE_LENGTH,
    PERMUTATION_MATRIX,
    build_permutation_event,
)


def test_a_lap_covers_every_archetype_exactly_once() -> None:
    labels = [build_permutation_event(i)[1] for i in range(PERMUTATION_CYCLE_LENGTH)]
    assert len(set(labels)) == PERMUTATION_CYCLE_LENGTH


def test_every_archetype_still_lands_on_its_intended_diagnosis() -> None:
    """Randomising the numbers must not randomise the meaning.

    A `days_overdue` drawn too wide would silently turn the "forgot" archetype
    into `cash_flow_trouble`, and the walk would stop covering what it claims to.
    """
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for i in range(600):
        event, label = build_permutation_event(i)
        seen[label].add(diagnose(event)[0])

    for label, diagnoses in seen.items():
        assert len(diagnoses) == 1, f"{label} drifted across diagnoses: {diagnoses}"


def test_two_cases_from_the_same_slot_do_not_look_alike() -> None:
    """The bug this replaced: the b2b/forgot slot was ₹41,000 on every lap.

    A queue of rows identical but for their id reads as placeholder data no
    matter how correct the logic behind it is.
    """
    for slot in range(PERMUTATION_CYCLE_LENGTH):
        amounts = {
            build_permutation_event(slot + lap * PERMUTATION_CYCLE_LENGTH)[0].amount
            for lap in range(40)
        }
        assert len(amounts) == 40, f"slot {slot} repeated an amount"


def test_amounts_are_not_round_numbers() -> None:
    """Real invoices are 18,742.63. A column of round figures looks synthetic."""
    amounts = [build_permutation_event(i)[0].amount for i in range(100)]
    assert not any(a == round(a) for a in amounts)


def test_amounts_stay_inside_their_archetypes_range() -> None:
    for slot, spec in enumerate(PERMUTATION_MATRIX):
        low, high = spec["amount_range"]
        for lap in range(30):
            amount = build_permutation_event(slot + lap * PERMUTATION_CYCLE_LENGTH)[0].amount
            assert low <= amount <= high + 1, f"{spec['label']} produced {amount}"


def test_the_walk_spreads_across_ltv_tiers() -> None:
    """A slot weighted toward one tier should still produce the others."""
    tiers = collections.Counter(
        build_permutation_event(i * PERMUTATION_CYCLE_LENGTH)[0].customer_ltv_tier.value
        for i in range(60)
    )
    assert len(tiers) >= 2


def test_failure_codes_vary_within_an_archetype() -> None:
    """Four different real decline codes beat one repeated forever."""
    codes = {
        build_permutation_event(lap * PERMUTATION_CYCLE_LENGTH)[0].raw_failure_reason
        for lap in range(40)
    }
    assert len(codes) > 1


def test_customer_ids_stay_unique_across_laps() -> None:
    ids = [build_permutation_event(i)[0].customer_id for i in range(200)]
    assert len(set(ids)) == 200
