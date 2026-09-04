"""hardening: case version, guardrail version, shadow marker, failed deliveries

Revision ID: 5728a0b06b86
Revises: e9aa103c2b34
Create Date: 2026-08-26 23:03:22.869525

Hand-adjusted after autogenerate, in two places:

1. Autogenerate proposed `op.drop_table('llm_daily_budget')`. That table is not
   in `Base.metadata` because it is owned by raw `sqlite3` in
   `llm.governance.DailyBudgetCounter`, not by the ORM — so autogenerate sees it
   as an orphan. Dropping it would destroy the LLM spend counter on any
   deployment predating the sidecar-file change. Removed from both directions.

2. Every new NOT NULL column carries a `server_default`. Without one, a table
   with existing rows cannot take the column at all, so the migration would pass
   on a fresh database and fail on the only databases that matter.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5728a0b06b86'
down_revision: Union[str, Sequence[str], None] = 'e9aa103c2b34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'failed_deliveries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('case_id', sa.String(length=64), nullable=False),
        sa.Column('intent_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('action', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('action_intent', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('failed_deliveries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_failed_deliveries_case_id'), ['case_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_failed_deliveries_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_failed_deliveries_intent_id'), ['intent_id'], unique=False)

    with op.batch_alter_table('case_steps', schema=None) as batch_op:
        # Existing rows were judged by rules we can no longer identify, and
        # saying so is more honest than backfilling them with today's version.
        batch_op.add_column(
            sa.Column(
                'guardrail_version', sa.String(length=32), nullable=False, server_default='unknown'
            )
        )
        # Every pre-existing step was dispatched for real; shadow mode wrote
        # `action_status='shadow_logged'` but nothing durable, so False is the
        # correct backfill for the overwhelming majority and the only value the
        # old schema can justify for the rest.
        batch_op.add_column(
            sa.Column('was_shadow', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        # A step that nothing scored is not a step scored 0.5.
        batch_op.alter_column('confidence', existing_type=sa.FLOAT(), nullable=True)
        batch_op.create_index(batch_op.f('ix_case_steps_was_shadow'), ['was_shadow'], unique=False)

    with op.batch_alter_table('cases', schema=None) as batch_op:
        # Optimistic concurrency token. Existing rows start at 1, which is what
        # the mapper would have assigned them on insert.
        batch_op.add_column(
            sa.Column('version', sa.Integer(), nullable=False, server_default='1')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cases', schema=None) as batch_op:
        batch_op.drop_column('version')

    with op.batch_alter_table('case_steps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_case_steps_was_shadow'))
        # Rows written with a NULL confidence cannot go back into a NOT NULL
        # column, so they are given the old implicit default first.
        op.execute("UPDATE case_steps SET confidence = 0.5 WHERE confidence IS NULL")
        batch_op.alter_column('confidence', existing_type=sa.FLOAT(), nullable=False)
        batch_op.drop_column('was_shadow')
        batch_op.drop_column('guardrail_version')

    with op.batch_alter_table('failed_deliveries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_failed_deliveries_intent_id'))
        batch_op.drop_index(batch_op.f('ix_failed_deliveries_created_at'))
        batch_op.drop_index(batch_op.f('ix_failed_deliveries_case_id'))

    op.drop_table('failed_deliveries')
