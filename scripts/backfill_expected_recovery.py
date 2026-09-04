"""Price cases that predate the expected-recovery column.

The migration adding `expected_recovery` defaults it to zero, because the figure
depends on guardrail capacity derived from `case_steps` — application logic that
does not belong inside a schema migration. Until a case takes another decision it
therefore reads as worth nothing, which is indistinguishable from a case that
genuinely has nothing left to recover.

This prices them once, using the same `economics.assess` the agent uses, so an
open case shows what it is actually worth rather than a placeholder. Terminal
cases are skipped: nothing is going to be recovered on a case that is already
resolved, escalated or closed, and pricing one would put a number on the queue
that no scheduler will ever act on.

    venv/Scripts/python.exe scripts/backfill_expected_recovery.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from recoveryai.core.cases import TERMINAL_STATUSES, CaseStore  # noqa: E402
from recoveryai.core.economics import assess  # noqa: E402
from recoveryai.core.verticals import get_vertical  # noqa: E402
from recoveryai.db.models import Case  # noqa: E402
from recoveryai.db.session import session_scope  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    priced = skipped = 0
    with session_scope() as session:
        store = CaseStore(session)
        cases = session.query(Case).filter(Case.status.notin_(list(TERMINAL_STATUSES))).all()

        for case in cases:
            try:
                vertical = get_vertical(case.vertical)
            except Exception:
                # An unknown vertical is a data problem, not a reason to abort a
                # backfill that is correct for every other row.
                skipped += 1
                continue

            prospects = assess(
                vertical=vertical,
                diagnosis=case.diagnosis,
                amount=case.amount,
                capacity=store.capacity_for(case),
                steps_taken=case.step_count,
            )
            if not args.dry_run:
                case.expected_recovery = prospects.value
            priced += 1
            print(
                f"  {case.id}  {case.vertical:<8} Rs{case.amount:>11,.2f}  "
                f"-> Rs{prospects.value:>10,.2f} via {prospects.action or 'nothing permitted'}"
            )

        if args.dry_run:
            session.rollback()

    verb = "would price" if args.dry_run else "priced"
    print(f"\n{verb} {priced} open case(s); skipped {skipped}")


if __name__ == "__main__":
    main()
