"""US-3.1: communications log against a job.

Revision ID: e5f2b0d8c3a1
Revises: d4e1a9c7b2f0
Create Date: 2026-08-24 04:25:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f2b0d8c3a1"
down_revision: str | Sequence[str] | None = "d4e1a9c7b2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_DEFAULT = sa.text("'default'")
_EMPTY = sa.text("''")


def upgrade() -> None:
    op.create_table(
        "job_communications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("job_id", sa.String(length=400), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("next_action", sa.Text(), nullable=False, server_default=_EMPTY),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'phone', 'linkedin', 'other')",
            name="ck_job_communications_channel",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_job_communications_direction",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_job_communications_tenant_job",
        "job_communications",
        ["tenant_id", "job_id"],
    )
    op.create_index(
        "ix_job_communications_job_occurred",
        "job_communications",
        ["job_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_communications_job_occurred", table_name="job_communications")
    op.drop_index("ix_job_communications_tenant_job", table_name="job_communications")
    op.drop_table("job_communications")
