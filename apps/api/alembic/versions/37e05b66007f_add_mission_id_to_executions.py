"""add_mission_id_to_executions

Revision ID: 37e05b66007f
Revises: 5b1f71594ef1
Create Date: 2026-07-02 15:34:49.623485

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '37e05b66007f'
down_revision: Union[str, None] = '5b1f71594ef1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ADR-012: Execution becomes the canonical runtime unit; Mission becomes
    # an aggregation/planning container over its child Executions. Nullable
    # because not every Execution originates from a Mission (e.g. a one-off
    # "quanto é 2+2?" question has no Mission at all).
    op.add_column(
        "executions",
        sa.Column("mission_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_executions_mission_id_missions",
        "executions",
        "missions",
        ["mission_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_executions_mission_id"), "executions", ["mission_id"], unique=False
    )
    # workspace_id was never indexed on this table either (pre-existing gap
    # from migration 5b1f71594ef1) — every query so far filters by it.
    op.create_index(
        op.f("ix_executions_workspace_id"), "executions", ["workspace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_executions_workspace_id"), table_name="executions")
    op.drop_index(op.f("ix_executions_mission_id"), table_name="executions")
    op.drop_constraint(
        "fk_executions_mission_id_missions", "executions", type_="foreignkey"
    )
    op.drop_column("executions", "mission_id")
