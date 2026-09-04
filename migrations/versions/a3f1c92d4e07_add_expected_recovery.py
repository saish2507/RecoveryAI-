"""Add expected_recovery to cases.

Stores what automation can still expect to collect on a case, net of the cost of
the next attempt. Kept alongside `priority_score` rather than replacing it: the
two answer different questions for different audiences, and collapsing them was
the reason a disputed invoice could sit at the top of a work queue the agent was
forbidden to act on.

Backfilled to 0.0 rather than computed here — the value depends on guardrail
capacity derived from `case_steps`, which is application logic and does not
belong in a migration. Existing cases pick up a real figure on their next
decision; terminal cases never need one.

Revision ID: a3f1c92d4e07
Revises: 5728a0b06b86
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f1c92d4e07"
down_revision: str | Sequence[str] | None = "5728a0b06b86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("expected_recovery", sa.Float(), nullable=False, server_default="0.0"),
    )
    op.create_index("ix_cases_expected_recovery", "cases", ["expected_recovery"])


def downgrade() -> None:
    op.drop_index("ix_cases_expected_recovery", table_name="cases")
    op.drop_column("cases", "expected_recovery")
